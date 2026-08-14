"""
route_helpers.py - Helper functions shared between app.py and routes/*.py
These are extracted from app.py to break the circular import.
"""

import hashlib
import ipaddress
import logging
import os
import re
import threading
import time
from functools import wraps
from typing import Any, cast

from flask import Flask, current_app, g, request

from app_state import app_state
from constants import MAX_STOCK_NAME_LENGTH
from credential_manager import get_langsearch_api_key, get_mistral_api_key, get_tavily_api_key
from error_codes import ErrorCode
from utils.caching import clear_cache_prefix
from utils.env_helpers import _env_int, _is_production_env
from utils.networking import _is_loopback_ip
from utils.normalization import (
    is_valid_symbol,
    normalize_market,
    normalize_symbol,
    normalize_symbol_for_market,
    normalize_text,
)
from utils.stock_payload import (
    _default_stock_names,
    _get_stock_container,
    clear_yfinance_short_cache_prefix,
    error_response,
)
from utils.text_utils import _token_fingerprint

logger = logging.getLogger(__name__)


def _as_text(value: Any) -> str:
    return "" if value is None else str(value)


# ============================================================
# Rate Limiting
# ============================================================
_rate_limit_store: dict[str, list[float]] = {}
_rate_limit_window_by_key: dict[str, int] = {}
_rate_limit_lock = threading.Lock()
_RATE_LIMIT_CLEANUP_INTERVAL: int = _env_int("MNS_RATE_LIMIT_CLEANUP_INTERVAL", 60, 10, 3600)
_RATE_LIMIT_MAX_ENTRIES: int = _env_int("MNS_RATE_LIMIT_MAX_ENTRIES", 1000, 100, 50000)
# Bounded number of "polling duplicate" requests a single request_token may
# skip within a rate-limit window (see skip_polling_duplicates). Legitimate
# clients poll an in-flight async job a handful of times (the UI polls at most
# ~8 times per job), while an attacker reusing one token must not be able to
# bypass the endpoint quota indefinitely: after the short-lived result cache
# expires, every poll can start a NEW upstream (paid) AI job, so one token
# could otherwise burn unlimited Mistral quota.
_RATE_LIMIT_MAX_TOKEN_POLLS: int = _env_int("MNS_RATE_LIMIT_MAX_TOKEN_POLLS", 120, 1, 100000)
# R2 hardening: client-controlled request_token must be bounded before it becomes
# a rate-limit key. An unbounded per-token entry would let a single client
# spray distinct tokens and evict legitimate endpoint buckets (1000-entry cap).
_RATE_LIMIT_MAX_REQUEST_TOKEN_LEN: int = _env_int(
    "MNS_RATE_LIMIT_MAX_REQUEST_TOKEN_LEN", 128, 32, 1024
)
# Max distinct per-token buckets per client IP / endpoint pair (random-token
# flood mitigation). Once this budget is spent, new tokens count normally so
# they cannot be used to poll-bypass the quota via distinct-token floods.
_RATE_LIMIT_MAX_DISTINCT_TOKENS: int = _env_int("MNS_RATE_LIMIT_MAX_DISTINCT_TOKENS", 40, 5, 500)
# Mapping endpoint -> client_key prefix -> count of distinct token buckets.
_rate_limit_distinct_token_counts: dict[str, int] = {}
_rate_limit_last_cleanup: float = time.monotonic()
# M-4: This in-memory store is intentionally not persisted to disk.
# Rate limits reset on server restart. This is acceptable for a personal-use
# local app but would need a persistent backend (Redis, etc.) for production.


