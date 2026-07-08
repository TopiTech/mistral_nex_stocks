"""
session_manager.py - YFinance session management.

Extracted from app_state.py to reduce module complexity.
Manages yfinance sessions with user-agent rotation, curl_cffi impersonation,
and rate-limit detection.
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

logger = logging.getLogger("backend")

YFINANCE_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:137.0) Gecko/20100101 Firefox/137.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Safari/605.1.15",
]

try:
    from curl_cffi import requests as curl_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False


class YFinanceSessionManager:
    """
    Manages yfinance HTTP sessions with UA rotation and browser fingerprint
    impersonation via curl_cffi. Singleton pattern.
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
                    self._last_request_ts = 0.0
                    self._request_min_interval_sec = YFINANCE_REQ_MIN_INTERVAL_BASE
                    # Adaptive spacing interval: grows on blocks, relaxes when quiet.
                    self._adaptive_interval_sec = YFINANCE_REQ_MIN_INTERVAL_BASE
                    self._last_block_ts = 0.0
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

    def _create_session(self, ua):
        """Create a session that mimics Chrome browser fingerprint."""
        if CURL_CFFI_AVAILABLE:
            session: Any = curl_requests.Session(impersonate="chrome")
        else:
            import requests
            session = requests.Session()

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

            if wait_time > 0.0:
                time.sleep(wait_time)

            # Thundering-herd guard: cap concurrent in-flight yfinance HTTP requests.
            with self._concurrency_semaphore:
                resp = original_request(*args, **kwargs)
            try:
                status_code = getattr(resp, "status_code", None)
                if status_code in (401, 402, 429, 439):
                    url = kwargs.get("url") or (args[1] if len(args) > 1 else "")
                    label = {
                        401: "401 (Invalid Crumb)",
                        402: "402 (Payment Required)",
                        429: "429 (Too Many Requests)",
                        439: "439 (Blocked)",
                    }.get(status_code, str(status_code))
                    # A block means we must slow down further and rotate the session
                    # (UA + fresh crumb). Combine the adaptive spacing growth with any
                    # server-provided Retry-After hint for the exclusion window.
                    retry_after = self._parse_retry_after(resp)
                    with self._lock:
                        self._adaptive_interval_sec = min(
                            self._adaptive_interval_sec * YFINANCE_REQ_INTERVAL_GROWTH,
                            YFINANCE_REQ_MIN_INTERVAL_MAX,
                        )
                        self._last_block_ts = time.time()
                        default_dur = 300 if status_code == 429 else 5
                        duration = max(default_dur, retry_after) if retry_after else default_dur
                        self.mark_rate_limited("yfinance", duration=int(duration))
                    logger.warning(
                        "yfinance session received %s for url: %s (retry_after=%.0fs, interval=%.1fs)",
                        label, url, retry_after or 0.0, self._adaptive_interval_sec,
                    )
            except Exception as e:
                logger.debug("Error in session wrapper: %s", e)
            return resp

        session.request = custom_request
        with self._lock:
            self._all_sessions.append(session)
        return session

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

            if idx in self._local.sessions:
                sess, epoch = self._local.sessions[idx]
                if epoch == current_epoch:
                    return sess
                try:
                    sess.close()
                except Exception as exc:
                    logger.debug("Failed to close yfinance session: %s", exc)
                self._local.sessions.pop(idx, None)

            ua = YFINANCE_USER_AGENTS[idx]
            sess = self._create_session(ua)
            self._local.sessions[idx] = (sess, current_epoch)
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

    @staticmethod
    def _parse_retry_after(resp) -> float:
        """Parse a Retry-After header (seconds or HTTP-date) from a response."""
        try:
            headers = getattr(resp, "headers", None)
            if headers is None:
                return 0.0
            if isinstance(headers, dict):
                raw = headers.get("Retry-After") or headers.get("retry-after")
            else:
                get = getattr(headers, "get", None)
                raw = get("Retry-After") if get else None
                if not raw:
                    raw = get("retry-after") if get else None
            if not raw:
                return 0.0
            try:
                return float(raw)
            except (TypeError, ValueError):
                from email.utils import parsedate_to_datetime

                try:
                    dt = parsedate_to_datetime(str(raw))
                    return max(0.0, dt.timestamp() - time.time())
                except Exception:
                    return 0.0
        except Exception:
            return 0.0

    def get_request_interval(self) -> float:
        """Current adaptive spacing interval between yfinance requests (seconds)."""
        with self._lock:
            return self._adaptive_interval_sec

    def clear_rate_limit(self, key="default"):
        """Clear rate limit state for a key."""
        with self._lock:
            if key in self._excluded_until:
                self._excluded_until[key] = 0

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
            self._adaptive_interval_sec = YFINANCE_REQ_MIN_INTERVAL_BASE
            self._last_block_ts = 0.0
            self._session_epoch += 1


yf_session_manager = YFinanceSessionManager()
