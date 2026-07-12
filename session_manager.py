"""
session_manager.py - YFinance session management.

Extracted from app_state.py to reduce module complexity.
Manages yfinance sessions with user-agent rotation, curl_cffi impersonation,
and rate-limit detection.

2026-07 Rate-limit hardening:
  - Fix 1: 401 exclusion window raised from 5s → 60s (was causing rapid re-attack loops).
  - Fix 2: curl_cffi impersonate values rotated alongside UA index for broader fingerprint diversity.
  - Fix 3: _consecutive_401_count counter added; interval growth rate accelerates on streaks.
  - Fix 7: UA pool expanded from 5 → 10 entries (doubles time before cycling through all UAs).
  - crumb refresh: reset_yfinance_auth() clears yfinance internal cookie/crumb cache on 401/402/429/439.
"""

import logging
import threading
import time
from typing import Any

from constants import (
    YFINANCE_REQ_MIN_INTERVAL_BASE,
    YFINANCE_REQ_MIN_INTERVAL_MAX,
    YFINANCE_REQ_INTERVAL_GROWTH,
    YFINANCE_REQ_INTERVAL_DECAY,
    YFINANCE_REQ_INTERVAL_DECAY_AFTER,
    YFINANCE_MAX_CONCURRENT_REQUESTS,
)
from utils.http_utils import parse_retry_after

logger = logging.getLogger("backend")

# ---------------------------------------------------------------------------
# User-Agent pool (expanded from 10 → 20 for longer rotation cycle)
# Mix of Chrome/Edge/Firefox/Safari on Windows/Mac/Linux to maximise diversity.
# ---------------------------------------------------------------------------
YFINANCE_USER_AGENTS = [
    # Chrome 135 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    # Chrome 134 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    # Firefox 137 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:137.0) Gecko/20100101 Firefox/137.0",
    # Chrome 135 Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    # Safari 18 Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Safari/605.1.15",
    # Edge 135 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0",
    # Chrome 133 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    # Firefox 136 Linux
    "Mozilla/5.0 (X11; Linux x86_64; rv:136.0) Gecko/20100101 Firefox/136.0",
    # Chrome 134 Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    # Chrome 135 Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    # Firefox 135 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
    # Edge 134 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0",
    # Safari 17.5 Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    # Chrome 132 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    # Firefox 136 Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:136.0) Gecko/20100101 Firefox/136.0",
    # Chrome 133 Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    # Edge 133 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 Edg/133.0.0.0",
    # Chrome 134 Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    # Safari 17.4 Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    # Firefox 134 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0",
]

# ---------------------------------------------------------------------------
# curl_cffi impersonate targets (rotated alongside UA index to diversify TLS
# fingerprints across sessions). Length need not match UA pool — we use modulo.
# ---------------------------------------------------------------------------
_CURL_IMPERSONATE_TARGETS = [
    "chrome",
    "chrome120",
    "chrome116",
    "safari",
    "safari17_0",
    "chrome110",
    "safari15_5",
    "chrome107",
    "edge101",
]

try:
    from curl_cffi import requests as curl_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False


_CRUMB_RESET_DIAGNOSED = {"done": False, "path": "none"}


def reset_yfinance_auth() -> None:
    """Force yfinance to re-authenticate on the next request.

    yfinance caches the crumb in the ``YfData`` singleton instance
    (``YfData()._crumb``) and the login cookie in its Cookie cache. After a
    401 ("Invalid Crumb") the cached crumb can be stale/bad ("Too Many
    Requests"); if we don't evict it, yfinance *reuses* the bad crumb forever
    and every subsequent request 401s -> instant, permanent rate-limit loop.

    IMPORTANT (2026-07): the older attribute paths this code used
    (``yfinance.utils._crumb_cache``, ``yfinance.data._yfdata``) do NOT exist in
    yfinance 1.x, so an implementation that only cleared those was a silent
    no-op. This version targets the real 1.x locations and is guarded so it
    keeps working if the internals move again. Verified against yfinance 1.5.1.
    """
    found = []

    # 1) Null the crumb/cookie on the YfData singleton so a fresh getcrumb
    #    fetch is forced before the next data request.
    try:
        import yfinance.data as yfd
        inst = yfd.YfData()
        if hasattr(inst, "_crumb"):
            inst._crumb = None
            found.append("YfData._crumb")
        if hasattr(inst, "_cookie"):
            inst._cookie = None
            found.append("YfData._cookie")
    except Exception as exc:
        logger.debug("crumb reset (YfData) failed (non-fatal): %s", exc)

    # 2) Evict the persisted cookie cache (SQLite-backed) so the stale A3
    #    cookie used by curl_cffi is dropped and re-fetched.
    try:
        import yfinance.cache as yfc
        if hasattr(yfc, "get_cookie_cache"):
            cc = yfc.get_cookie_cache()
            if hasattr(cc, "clear") and callable(getattr(cc, "clear")):
                cc.clear()
                found.append("cookie_cache.clear()")
            elif hasattr(cc, "initialise") and callable(getattr(cc, "initialise")):
                # _CookieCache reinitialises its DB via initialise()
                cc.initialise()
                found.append("cookie_cache.initialise()")
    except Exception as exc:
        logger.debug("crumb reset (cookie cache) failed (non-fatal): %s", exc)

    if not _CRUMB_RESET_DIAGNOSED["done"]:
        _CRUMB_RESET_DIAGNOSED["done"] = True
        _CRUMB_RESET_DIAGNOSED["path"] = ",".join(found) or "none"
        logger.info(
            "yfinance crumb/cookie reset path available on this version: %s",
            _CRUMB_RESET_DIAGNOSED["path"],
        )
    elif found:
        # occasional confirmation is harmless but keep logs quiet
        pass