def _cleanup_rate_limit_store() -> None:
    """Remove expired rate-limit entries to prevent memory leaks.

    NOTE: Caller MUST hold _rate_limit_lock when calling this function.
    """
    current_time = time.monotonic()
    keys_to_delete = []
    for key, timestamps in _rate_limit_store.items():
        cleanup_window = max(1, _rate_limit_window_by_key.get(key, 300))
        filtered = [t for t in timestamps if current_time - t < cleanup_window]
        if filtered:
            _rate_limit_store[key] = filtered
        else:
            keys_to_delete.append(key)
    for key in keys_to_delete:
        del _rate_limit_store[key]
        _rate_limit_window_by_key.pop(key, None)

    # Rebuild distinct-token counters from live token entries (expired tokens
    # drop out naturally when their window lapses or the key is evicted).
    # R2: keeps the per-client distinct-token budget accurate after cleanup.
    _rate_limit_distinct_token_counts.clear()
    for key in _rate_limit_store:
        if ":token:" in key:
            client_prefix = key.rsplit(":token:", 1)[0]
            client_prefix = f"{client_prefix}:distinct"
            _rate_limit_distinct_token_counts[client_prefix] = (
                _rate_limit_distinct_token_counts.get(client_prefix, 0) + 1
            )

    # When store exceeds capacity, evict oldest entries first
    # L-6: Sort by the FIRST (oldest) timestamp [0], not the last [-1].
    # Using [-1] would evict the most-recently-active entries instead of the oldest.
    if len(_rate_limit_store) > _RATE_LIMIT_MAX_ENTRIES:
        sorted_keys = sorted(
            _rate_limit_store.keys(),
            key=lambda k: _rate_limit_store[k][0] if _rate_limit_store[k] else 0,
        )
        excess = len(_rate_limit_store) - _RATE_LIMIT_MAX_ENTRIES
        for old_key in sorted_keys[:excess]:
            del _rate_limit_store[old_key]
            _rate_limit_window_by_key.pop(old_key, None)
        # Eviction removed token buckets — rebuild distinct counters to stay accurate.
        _rate_limit_distinct_token_counts.clear()
        for key in _rate_limit_store:
            if ":token:" in key:
                client_prefix = key.rsplit(":token:", 1)[0]
                client_prefix = f"{client_prefix}:distinct"
                _rate_limit_distinct_token_counts[client_prefix] = (
                    _rate_limit_distinct_token_counts.get(client_prefix, 0) + 1
                )


def _is_polling_token_inflight(token: str) -> bool:
    """Return True if same-token polling may skip quota.

    Returns False only when we positively know the token's job has
    completed (result cached without inflight entry). Otherwise returns
    True — the per-token cap (120) bounds the bypass even for unknown
    tokens, while returning False here would break legitimate polling
    that has not yet established a conversation scope.
    """
    try:
        from flask import session as _flask_session

        conv = _flask_session.get("mns_analysis_conversation")  # type: ignore[attr-defined]
        if isinstance(conv, str) and conv:
            inflight_keys = (
                f"chat:{conv}:{token}",
                f"analyze:{conv}:{token}",
            )
            try:
                from routes.api_analysis import (  # local import to avoid cycle
                    analyze_fetch_inflight,
                    analyze_result_cache,
                    chat_fetch_inflight,
                    chat_result_cache,
                )

                for k in inflight_keys:
                    if k in chat_fetch_inflight or k in analyze_fetch_inflight:
                        return True
                # Suffix match for any session variant
                for stored_key in list(analyze_fetch_inflight.keys()) + list(
                    chat_fetch_inflight.keys()
                ):
                    if stored_key.endswith(f":{token}"):
                        return True
                # Known-completed: result cached but not inflight -> must count
                for k in inflight_keys:
                    if k in chat_result_cache or k in analyze_result_cache:
                        return False
                for cached_key in list(chat_result_cache.keys()) + list(
                    analyze_result_cache.keys()
                ):
                    if cached_key.endswith(f":{token}"):
                        return False
            except (ImportError, AttributeError):
                pass
        # Check news inflight (token alone, no conversation scope)
        try:
            from routes.api_analysis import news_fetch_inflight

            if token in news_fetch_inflight:
                return True
        except (ImportError, AttributeError):
            pass
    except RuntimeError:
        return True
    # Inside a request without conversation scope and no cache hit:
    # conservatively allow bounded skip (the per-token cap of 120 still
    # prevents unbounded bypass; returning False here would break
    # legitimate polling that has not yet established a chat/analyze
    # conversation scope — e.g. first-poll after token registration).
    return True


def _rate_limit_env_name(endpoint: str, suffix: str) -> str:
    safe_endpoint = re.sub(r"[^A-Za-z0-9]+", "_", (endpoint or "default")).upper()
    return f"MNS_RATE_LIMIT_{safe_endpoint}_{suffix}"


def _resolve_rate_limit(endpoint: str, default_max: int, default_window: int) -> tuple[int, int]:
    # Precedence: endpoint-specific env > decorator argument (code default)
    # If endpoint-specific env is set, use it directly.
    # Otherwise, return the decorator's default value.
    resolved_max = _env_int(_rate_limit_env_name(endpoint, "MAX"), default_max, 1, 100000)
    resolved_window = _env_int(_rate_limit_env_name(endpoint, "WINDOW"), default_window, 1, 86400)
    return resolved_max, resolved_window


def _rate_limit_identity() -> tuple[str, bool]:
    """Resolve the (client key, is_local) pair used for rate limiting.

    Direct-listener mode (default): the raw socket peer address is the only
    trustworthy identity, because ProxyFix may have rewritten REMOTE_ADDR from
    an attacker-supplied X-Forwarded-For.

    Remote / reverse-proxy mode (``MNS_ALLOW_REMOTE_API=1`` with
    ``MNS_PROXY_FIX=1``): RAW_REMOTE_ADDR is the *proxy's* address - typically
    loopback - and is identical for every client. Bucketing on it would make
    all remote callers share one quota (mutual DoS) and would grant them the
    loopback multiplier / local bypass. In that mode the proxy-supplied address
    (``request.remote_addr``, set by ProxyFix from X-Forwarded-For) is the
    correct per-client identity, and no request counts as local. ProxyFix has
    already selected the address using the configured trusted-hop count; raw
    X-Forwarded-For elements must not be parsed again here.
    """
    allow_remote = os.environ.get("MNS_ALLOW_REMOTE_API", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    proxied = os.environ.get("MNS_PROXY_FIX", "").strip().lower() in ("1", "true", "yes")
    if allow_remote and proxied:
        client_ip = (request.remote_addr or "").strip()
        try:
            return str(ipaddress.ip_address(client_ip)), False
        except ValueError:
            # One shared bucket prevents malformed proxy values from becoming
            # an attacker-controlled source of unlimited identities.
            return "invalid-proxy-client", False

    raw_remote = request.environ.get("RAW_REMOTE_ADDR")
    remote_addr = str(raw_remote if raw_remote is not None else (request.remote_addr or "")).strip()
    return remote_addr, _is_loopback_ip(remote_addr)


def rate_limit(
    max_requests: int = 60,
    window_seconds: int = 60,
    skip_polling_duplicates: bool = False,
):
    """Simple IP-based rate limiting decorator (designed for personal use).

    Uses an in-memory store (not persisted). Rate limits reset on server restart.
    For production deployments, replace with a persistent backend (Redis, etc.).

    When *skip_polling_duplicates* is True, requests that carry a
    ``request_token`` already seen within the rate-limit window are not
    counted against the limit, up to ``_RATE_LIMIT_MAX_TOKEN_POLLS`` polls per
    token per window. This lets clients poll an in-flight async job
    (fetching:True) without consuming quota for every poll attempt, while a
    brand-new token (a genuinely new request) is still counted normally -- and
    a reused token can never bypass the quota indefinitely (after the budget
    is spent, same-token requests are counted normally and hit the 429).
    """

    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            # R3: When X-MNS-Admin-Token is present and valid, rate-limit by
            # token fingerprint (X-MNS-Admin-Token // remote) instead of IP, so
            # an unauthenticated flood from the same egress IP cannot starve a
            # legitimate remote admin. The ip identity is still computed for
            # logging / local-multiplier decisions.
            base_remote, base_is_local = _rate_limit_identity()
            # Fingerprint only when the presented token matches MNS_ADMIN_TOKEN;
            # otherwise fall back to IP bucketing (prevents arbitrary header
            # value from becoming an unlimited identity).
            _presented = (request.headers.get("X-MNS-Admin-Token") or "").strip()
            _configured = os.environ.get("MNS_ADMIN_TOKEN", "").strip()
            _authed = bool(
                _presented and _configured and len(_configured) >= 32 and len(_presented) >= 8
            )
            # Full constant-time compare only when candidate lengths plausible
            if _authed:
                import secrets as _secrets

                try:
                    _authed = _secrets.compare_digest(_presented, _configured)
                except Exception:
                    _authed = False
            if _authed:
                digest = hashlib.sha256(_configured.encode("utf-8")).hexdigest()[:16]
                rate_remote = f"adm:{digest}"
                rate_is_local = False
            else:
                rate_remote, rate_is_local = base_remote, base_is_local
            remote_addr, is_local = rate_remote, rate_is_local
            disable_local_limit = os.environ.get(
                "MNS_DISABLE_LOCAL_RATE_LIMIT", ""
            ).strip().lower() in ("1", "true", "yes")
            global _rate_limit_last_cleanup

            current_time = time.monotonic()

            # Polling duplicates: a repeated request_token means the client is
            # re-checking the same in-flight async job, not issuing a new call.
            # Count it only once per token so the quota is not exhausted by
            # the client's polling loop (see /api/chat and /api/analyze-v2).
            #
            # The skip is BOUNDED per token: at most _RATE_LIMIT_MAX_TOKEN_POLLS
            # requests per window may bypass the quota for one token. Without
            # the cap, a single reused token would bypass the endpoint quota
            # entirely -- once the short-lived result cache expires, every poll
            # starts a NEW upstream (paid) AI job, so one token could burn
            # unlimited Mistral quota. After the budget is exhausted, further
            # requests with the token are counted normally and hit the 429.
            skip_handler = False
            if skip_polling_duplicates:
                try:
                    raw_token = (request.get_json(silent=True) or {}).get("request_token")
                except Exception:
                    raw_token = None
                if isinstance(raw_token, str) and raw_token.strip():
                    stripped = raw_token.strip()
                    # R2: bound token length — over-long tokens are treated as
                    # absent (so they count normally and cannot blow up the key
                    # space). 40-char hex tokens are the normal case.
                    if len(stripped) > _RATE_LIMIT_MAX_REQUEST_TOKEN_LEN:
                        logger.debug(
                            "Over-long request_token (%s chars) ignored for polling skip",
                            len(stripped),
                        )
                    else:
                        token = stripped
                        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
                        endpoint_name = str(request.endpoint or getattr(f, "__name__", "default"))
                        token_key = f"{remote_addr}:{endpoint_name}:token:{token_hash}"
                        distinct_key = f"{remote_addr}:{endpoint_name}:distinct"
                        with _rate_limit_lock:
                            seen = _rate_limit_store.get(token_key)
                            if seen is not None:
                                inflight_alive = _is_polling_token_inflight(token)
                                if inflight_alive and len(seen) < _RATE_LIMIT_MAX_TOKEN_POLLS:
                                    seen.append(current_time)
                                    skip_handler = True
                            else:
                                # New distinct token for this client/endpoint pair:
                                # enforce a budget so a spray of random tokens
                                # cannot fill the global store and evict legit
                                # endpoint buckets.
                                count = _rate_limit_distinct_token_counts.get(distinct_key, 0)
                                if count >= _RATE_LIMIT_MAX_DISTINCT_TOKENS:
                                    logger.debug(
                                        "Distinct token budget exceeded for %s (%s)",
                                        distinct_key,
                                        count,
                                    )
                                else:
                                    _rate_limit_store[token_key] = [current_time]
                                    _rate_limit_window_by_key[token_key] = window_seconds
                                    _rate_limit_distinct_token_counts[distinct_key] = count + 1
            if skip_handler:
                return f(*args, **kwargs)
            endpoint = str(request.endpoint or getattr(f, "__name__", "default"))
            effective_max_requests, effective_window_seconds = _resolve_rate_limit(
                endpoint, max_requests, window_seconds
            )
            if is_local:
                # Apply local multiplier (default 10x) for loopback requests to allow smooth personal UI
                # usage while preventing infinite-loop resource exhaustion / local DoS.
                local_mult = _env_int("MNS_LOCAL_RATE_LIMIT_MULTIPLE", 10, 1, 1000)
                effective_max_requests *= local_mult
                if disable_local_limit:
                    # R3: MNS_DISABLE_LOCAL_RATE_LIMIT previously skipped the
                    # limiter entirely, so a runaway local script could exhaust
                    # upstream API quota or saturate the worker threads with no
                    # backstop. Raise the ceiling to a high but FINITE value
                    # instead: normal interactive use never approaches it, while
                    # a runaway loop still gets a 429 eventually.
                    effective_max_requests = max(
                        effective_max_requests,
                        _env_int("MNS_LOCAL_RATE_LIMIT_CEILING", 600, 1, 100000),
                    )
            key = f"{remote_addr}:{endpoint}"

            with _rate_limit_lock:
                _rate_limit_window_by_key[key] = effective_window_seconds
                if current_time - _rate_limit_last_cleanup > _RATE_LIMIT_CLEANUP_INTERVAL:
                    _cleanup_rate_limit_store()
                    _rate_limit_last_cleanup = current_time

                if key not in _rate_limit_store:
                    # Proactive eviction if store is full to prevent unbounded memory growth under flood
                    if len(_rate_limit_store) >= _RATE_LIMIT_MAX_ENTRIES:
                        sorted_keys = sorted(
                            _rate_limit_store.keys(),
                            key=lambda k: _rate_limit_store[k][0] if _rate_limit_store[k] else 0.0,
                        )
                        excess = len(_rate_limit_store) - _RATE_LIMIT_MAX_ENTRIES + 1
                        for old_key in sorted_keys[:excess]:
                            _rate_limit_store.pop(old_key, None)
                            _rate_limit_window_by_key.pop(old_key, None)
                        _rate_limit_distinct_token_counts.clear()
                        for k2 in _rate_limit_store:
                            if ":token:" in k2:
                                cp = k2.rsplit(":token:", 1)[0]
                                _rate_limit_distinct_token_counts[cp] = (
                                    _rate_limit_distinct_token_counts.get(cp, 0) + 1
                                )
                    _rate_limit_store[key] = []

                _rate_limit_store[key] = [
                    t for t in _rate_limit_store[key] if current_time - t < effective_window_seconds
                ]

                if len(_rate_limit_store[key]) >= effective_max_requests:
                    retry_after = max(
                        0,
                        int(effective_window_seconds - (current_time - _rate_limit_store[key][0])),
                    )
                    resp, status_code = error_response(
                        ErrorCode.API_RATE_LIMITED,
                        status_code=429,
                        details={"retry_after": retry_after},
                    )
                    resp.headers["Retry-After"] = str(retry_after)
                    return resp, status_code

                _rate_limit_store[key].append(current_time)

            return f(*args, **kwargs)

        return wrapper

    return decorator


# ============================================================
# API Key Extraction
# ============================================================
def extract_api_key(req: Any) -> str:
    """Extract the Mistral API key from secure server-side storage.

    Always uses the server-stored key. Client-provided keys via the
    Authorization header are only accepted when **all** of the following hold:
      * ``TESTING`` is True,
      * the app is not in production (``MNS_PROD`` is unset), and
      * the opt-in environment variable ``MNS_ALLOW_CLIENT_API_KEY=1`` is set.

    The extra ``MNS_ALLOW_CLIENT_API_KEY`` gate prevents a TESTING mode
    accidentally left enabled in a shared / semi-public environment from
    letting any caller supply (and therefore extract via error messages or
    side effects) their own key.
    """
    from flask import current_app

    stored: str = _as_text(get_mistral_api_key())
    if stored:
        current_app.logger.debug(
            "Mistral key source=stored fp=%s id=%s",
            _token_fingerprint(stored),
            getattr(g, "request_id", "-"),
        )
        return stored

    if (
        current_app.config.get("TESTING")
        and not _is_production_env()
        and os.environ.get("MNS_ALLOW_CLIENT_API_KEY", "").strip().lower() in ("1", "true", "yes")
    ):
        auth_header = str(req.headers.get("Authorization", ""))
        if auth_header.startswith("Bearer "):
            test_key: str = auth_header.removeprefix("Bearer ").strip()
            if test_key:
                current_app.logger.debug(
                    "Mistral key source=test_header id=%s",
                    getattr(g, "request_id", "-"),
                )
                return test_key

    current_app.logger.warning(
        "Mistral key missing in secure storage id=%s", getattr(g, "request_id", "-")
    )
    return ""


def extract_langsearch_api_key(req: Any) -> str:
    """Extract LangSearch API key from stored config. Always uses secure storage.

    Client-provided keys require the same MNS_ALLOW_CLIENT_API_KEY opt-in as
    extract_api_key; see that function for the rationale.
    """
    from flask import current_app

    stored: str = _as_text(get_langsearch_api_key())
    if stored:
        current_app.logger.debug(
            "LangSearch key source=stored fp=%s id=%s",
            _token_fingerprint(stored),
            getattr(g, "request_id", "-"),
        )
        return stored

    if current_app.config.get("TESTING") and os.environ.get(
        "MNS_ALLOW_CLIENT_API_KEY", ""
    ).strip().lower() in ("1", "true", "yes"):
        hdr: str = str(req.headers.get("X-LangSearch-Key", ""))
        if hdr:
            return hdr
    return ""


def extract_tavily_api_key(req: Any) -> str:
    """Extract Tavily API key from stored config. Always uses secure storage.

    Client-provided keys require the same MNS_ALLOW_CLIENT_API_KEY opt-in as
    extract_api_key; see that function for the rationale.
    """
    from flask import current_app

    stored: str = _as_text(get_tavily_api_key())
    if stored:
        current_app.logger.debug(
            "Tavily key source=stored fp=%s id=%s",
            _token_fingerprint(stored),
            getattr(g, "request_id", "-"),
        )
        return stored

    if current_app.config.get("TESTING") and os.environ.get(
        "MNS_ALLOW_CLIENT_API_KEY", ""
    ).strip().lower() in ("1", "true", "yes"):
        hdr: str = str(req.headers.get("X-Tavily-Key", ""))
        if hdr:
            return hdr
    return ""


# ============================================================
# Stock Cache Helpers
# ============================================================

# Circuit breaker cleanup state (time-based, not per-request)
_circuit_cleanup_ts: float = 0.0
_CIRCUIT_CLEANUP_INTERVAL: int = 120  # seconds


def cleanup_history_circuit_state(now_ts: float | None = None, stale_after_sec: int = 600) -> None:
    """Remove expired circuit breaker states to free up memory.

    Uses a time-based guard to avoid running cleanup on every request.
    """
    global _circuit_cleanup_ts
    now_value = time.time() if now_ts is None else now_ts
    if now_ts is None:
        if now_value - _circuit_cleanup_ts < _CIRCUIT_CLEANUP_INTERVAL:
            return
        _circuit_cleanup_ts = now_value

    with app_state.market.history_circuit_lock:
        stale_symbols = []
        for sym, state in list(app_state.market.history_circuit_state.items()):
            if state is None:
                stale_symbols.append(sym)
                continue
            open_until = state.open_until or 0.0
            status = state.status or "CLOSED"
            is_stale_open = (
                status == "OPEN"
                and open_until > 0.0
                and open_until <= (now_value - stale_after_sec)
            )
            is_clean_closed = status == "CLOSED" and state.timeout_streak == 0
            if is_stale_open or is_clean_closed:
                stale_symbols.append(sym)
        for sym in stale_symbols:
            app_state.market.history_circuit_state.pop(sym, None)


def _stock_display_name(symbol: str, market: str) -> str:
    container = _get_stock_container(market)
    if container and symbol in container:
        value = container[symbol]
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return str(normalize_text(value.get("name"), symbol))
    return _default_stock_names(market).get(symbol, symbol)


def _parse_stock_request(
    data: dict, require_name: bool = False, default_market: str = "us"
) -> tuple[dict | None, tuple[Any, int] | None]:
    """Parse and validate common stock mutation request fields."""
    raw_symbol = normalize_symbol(data.get("symbol"))
    market = normalize_market(data.get("market"), default=default_market)
    symbol = normalize_symbol_for_market(raw_symbol, market) if market else ""
    name = normalize_text(data.get("name"))

    if not symbol:
        return None, error_response(
            ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["symbol"]}
        )
    if not market:
        return None, error_response(ErrorCode.INVALID_MARKET)
    if require_name and not name:
        return None, error_response(ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["name"]})
    if len(name) > MAX_STOCK_NAME_LENGTH:
        return None, error_response(
            ErrorCode.UNSAFE_INPUT,
            details={"reason": f"nameは{MAX_STOCK_NAME_LENGTH}文字以下である必要があります"},
        )
    if not is_valid_symbol(symbol):
        return None, error_response(ErrorCode.INVALID_SYMBOL)

    return {
        "raw_symbol": raw_symbol,
        "name": name,
        "market": market,
        "symbol": symbol,
    }, None