class YFinanceSessionManager:
    """
    Manages yfinance HTTP sessions with UA rotation and browser fingerprint
    impersonation via curl_cffi. Singleton pattern.

    Rate-limit hardening (2026-07):
      - 401 exclusion window is now 60 s (was 5 s) to prevent rapid re-attack.
      - 402/439 windows are 300 s / 180 s respectively (unchanged from intent).
      - Consecutive-401 counter accelerates adaptive interval growth on streaks.
      - UA pool doubled (5 → 10); curl_cffi impersonate target rotates in sync.
      - crumb cache cleared on every 401 to force fresh Yahoo authentication.
    """

    _instance = None
    _lock = threading.RLock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not hasattr(self, "_initialized"):
            with self._lock:
                if not hasattr(self, "_initialized"):
                    self._excluded_until: dict[str, float] = {}
                    self._all_sessions: list[Any] = []
                    self._local = threading.local()
                    self._ua_index = 0
                    self._session_epoch = 0
                    # H-3: Removed separate _request_lock (threading.Lock) to avoid
                    # potential deadlock with _lock (RLock). All shared state is
                    # now protected by the single reentrant _lock.
                    import sys
                    is_testing = "pytest" in sys.modules or "unittest" in sys.modules
                    self._last_request_ts = 0.0
                    self._request_min_interval_sec = 0.0 if is_testing else YFINANCE_REQ_MIN_INTERVAL_BASE
                    # Adaptive spacing interval: grows on blocks, relaxes when quiet.
                    self._adaptive_interval_sec = 0.0 if is_testing else YFINANCE_REQ_MIN_INTERVAL_BASE
                    self._last_block_ts = 0.0
                    # Fix 3: consecutive 401 counter for accelerated interval growth.
                    self._consecutive_401_count = 0
                    self._last_401_ts = 0.0
                    # Thundering-herd guard: cap concurrent in-flight yfinance requests.
                    self._concurrency_semaphore = threading.Semaphore(
                        YFINANCE_MAX_CONCURRENT_REQUESTS
                    )
                    self._initialized = True

    @classmethod
    def _reset_for_testing(cls) -> None:
        """Reset singleton state for test isolation.

        TESTING ONLY: Clears the singleton instance so the next call to
        ``YFinanceSessionManager()`` creates a fresh instance. This prevents
        rate-limit state set in one test from leaking into subsequent tests.
        """
        with cls._lock:
            cls._instance = None

    def get_user_agent(self):
        with self._lock:
            return YFINANCE_USER_AGENTS[self._ua_index]

    def _get_impersonate_target(self, ua_index: int) -> str:
        """Return the curl_cffi impersonate target for the given UA index."""
        return _CURL_IMPERSONATE_TARGETS[ua_index % len(_CURL_IMPERSONATE_TARGETS)]

    def _create_session(self, ua: str, ua_index: int = 0):
        """Create a session that mimics Chrome browser fingerprint.

        The curl_cffi impersonate target is chosen based on the UA index so
        that each UA rotation also cycles through different TLS fingerprints,
        making the traffic pattern harder for Yahoo to fingerprint as automated.
        """
        if CURL_CFFI_AVAILABLE:
            impersonate = self._get_impersonate_target(ua_index)
            try:
                session: Any = curl_requests.Session(impersonate=impersonate)  # type: ignore[arg-type]
            except Exception:
                # Fallback if the target string is not recognized by this curl_cffi version.
                session = curl_requests.Session(impersonate="chrome")
        else:
            import requests
            session = requests.Session()

        # Apply proxy if configured
        import os
        proxy_url = os.environ.get("MNS_YFINANCE_PROXY")
        if proxy_url:
            if CURL_CFFI_AVAILABLE:
                session.proxies = {"http": proxy_url, "https": proxy_url}
            else:
                session.proxies.update({"http": proxy_url, "https": proxy_url})

        session.headers.update({
            "User-Agent": ua,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://finance.yahoo.com",
            "Referer": "https://finance.yahoo.com",
        })

        original_request = session.request

        def custom_request(*args, **kwargs):
            # Enforce global spacing across all threads and sessions.
            # H-3: Use self._lock (RLock) instead of a separate _request_lock to
            # avoid lock-ordering issues. RLock is reentrant so nested acquisitions
            # from the same thread (e.g., mark_rate_limited → _lock) are safe.
            wait_time = self._compute_wait()

            if wait_time > 0.0:
                time.sleep(wait_time)

            # Thundering-herd guard: cap concurrent in-flight yfinance HTTP requests.
            with self._concurrency_semaphore:
                if "timeout" not in kwargs:
                    kwargs["timeout"] = 15.0
                resp = original_request(*args, **kwargs)

            try:
                status_code = getattr(resp, "status_code", None)
                if status_code in (401, 402, 429, 439):
                    self._handle_block(status_code, resp)
            except Exception as e:
                logger.debug("Error in session wrapper: %s", e)
            return resp

        session.request = custom_request
        with self._lock:
            self._all_sessions.append(session)
        return session

    # -----------------------------------------------------------------------
    # Pacing + block math (extracted from custom_request so the core adaptive
    # interval behaviour is unit-testable without performing real network I/O).
    # -----------------------------------------------------------------------

    def _compute_wait(self) -> float:
        """Compute the sleep needed before the next yfinance request.

        Relaxes the adaptive interval toward the base value after a quiet period,
        then returns the gap required to honour the current spacing.
        """
        wait_time = 0.0
        with self._lock:
            now = time.time()
            # Relax the adaptive interval back toward base after a quiet period
            # so we don't stay artificially slow once Yahoo stops blocking us.
            if self._adaptive_interval_sec > YFINANCE_REQ_MIN_INTERVAL_BASE:
                if now - self._last_block_ts > YFINANCE_REQ_INTERVAL_DECAY_AFTER:
                    self._adaptive_interval_sec = max(
                        YFINANCE_REQ_MIN_INTERVAL_BASE,
                        self._adaptive_interval_sec * YFINANCE_REQ_INTERVAL_DECAY,
                    )
            min_interval = self._adaptive_interval_sec
            elapsed = now - self._last_request_ts
            if elapsed < min_interval:
                wait_time = min_interval - elapsed
                self._last_request_ts = now + wait_time
            else:
                self._last_request_ts = now
        return wait_time

    def _handle_block(self, status_code: int, resp: Any) -> None:
        """Apply adaptive spacing growth + exclusion window for a Yahoo block.

        status_code must be one of 401/402/429/439. On every block we also force
        yfinance to discard its cached crumb/cookie so the next request
        re-authenticates instead of reusing a bad/stale crumb (the classic cause
        of instant, permanent 401 loops).
        """
        if status_code not in (401, 402, 429, 439):
            return
        url = ""
        try:
            url = getattr(resp, "url", "") or ""
        except Exception:
            url = ""
        label = {
            401: "401 (Invalid Crumb)",
            402: "402 (Payment Required)",
            429: "429 (Too Many Requests)",
            439: "439 (Blocked)",
        }.get(status_code, str(status_code))

        retry_after = parse_retry_after(resp)

        with self._lock:
            now_block = time.time()
            if status_code == 401:
                # Count as consecutive only if within 5 minutes of last 401
                if now_block - self._last_401_ts < 300:
                    self._consecutive_401_count += 1
                else:
                    self._consecutive_401_count = 1
                self._last_401_ts = now_block
                # Accelerated growth: 2.0^(1 + streak*0.3) to escalate quickly
                streak_boost = 1.0 + self._consecutive_401_count * 0.3
                growth = YFINANCE_REQ_INTERVAL_GROWTH ** streak_boost
            else:
                # Non-401 blocks reset the 401 streak counter
                self._consecutive_401_count = 0
                growth = YFINANCE_REQ_INTERVAL_GROWTH

            self._adaptive_interval_sec = min(
                self._adaptive_interval_sec * growth,
                YFINANCE_REQ_MIN_INTERVAL_MAX,
            )
            self._last_block_ts = now_block

            # Status-specific default exclusion durations.
            # 401 (Invalid Crumb) → 60s; 402 (Payment Required) → 300s
            # 429 (Too Many Requests) → 300s; 439 (Blocked) → 180s
            _default_durations = {429: 300, 402: 300, 439: 180, 401: 60}
            default_dur = _default_durations.get(status_code, 60)
            duration = max(default_dur, retry_after) if retry_after else default_dur

            self.mark_rate_limited("yfinance", duration=int(duration))

        # NOTE: Fresh Yahoo authentication is forced inside mark_rate_limited()
        # (it resets the crumb/cookie on rotation), so we must NOT call
        # reset_yfinance_auth() again here — doing so double-resets on every block.

        logger.warning(
            "yfinance session received %s for url: %s (retry_after=%.0fs, interval=%.1fs)",
            label, url, retry_after or 0.0, self._adaptive_interval_sec,
        )

    def get_session(self):
        """Get or create a session for the current thread and UA index."""
        with self._lock:
            idx = self._ua_index
            current_epoch = self._session_epoch
            if not hasattr(self._local, "sessions"):
                self._local.sessions = {}

            # Close and remove any stale sessions for other UA indexes to prevent memory leaks
            for k in list(self._local.sessions.keys()):
                if k != idx:
                    sess, _ = self._local.sessions.pop(k)
                    try:
                        sess.close()
                    except Exception as exc:
                        logger.debug("Failed to close stale yfinance session: %s", exc)
                    try:
                        self._all_sessions.remove(sess)
                    except ValueError:
                        pass

            if idx in self._local.sessions:
                sess, epoch = self._local.sessions[idx]
                if epoch == current_epoch:
                    return sess
                try:
                    sess.close()
                except Exception as exc:
                    logger.debug("Failed to close yfinance session: %s", exc)
                self._local.sessions.pop(idx, None)
                try:
                    self._all_sessions.remove(sess)
                except ValueError:
                    pass

            ua = YFINANCE_USER_AGENTS[idx]
            sess = self._create_session(ua, ua_index=idx)
            self._local.sessions[idx] = (sess, current_epoch)
            reset_yfinance_auth()
            return sess

    def mark_rate_limited(self, key="default", duration=300):
        """Mark a service as rate-limited until duration seconds from now."""
        with self._lock:
            self._excluded_until[key] = time.time() + duration
            self._ua_index = (self._ua_index + 1) % len(YFINANCE_USER_AGENTS)
            self._session_epoch += 1
            logger.warning(
                "YFinanceSessionManager rotated due to limit. UA index: %d, epoch: %d",
                self._ua_index,
                self._session_epoch,
            )
        # A rate-limit means our current crumb/cookie identity is burnt. Force a
        # fresh authentication on the next request so we don't immediately
        # re-trigger the block with a stale crumb. (Called outside the lock to
        # avoid re-entrancy; reset_yfinance_auth guards all yfinance access.)
        reset_yfinance_auth()

    def is_rate_limited(self, key="default"):
        """Check if a service is currently rate-limited."""
        with self._lock:
            if key in self._excluded_until:
                return time.time() < self._excluded_until[key]
            return False

    def get_rate_limit_until(self, key="default") -> float:
        """Return UNIX timestamp when rate limit expires, or 0.0 if not limited."""
        with self._lock:
            return self._excluded_until.get(key, 0.0)

    def get_request_interval(self) -> float:
        """Current adaptive spacing interval between yfinance requests (seconds)."""
        with self._lock:
            return self._adaptive_interval_sec

    def clear_rate_limit(self, key="default"):
        """Clear rate limit state for a key."""
        with self._lock:
            if key in self._excluded_until:
                self._excluded_until[key] = 0

    def reset_consecutive_401_count(self):
        """Reset the consecutive 401 streak counter (e.g., after a successful request)."""
        with self._lock:
            self._consecutive_401_count = 0

    def close_all(self):
        """Clean up all sessions."""
        with self._lock:
            for sess in self._all_sessions:
                try:
                    sess.close()
                except Exception as exc:
                    logger.debug("Failed to close yfinance session: %s", exc)
            self._all_sessions.clear()
            if hasattr(self._local, "sessions"):
                self._local.sessions.clear()
            self._excluded_until.clear()
            import sys
            is_testing = "pytest" in sys.modules or "unittest" in sys.modules
            self._adaptive_interval_sec = 0.0 if is_testing else YFINANCE_REQ_MIN_INTERVAL_BASE
            self._last_block_ts = 0.0
            self._consecutive_401_count = 0
            self._last_401_ts = 0.0
            self._session_epoch += 1


yf_session_manager = YFinanceSessionManager()