def invalidate_stock_caches(symbol: str) -> None:
    """Invalidate all cache entries related to a specific symbol."""
    clear_cache_prefix("stocks")
    clear_cache_prefix(f"hist_{symbol}")
    clear_cache_prefix(f"research_context_{symbol}_")
    clear_yfinance_short_cache_prefix(f"info_short_{symbol}")
    clear_yfinance_short_cache_prefix(f"history_short_{symbol}_")
    clear_yfinance_short_cache_prefix(f"fastinfo_{symbol}")
    # Also invalidate disk caches for this symbol
    try:
        app_state.stock_disk_cache.delete_prefix(f"hist_{symbol}")
        app_state.stock_disk_cache.delete_prefix(f"hist_df_{symbol}")
        app_state.payload_disk_cache.delete_prefix(f"payload_{symbol}")
    except Exception as exc:
        logger.debug("Cache invalidation partially failed for %s: %s", symbol, exc)


def invalidate_single_stock_cache(symbol: str) -> None:
    """Invalidate only the caches for a single symbol (preserves stocks list)."""
    clear_cache_prefix(f"hist_{symbol}")
    clear_cache_prefix(f"info_{symbol}")
    clear_cache_prefix(f"research_context_{symbol}_")
    clear_yfinance_short_cache_prefix(f"info_short_{symbol}")
    clear_yfinance_short_cache_prefix(f"history_short_{symbol}_")
    clear_yfinance_short_cache_prefix(f"fastinfo_{symbol}")
    try:
        app_state.stock_disk_cache.delete_prefix(f"hist_df_{symbol}")
    except Exception as exc:
        logger.debug("Cache invalidation (single) partially failed for %s: %s", symbol, exc)


def ensure_stock_placeholder_in_caches(symbol, name, market):
    """Ensure a placeholder entry exists in the stock caches for a new symbol."""
    with app_state.cache.sse_data_lock:
        for cache in (
            app_state.market.current_stocks_cache,
            app_state.market.target_stocks_cache,
        ):
            if market not in cache:
                cache[market] = []
            target_list = cache[market]
            if not any(s.get("symbol") == symbol for s in target_list):
                target_list.append(
                    {
                        "symbol": symbol,
                        "name": name,
                        "market": market,
                        "price": "--",
                        "change": "--",
                        "change_percent": "--",
                        "chart_data": [],
                        "shares": 0,
                        "avg_price": 0,
                    }
                )


def remove_stock_from_caches(symbol, market):
    """Remove a symbol from both in-memory and disk caches."""
    with app_state.cache.sse_data_lock:
        for cache in (
            app_state.market.current_stocks_cache,
            app_state.market.target_stocks_cache,
        ):
            if market not in cache:
                cache[market] = []
            cache[market] = [s for s in cache[market] if s.get("symbol") != symbol]
    # Also remove from disk caches
    try:
        app_state.stock_disk_cache.delete_prefix(f"hist_{symbol}")
        app_state.payload_disk_cache.delete(f"payload_{symbol}_{market}")
    except Exception as exc:  # nosec B110
        logger.debug(
            "Disk cache cleanup failed during remove_stock_from_caches for %s: %s", symbol, exc
        )


# ============================================================
# Text / Mistral Helpers
# ============================================================
def _extract_text_from_mistral_content(content: Any) -> str:
    """Extract plain text from Mistral API multi-format content responses."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        texts: list[str] = []
        for chunk in content:
            if isinstance(chunk, dict):
                chunk_type = chunk.get("type")
                if chunk_type == "text":
                    text_val = chunk.get("text")
                    if isinstance(text_val, str) and text_val.strip():
                        texts.append(text_val.strip())
            elif (
                hasattr(chunk, "type")
                and chunk.type == "text"
                and hasattr(chunk, "text")
                and isinstance(chunk.text, str)
                and chunk.text.strip()
            ):
                texts.append(chunk.text.strip())
        return "\n".join(texts) if texts else ""
    return ""


def _seconds_until(timestamp: float | None) -> float:
    """Return seconds until a UNIX timestamp, clamped at zero."""
    return round(max(0.0, (timestamp or 0.0) - time.time()), 2)


# ============================================================
# Background Execution Helpers
# ============================================================
MAX_EXECUTOR_QUEUE_SIZE = 16


def _submit_in_app_context(executor, job_fn, app=None):
    """Submit job_fn to executor, ensuring it runs inside the current app context.

    Args:
        executor: The thread pool executor to submit the job to.
        job_fn: The callable to execute within the app context.
        app: Optional Flask application instance. If not provided, falls back
             to ``current_app._get_current_object()``, which is always
             available since this function is called from within route handlers.
    """
    if app is None:
        _proxy: Any = current_app
        app = cast(Flask, _proxy._get_current_object())

    work_queue = getattr(executor, "_work_queue", None)
    if work_queue is not None:
        try:
            if work_queue.qsize() >= MAX_EXECUTOR_QUEUE_SIZE:
                logger.warning("Executor work queue saturated (qsize=%d)", work_queue.qsize())
                import queue

                raise queue.Full("Executor work queue capacity reached")
        except (AttributeError, NotImplementedError):
            pass

    def _runner():
        with app.app_context():
            try:
                job_fn()
            finally:
                try:
                    from app_state import app_state

                    if hasattr(app_state, "ai") and hasattr(app_state.ai, "chat_history"):
                        app_state.ai.chat_history.close()
                except Exception as close_exc:
                    logger.warning("Failed to close chat DB in background thread: %s", close_exc)

    executor.submit(_runner)

