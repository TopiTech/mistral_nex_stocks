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
from collections.abc import Mapping
from functools import wraps
from typing import Any, cast

from flask import Flask, current_app, g, request
from pydantic import ValidationError

from app_state import app_state
from constants import (
    CHAT_MAX_MSG_LENGTH,
    LANGSEARCH_API_KEY_MIN_LENGTH,
    MAX_AI_PORTFOLIO_ITEMS,
    MAX_STOCK_NAME_LENGTH,
    MISTRAL_API_KEY_MIN_LENGTH,
    PORTFOLIO_AVG_PRICE_MAX,
    PORTFOLIO_SHARES_MAX,
    PORTFOLIO_TOTAL_VALUE_MAX,
    TAVILY_API_KEY_MIN_LENGTH,
)
from credential_manager import get_langsearch_api_key, get_mistral_api_key, get_tavily_api_key
from error_codes import ErrorCode
from schemas.ai_portfolio import (
    AIPortfolioCopyToMyRequest,
    AIPortfolioDeleteRequest,
    AIPortfolioGenerateRequest,
    AIPortfolioRebalanceRequest,
    AIPortfolioSaveRequest,
)
from schemas.analysis import (
    AIAnalyzeChartImageRequest,
    AIAnalyzeV2Request,
    AIChatRequest,
    AITechnicalLinesRequest,
)
from schemas.config import CredentialsSaveRequest
from schemas.stocks import (
    PortfolioUpdateRequest,
    StockAddExtRequest,
    StockAddRequest,
    StockDeleteRequest,
)
from utils.caching import clear_cache_key, clear_cache_prefix
from utils.env_helpers import _env_bool as _route_env_bool
from utils.env_helpers import _env_int, _is_production_env
from utils.networking import _is_loopback_ip, check_admin_token
from utils.normalization import (
    normalize_symbol,
    normalize_text,
)
from utils.stock_payload import (
    _default_stock_names,
    _get_stock_container,
    clear_yfinance_short_cache_key,
    clear_yfinance_short_cache_prefix,
    error_response,
)
from utils.text_utils import _parse_json_request, _token_fingerprint

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
                    analyze_fetch_lock,
                    analyze_result_cache,
                    chat_fetch_inflight,
                    chat_fetch_lock,
                    chat_result_cache,
                )

                with chat_fetch_lock:
                    for k in inflight_keys:
                        if k in chat_fetch_inflight:
                            return True
                    for stored_key in list(chat_fetch_inflight.keys()):
                        if stored_key.endswith(f":{token}"):
                            return True
                    for k in inflight_keys:
                        if k in chat_result_cache:
                            return False
                    for cached_key in list(chat_result_cache.keys()):
                        if cached_key.endswith(f":{token}"):
                            return False

                with analyze_fetch_lock:
                    for k in inflight_keys:
                        if k in analyze_fetch_inflight:
                            return True
                    for stored_key in list(analyze_fetch_inflight.keys()):
                        if stored_key.endswith(f":{token}"):
                            return True
                    for k in inflight_keys:
                        if k in analyze_result_cache:
                            return False
                    for cached_key in list(analyze_result_cache.keys()):
                        if cached_key.endswith(f":{token}"):
                            return False
            except Exception:
                pass
        # Check news inflight (token alone, no conversation scope)
        try:
            from routes.api_analysis import news_fetch_inflight, news_fetch_lock

            with news_fetch_lock:
                if token in news_fetch_inflight:
                    return True
        except Exception:
            pass
    except Exception:
        return True
    # Inside a request without conversation scope and no cache hit:
    # conservatively allow bounded skip (the per-token cap of 120 still
    # prevents unbounded bypass; returning False here would break
    # legitimate polling that has not yet established a chat/analyze
    # conversation scope — e.g. first-poll after token registration).
    return True


def _consume_polling_duplicate_locked(token_key: str, token: str, current_time: float) -> bool:
    """Record one bounded polling duplicate while the rate-limit lock is held."""
    seen = _rate_limit_store.get(token_key)
    if seen is None:
        return False
    if _is_polling_token_inflight(token) and len(seen) < _RATE_LIMIT_MAX_TOKEN_POLLS:
        seen.append(current_time)
        return True
    return False


def _trim_polling_token_locked(
    token_key: str,
    distinct_key: str,
    current_time: float,
    fallback_window_seconds: int,
) -> bool:
    """Expire a polling-token bucket and return whether it remains live.

    Polling buckets bypass the endpoint quota, so they must be pruned before
    every bypass decision rather than waiting for the periodic global cleanup.
    The caller must hold ``_rate_limit_lock``.
    """
    seen = _rate_limit_store.get(token_key)
    if seen is None:
        return False

    token_window = max(1, _rate_limit_window_by_key.get(token_key, fallback_window_seconds))
    recent = [timestamp for timestamp in seen if current_time - timestamp < token_window]
    if recent:
        _rate_limit_store[token_key] = recent
        return True

    _rate_limit_store.pop(token_key, None)
    _rate_limit_window_by_key.pop(token_key, None)
    count = _rate_limit_distinct_token_counts.get(distinct_key, 0)
    if count <= 1:
        _rate_limit_distinct_token_counts.pop(distinct_key, None)
    else:
        _rate_limit_distinct_token_counts[distinct_key] = count - 1
    return False


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
    allow_remote = _route_env_bool("MNS_ALLOW_REMOTE_API")
    proxied = _route_env_bool("MNS_PROXY_FIX")
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
            if request.method == "OPTIONS":
                return f(*args, **kwargs)

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
                _authed = check_admin_token(_presented)
            if _authed:
                digest = hashlib.sha256(_configured.encode("utf-8")).hexdigest()[:16]
                rate_remote = f"adm:{digest}"
                rate_is_local = False
            else:
                rate_remote, rate_is_local = base_remote, base_is_local
            remote_addr, is_local = rate_remote, rate_is_local
            disable_local_limit = _route_env_bool("MNS_DISABLE_LOCAL_RATE_LIMIT")
            global _rate_limit_last_cleanup

            endpoint = str(request.endpoint or getattr(f, "__name__", "default"))
            effective_max_requests, effective_window_seconds = _resolve_rate_limit(
                endpoint, max_requests, window_seconds
            )
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
            # A new token is registered only after its first request has passed
            # the endpoint quota below. Registering it before that check would
            # let a rejected token become a later polling-skip identity.
            pending_token_registration: tuple[str, str, str] | None = None
            if skip_polling_duplicates:
                try:
                    # This wrapper runs before the view's own parser. Reuse the
                    # bounded parser here so a chunked request cannot be fully
                    # decoded under Flask's broader application-wide limit
                    # merely to inspect its polling token.
                    raw_token = (_parse_json_request() or {}).get("request_token")
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
                        token_key = f"{remote_addr}:{endpoint}:token:{token_hash}"
                        distinct_key = f"{remote_addr}:{endpoint}:distinct"
                        with _rate_limit_lock:
                            if _trim_polling_token_locked(
                                token_key,
                                distinct_key,
                                current_time,
                                effective_window_seconds,
                            ):
                                if _consume_polling_duplicate_locked(
                                    token_key, token, current_time
                                ):
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
                                    pending_token_registration = (token_key, distinct_key, token)
            if skip_handler:
                return f(*args, **kwargs)
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
                if pending_token_registration is not None:
                    token_key, distinct_key, token = pending_token_registration
                    # Another simultaneous first request may have accepted and
                    # registered the same token while this request waited for
                    # the endpoint lock. Treat it as the bounded duplicate it
                    # now is, rather than charging it as a new request.
                    if _trim_polling_token_locked(
                        token_key,
                        distinct_key,
                        current_time,
                        effective_window_seconds,
                    ):
                        skip_handler = _consume_polling_duplicate_locked(
                            token_key, token, current_time
                        )

                if not skip_handler:
                    _rate_limit_window_by_key[key] = effective_window_seconds
                    if current_time - _rate_limit_last_cleanup > _RATE_LIMIT_CLEANUP_INTERVAL:
                        _cleanup_rate_limit_store()
                        _rate_limit_last_cleanup = current_time

                    if key not in _rate_limit_store:
                        # Proactive eviction if store is full to prevent unbounded memory growth under flood
                        if len(_rate_limit_store) >= _RATE_LIMIT_MAX_ENTRIES:
                            sorted_keys = sorted(
                                _rate_limit_store.keys(),
                                key=lambda k: (
                                    _rate_limit_store[k][0] if _rate_limit_store[k] else 0.0
                                ),
                            )
                            excess = len(_rate_limit_store) - _RATE_LIMIT_MAX_ENTRIES + 1
                            for old_key in sorted_keys[:excess]:
                                _rate_limit_store.pop(old_key, None)
                                _rate_limit_window_by_key.pop(old_key, None)
                            _rate_limit_distinct_token_counts.clear()
                            for k2 in _rate_limit_store:
                                if ":token:" in k2:
                                    client_prefix = k2.rsplit(":token:", 1)[0]
                                    client_prefix = f"{client_prefix}:distinct"
                                    _rate_limit_distinct_token_counts[client_prefix] = (
                                        _rate_limit_distinct_token_counts.get(client_prefix, 0) + 1
                                    )
                        _rate_limit_store[key] = []

                    _rate_limit_store[key] = [
                        t
                        for t in _rate_limit_store[key]
                        if current_time - t < effective_window_seconds
                    ]

                    if len(_rate_limit_store[key]) >= effective_max_requests:
                        retry_after = max(
                            0,
                            int(
                                effective_window_seconds
                                - (current_time - _rate_limit_store[key][0])
                            ),
                        )
                        resp, status_code = error_response(
                            ErrorCode.API_RATE_LIMITED,
                            status_code=429,
                            details={"retry_after": retry_after},
                        )
                        resp.headers["Retry-After"] = str(retry_after)
                        return resp, status_code

                    _rate_limit_store[key].append(current_time)

                    if pending_token_registration is not None:
                        token_key, distinct_key, _token = pending_token_registration
                        # Under a saturated store, keep the accepted endpoint
                        # bucket rather than evicting it for an optional
                        # polling cache entry.
                        if (
                            token_key not in _rate_limit_store
                            and len(_rate_limit_store) < _RATE_LIMIT_MAX_ENTRIES
                        ):
                            count = _rate_limit_distinct_token_counts.get(distinct_key, 0)
                            if count < _RATE_LIMIT_MAX_DISTINCT_TOKENS:
                                _rate_limit_store[token_key] = [current_time]
                                # Keep the polling bucket aligned with the effective
                                # endpoint window, including MNS_RATE_LIMIT_<ENDPOINT>_WINDOW.
                                # Using the decorator argument here would let a
                                # token-poll bypass outlive an administrator's
                                # endpoint-specific override.
                                _rate_limit_window_by_key[token_key] = effective_window_seconds
                                _rate_limit_distinct_token_counts[distinct_key] = count + 1

            return f(*args, **kwargs)

        return wrapper

    return decorator


# ============================================================
# API Key Extraction
# ============================================================
def _safe_app_context() -> tuple[logging.Logger, str, bool]:
    app_logger = logger
    req_id = "-"
    is_testing = False
    try:
        from flask import current_app

        is_testing = bool(current_app.config.get("TESTING", False))
        app_logger = current_app.logger
        req_id = getattr(g, "request_id", "-")
    except (RuntimeError, AttributeError):
        pass
    except Exception:
        pass
    return app_logger, req_id, is_testing


def extract_api_key(req: Any) -> str:
    """Extract Mistral or OpenAI API key based on the configured provider.

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
    from credential_manager import (
        PREVIEW_API_KEY,
        get_openai_api_key,
        is_local_openai_base_url,
        is_openai_provider,
        is_preview_mode,
    )

    app_log, req_id, is_testing = _safe_app_context()

    if is_openai_provider():
        stored_oa: str = _as_text(get_openai_api_key())
        if stored_oa:
            app_log.debug(
                "OpenAI key source=stored fp=%s id=%s",
                _token_fingerprint(stored_oa),
                req_id,
            )
            return stored_oa
        if is_local_openai_base_url():
            return "ollama-local"
        if is_preview_mode():
            return PREVIEW_API_KEY

        if (
            is_testing
            and not _is_production_env()
            and _route_env_bool("MNS_ALLOW_CLIENT_API_KEY")
        ):
            auth_header = str(req.headers.get("Authorization", ""))
            if auth_header.startswith("Bearer "):
                test_key_oa: str = auth_header.removeprefix("Bearer ").strip()
                if test_key_oa:
                    app_log.debug(
                        "OpenAI key source=test_header id=%s",
                        req_id,
                    )
                    return test_key_oa

        app_log.warning(
            "OpenAI key missing in secure storage id=%s", req_id
        )
        return ""

    stored: str = _as_text(get_mistral_api_key())
    if stored:
        app_log.debug(
            "Mistral key source=stored fp=%s id=%s",
            _token_fingerprint(stored),
            req_id,
        )
        return stored

    if (
        is_testing
        and not _is_production_env()
        and _route_env_bool("MNS_ALLOW_CLIENT_API_KEY")
    ):
        auth_header = str(req.headers.get("Authorization", ""))
        if auth_header.startswith("Bearer "):
            test_key: str = auth_header.removeprefix("Bearer ").strip()
            if test_key:
                app_log.debug(
                    "Mistral key source=test_header id=%s",
                    req_id,
                )
                return test_key

    app_log.warning(
        "Mistral key missing in secure storage id=%s", req_id
    )
    return ""


def extract_langsearch_api_key(req: Any) -> str:
    """Extract LangSearch API key from stored config. Always uses secure storage.

    Client-provided keys require the same MNS_ALLOW_CLIENT_API_KEY opt-in as
    extract_api_key; see that function for the rationale.
    """
    app_log, req_id, is_testing = _safe_app_context()

    stored: str = _as_text(get_langsearch_api_key())
    if stored:
        app_log.debug(
            "LangSearch key source=stored fp=%s id=%s",
            _token_fingerprint(stored),
            req_id,
        )
        return stored

    if (
        is_testing
        and not _is_production_env()
        and _route_env_bool("MNS_ALLOW_CLIENT_API_KEY")
    ):
        hdr: str = str(req.headers.get("X-LangSearch-Key", ""))
        if hdr:
            return hdr
    return ""


def extract_tavily_api_key(req: Any) -> str:
    """Extract Tavily API key from stored config. Always uses secure storage.

    Client-provided keys require the same MNS_ALLOW_CLIENT_API_KEY opt-in as
    extract_api_key; see that function for the rationale.
    """
    app_log, req_id, is_testing = _safe_app_context()

    stored: str = _as_text(get_tavily_api_key())
    if stored:
        app_log.debug(
            "Tavily key source=stored fp=%s id=%s",
            _token_fingerprint(stored),
            req_id,
        )
        return stored

    if (
        is_testing
        and not _is_production_env()
        and _route_env_bool("MNS_ALLOW_CLIENT_API_KEY")
    ):
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


def _stock_mutation_error_from_validation(
    exc: ValidationError, *, require_name: bool
) -> tuple[Any, int]:
    """Map canonical stock-mutation schema failures to historic errors.

    The historic ``_parse_stock_request`` checked identity fields first, then
    required-field presence, and only then name length / symbol shape.
    Pydantic reports every failure at once in field order, so re-order before
    picking the single error the client sees.
    """
    errors = exc.errors()
    identity_errors = [
        candidate
        for candidate in errors
        if (
            candidate.get("loc")
            and str(candidate["loc"][0]) in ("symbol", "market")
        )
        or (
            candidate.get("type") == "value_error"
            and any(
                marker in str(candidate.get("msg", ""))
                for marker in ("invalid_market", "invalid_symbol", "field_required")
            )
        )
    ]
    name_required_errors = [
        candidate
        for candidate in errors
        if candidate.get("loc")
        and str(candidate["loc"][0]) == "name"
        and (
            candidate.get("type") == "missing"
            or "field_required_name" in str(candidate.get("msg", ""))
        )
    ]
    name_type_errors = [
        candidate
        for candidate in errors
        if candidate.get("loc")
        and str(candidate["loc"][0]) == "name"
        and "name_must_be_string" in str(candidate.get("msg", ""))
    ]
    err: Mapping[str, Any]
    if identity_errors:
        err = identity_errors[0]
    elif name_type_errors:
        err = name_type_errors[0]
    elif require_name and name_required_errors:
        err = name_required_errors[0]
    else:
        err = errors[0] if errors else {"type": "value_error", "msg": str(exc), "loc": ()}
    loc = err.get("loc") or ()
    field = str(loc[0]) if loc else ""
    message = str(err.get("msg", ""))
    err_type = str(err.get("type", ""))

    if "name_must_be_string" in message:
        return error_response(
            ErrorCode.INVALID_INPUT, details={"reason": "name must be a string"}
        )
    if "invalid_market" in message:
        return error_response(ErrorCode.INVALID_MARKET)
    if "invalid_symbol" in message:
        return error_response(ErrorCode.INVALID_SYMBOL)
    if field == "market":
        return error_response(ErrorCode.INVALID_MARKET)
    if field == "symbol":
        if err_type in ("missing", "string_too_short") or "field_required" in message:
            return error_response(
                ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["symbol"]}
            )
        return error_response(ErrorCode.INVALID_SYMBOL)
    if field == "name":
        if err_type == "missing" or "field_required_name" in message:
            return error_response(
                ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["name"]}
            )
        if "name_too_long" in message:
            return error_response(
                ErrorCode.UNSAFE_INPUT,
                details={"reason": f"nameは{MAX_STOCK_NAME_LENGTH}文字以下である必要があります"},
            )
        return error_response(
            ErrorCode.INVALID_INPUT, details={"reason": "name must be a string"}
        )
    return error_response(ErrorCode.MALFORMED_INPUT)


def _validated_stock_mutation_to_dict(validated: Any, raw_data: dict) -> dict:
    """Render the canonical model back to the historic parsed-dict shape."""
    raw_symbol_val = raw_data.get("symbol")
    raw_symbol = normalize_symbol(raw_symbol_val)
    name = getattr(validated, "name", None)
    if name is None and raw_data.get("name") is None:
        name = ""
    return {
        "raw_symbol": raw_symbol,
        "name": name if isinstance(name, str) else normalize_text(name),
        "market": getattr(validated, "market", ""),
        "symbol": getattr(validated, "symbol", ""),
    }


def _parse_stock_add_request(
    data: dict,
) -> tuple[dict | None, tuple[Any, int] | None]:
    """Validate an add body with the canonical ``StockAddRequest`` schema."""
    try:
        validated = StockAddRequest.model_validate(data)
    except ValidationError as exc:
        return None, _stock_mutation_error_from_validation(exc, require_name=True)
    return _validated_stock_mutation_to_dict(validated, data), None


def _parse_stock_delete_request(
    data: dict,
) -> tuple[dict | None, tuple[Any, int] | None]:
    """Validate a delete body with the canonical ``StockDeleteRequest`` schema."""
    try:
        validated = StockDeleteRequest.model_validate(data)
    except ValidationError as exc:
        return None, _stock_mutation_error_from_validation(exc, require_name=False)
    parsed = _validated_stock_mutation_to_dict(validated, data)
    parsed["name"] = ""
    return parsed, None


def _parse_stock_add_ext_request(
    data: dict,
) -> tuple[dict | None, tuple[Any, int] | None]:
    """Validate an extension add body with ``StockAddExtRequest``."""
    try:
        validated = StockAddExtRequest.model_validate(data)
    except ValidationError as exc:
        return None, _stock_mutation_error_from_validation(exc, require_name=False)
    parsed = _validated_stock_mutation_to_dict(validated, data)
    if parsed.get("name") is None:
        parsed["name"] = ""
    return parsed, None


def _parse_stock_request(
    data: dict, require_name: bool = False, default_market: str = "us"
) -> tuple[dict | None, tuple[Any, int] | None]:
    """Legacy entry point kept for backward compatibility.

    Dispatches to the canonical schema-backed parsers so ``schemas/stocks.py``
    stays the single rule owner; behavior is unchanged for the three historic
    call shapes (add / delete / add_ext).
    """
    if require_name and default_market == "":
        return _parse_stock_add_request(data)
    if not require_name and default_market == "":
        return _parse_stock_delete_request(data)
    return _parse_stock_add_ext_request(data)


def _portfolio_numeric_error(field: str, err: Mapping[str, Any]) -> str:
    """Render the exact message ``parse_non_negative_float`` used to return."""
    err_type = str(err.get("type", ""))
    ctx = err.get("ctx") or {}
    if err_type == "finite_number":
        return f"{field} must be finite"
    if err_type == "less_than_equal":
        # Pydantic reports integer bounds as floats; the old route rendered the
        # int constants directly, so keep that spelling for byte-identical text.
        if field == "shares":
            return f"shares must be <= {PORTFOLIO_SHARES_MAX}"
        if field == "avg_price":
            return f"avg_price must be <= {PORTFOLIO_AVG_PRICE_MAX}"
        return f"{field} must be <= {ctx.get('le')}"
    if err_type == "greater_than_equal":
        return f"{field} must be >= 0"
    if err_type == "greater_than":
        raw_input = err.get("input")
        try:
            if raw_input is not None and float(raw_input) < 0:
                return f"{field} must be >= 0"
        except (TypeError, ValueError):
            pass
        return "avg_fx_rateは正の数値である必要があります"
    return f"{field} must be a number"


def _portfolio_error_from_validation(exc: ValidationError) -> tuple[Any, int]:
    """Map canonical-schema failures back to the endpoint's historic errors.

    The old route validated identity fields first, then required-field
    presence, and only then parsed numbers.  Pydantic reports every error at
    once in field order, so re-order before picking the single error the
    client sees (``shares="abc"`` with a missing ``avg_price`` must still say
    ``avg_price`` is required).
    """
    errors = exc.errors()
    identity_errors = [
        candidate
        for candidate in errors
        if (
            candidate.get("loc")
            and str(candidate["loc"][0]) in ("symbol", "market")
        )
        or (
            candidate.get("type") == "value_error"
            and any(
                marker in str(candidate.get("msg", ""))
                for marker in ("invalid_market", "invalid_symbol")
            )
        )
    ]
    required_numeric = [
        candidate
        for candidate in errors
        if candidate.get("loc")
        and str(candidate["loc"][0]) in ("shares", "avg_price")
        and (
            candidate.get("type") == "missing"
            or (
                candidate.get("type") == "value_error"
                and "field_required" in str(candidate.get("msg", ""))
            )
        )
    ]
    err: Mapping[str, Any]
    if identity_errors:
        err = identity_errors[0]
    elif required_numeric:
        err = required_numeric[0]
    else:
        err = errors[0] if errors else {"type": "value_error", "msg": str(exc), "loc": ()}
    loc = err.get("loc") or ()
    field = str(loc[0]) if loc else ""
    message = str(err.get("msg", ""))
    err_type = str(err.get("type", ""))

    if err_type == "value_error" and "invalid_market" in message:
        return error_response(ErrorCode.INVALID_MARKET)
    if err_type == "value_error" and "invalid_symbol" in message:
        return error_response(ErrorCode.INVALID_SYMBOL)
    if field == "market":
        return error_response(ErrorCode.INVALID_MARKET)
    if field == "symbol":
        if err_type in ("missing", "string_too_short") or (
            err_type == "value_error" and "field_required" in message
        ):
            return error_response(
                ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["symbol"]}
            )
        return error_response(ErrorCode.INVALID_SYMBOL)
    if field in ("shares", "avg_price") and (
        err_type == "missing" or (err_type == "value_error" and "field_required" in message)
    ):
        return error_response(ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": [field]})
    if "Portfolio total value" in message:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={
                "reason": f"ポートフォリオ総額は{PORTFOLIO_TOTAL_VALUE_MAX:,}以下である必要があります"
            },
        )
    return error_response(
        ErrorCode.INVALID_INPUT, details={"reason": _portfolio_numeric_error(field, err)}
    )


def _parse_portfolio_request(
    data: dict,
) -> tuple[PortfolioUpdateRequest | None, tuple[Any, int] | None]:
    """Validate a portfolio update body with the canonical schema."""
    try:
        return PortfolioUpdateRequest.model_validate(data), None
    except ValidationError as exc:
        return None, _portfolio_error_from_validation(exc)


def _chat_error_from_validation(exc: ValidationError) -> tuple[Any, int]:
    """Map canonical chat-schema failures back to the endpoint's errors.

    The old ``api_chat`` route checked fields sequentially: non-string
    identity/message types, then message length, then market presence, then
    the combined symbol/message presence, then symbol shape, and finally
    the operation token. Pydantic reports every failure at once in field
    order, so re-order before picking the single error the client sees.
    """
    errors = exc.errors()
    messages = [str(candidate.get("msg", "")) for candidate in errors]
    joined = " ".join(messages)
    if "message_must_be_string" in joined:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "message must be a string", "fields": ["message"]},
            status_code=400,
        )
    if "message_too_long" in joined or any(
        candidate.get("loc") == ("message",) and candidate.get("type") == "string_too_long"
        for candidate in errors
    ):
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": f"メッセージは{CHAT_MAX_MSG_LENGTH}文字以内で入力してください。"},
            status_code=400,
        )
    if "invalid_market" in joined or any(
        (candidate.get("loc") or (None,))[0] == "market" for candidate in errors
    ):
        return error_response(ErrorCode.INVALID_MARKET)
    symbol_blank = any(
        (candidate.get("loc") or (None,))[0] == "symbol"
        and (
            candidate.get("type") in ("missing", "string_too_short")
            or "field_required" in str(candidate.get("msg", ""))
        )
        for candidate in errors
    )
    message_blank = any(
        (candidate.get("loc") or (None,))[0] == "message"
        and (
            candidate.get("type") in ("missing", "string_too_short")
            or "field_required" in str(candidate.get("msg", ""))
        )
        for candidate in errors
    )
    if symbol_blank or message_blank:
        return error_response(
            ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["symbol", "message"]}
        )
    if "invalid_symbol" in joined or any(
        (candidate.get("loc") or (None,))[0] == "symbol" for candidate in errors
    ):
        return error_response(ErrorCode.INVALID_SYMBOL)
    if any((candidate.get("loc") or (None,))[0] == "request_token" for candidate in errors):
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "request_token must be a 16-128 character URL-safe token"},
            status_code=400,
        )
    return error_response(ErrorCode.MALFORMED_INPUT)


def _parse_chat_request(
    data: dict,
) -> tuple[AIChatRequest | None, tuple[Any, int] | None]:
    """Validate a chat body with the canonical ``AIChatRequest`` schema."""
    try:
        return AIChatRequest.model_validate(data), None
    except ValidationError as exc:
        return None, _chat_error_from_validation(exc)


def _analyze_v2_error_from_validation(exc: ValidationError) -> tuple[Any, int]:
    """Map canonical analyze-v2 failures back to the endpoint's errors.

    The old ``api_analyze_v2`` route checked fields sequentially: symbol and
    market types, name type/length, chart_data shape, price shape/range, then
    market presence, symbol presence/shape, and finally the operation token.
    The schema raises the first failure in that same order, so the adapter
    replays the single error the client used to see. Unreachable field-level
    fallbacks (``market``/``symbol`` locations) stay so a future schema edit
    cannot leak a raw validation message.
    """
    errors = exc.errors()
    joined = " ".join(str(candidate.get("msg", "")) for candidate in errors)
    locs = [(candidate.get("loc") or (None,))[0] for candidate in errors]
    if "symbol_not_string" in joined:
        return error_response(ErrorCode.INVALID_SYMBOL)
    if "market_not_string" in joined:
        return error_response(ErrorCode.INVALID_MARKET)
    if "name_must_be_string" in joined:
        return error_response(
            ErrorCode.INVALID_INPUT, details={"reason": "name must be a string"}
        )
    if "name_too_long" in joined:
        return error_response(
            ErrorCode.UNSAFE_INPUT,
            details={"reason": f"nameは{MAX_STOCK_NAME_LENGTH}文字以下である必要があります"},
        )
    if "chart_data_must_be_list" in joined:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "chart_data must be a list", "fields": ["chart_data"]},
            status_code=400,
        )
    if "chart_data_too_many" in joined:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={
                "reason": "chart_data has too many points (max 5000)",
                "fields": ["chart_data"],
            },
            status_code=400,
        )
    if "chart_data_entry_not_object" in joined:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={
                "reason": "chart_data entries must be objects",
                "fields": ["chart_data"],
            },
            status_code=400,
        )
    if "price_must_be_number" in joined:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "price must be a finite number", "fields": ["price"]},
            status_code=400,
        )
    if "price_not_finite" in joined or "price_negative" in joined:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={
                "reason": "price must be a finite non-negative number",
                "fields": ["price"],
            },
            status_code=400,
        )
    if "market_empty" in joined or "market" in locs:
        return error_response(ErrorCode.INVALID_MARKET)
    if "symbol_required" in joined:
        return error_response(
            ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["symbol"]}
        )
    if "symbol_bad_shape" in joined or "symbol" in locs:
        return error_response(ErrorCode.INVALID_SYMBOL)
    if "request_token" in locs:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "request_token must be a 16-128 character URL-safe token"},
            status_code=400,
        )
    return error_response(ErrorCode.MALFORMED_INPUT)


def _parse_analyze_v2_request(
    data: dict,
) -> tuple[AIAnalyzeV2Request | None, tuple[Any, int] | None]:
    """Validate an analyze-v2 body with ``AIAnalyzeV2Request``."""
    try:
        return AIAnalyzeV2Request.model_validate(data), None
    except ValidationError as exc:
        return None, _analyze_v2_error_from_validation(exc)


def _technical_lines_error_from_validation(exc: ValidationError) -> tuple[Any, int]:
    """Map canonical technical-lines failures back to the endpoint's errors.

    The old ``api_ai_technical_lines`` route checked fields sequentially:
    symbol presence/type, market type/presence, symbol presence/shape, then
    period and history_data shape. The schema raises the first failure in
    that same order, so the adapter replays the single error the client used
    to see. Unlike its neighbors, this endpoint renders a non-string symbol
    with a ``fields`` list and a bare ``INVALID_MARKET`` for a non-string
    market; both quirks are preserved here.
    """
    errors = exc.errors()
    joined = " ".join(str(candidate.get("msg", "")) for candidate in errors)
    locs = [(candidate.get("loc") or (None,))[0] for candidate in errors]
    if "symbol_required" in joined:
        return error_response(
            ErrorCode.MISSING_REQUIRED_FIELD,
            details={"fields": ["symbol"]},
            status_code=400,
        )
    if "symbol_not_string" in joined:
        return error_response(
            ErrorCode.INVALID_SYMBOL,
            details={"fields": ["symbol"]},
            status_code=400,
        )
    if "market_not_string" in joined or "market_empty" in joined or "market" in locs:
        return error_response(ErrorCode.INVALID_MARKET)
    if "symbol_bad_shape" in joined or "symbol" in locs:
        return error_response(ErrorCode.INVALID_SYMBOL)
    if "period_invalid" in joined or "period" in locs:
        return error_response(
            ErrorCode.INVALID_PERIOD,
            details={"reason": "periodは指定された期間のいずれかである必要があります"},
            status_code=400,
        )
    if "history_data_must_be_list" in joined:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "history_data must be a list", "fields": ["history_data"]},
            status_code=400,
        )
    if "history_data_too_many" in joined:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={
                "reason": "history_data has too many points (max 5000)",
                "fields": ["history_data"],
            },
            status_code=400,
        )
    if "history_data_entry_not_object" in joined:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={
                "reason": "history_data entries must be objects",
                "fields": ["history_data"],
            },
            status_code=400,
        )
    return error_response(ErrorCode.MALFORMED_INPUT)


def _parse_technical_lines_request(
    data: dict,
) -> tuple[AITechnicalLinesRequest | None, tuple[Any, int] | None]:
    """Validate a technical-lines body with ``AITechnicalLinesRequest``."""
    try:
        return AITechnicalLinesRequest.model_validate(data), None
    except ValidationError as exc:
        return None, _technical_lines_error_from_validation(exc)


def _chart_image_error_from_validation(exc: ValidationError) -> tuple[Any, int]:
    """Map canonical chart-image failures back to the endpoint's errors.

    The old ``api_analyze_chart_image`` route checked fields sequentially:
    image presence/shape/size, the Data-URI vs Base64 branch checks, symbol
    type/shape, market type/presence, then prompt type/length. The schema
    raises the first failure in that same order, so the adapter replays the
    single error the client used to see.
    """
    errors = exc.errors()
    joined = " ".join(str(candidate.get("msg", "")) for candidate in errors)
    locs = [(candidate.get("loc") or (None,))[0] for candidate in errors]
    if "image_required" in joined:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "image_data (Base64またはData URI) が必要です"},
            status_code=400,
        )
    if "image_too_large" in joined:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "image_dataが大きすぎます（上限約5MB）"},
            status_code=400,
        )
    if "image_bad_data_uri" in joined:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "image_dataはimage/*のData URI形式である必要があります"},
            status_code=400,
        )
    if "image_svg" in joined:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "SVG画像はサポートされていません"},
            status_code=400,
        )
    if "image_bad_mime" in joined:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "対応形式は png/jpeg/gif/webp のbase64 Data URIのみです"},
            status_code=400,
        )
    if "image_bad_b64" in joined:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "image_dataのData URIはbase64形式である必要があります"},
            status_code=400,
        )
    if "image_url" in joined:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={
                "reason": "URL形式のimage_dataは許可されていません。"
                "Data URIまたはBase64で送信してください"
            },
            status_code=400,
        )
    if "image_bad_scheme" in joined:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "image_dataのData URI形式が不正です"},
            status_code=400,
        )
    if "image_bad_base64" in joined:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "image_dataはBase64形式である必要があります"},
            status_code=400,
        )
    if "symbol_not_string" in joined:
        return error_response(ErrorCode.INVALID_SYMBOL)
    if "market_not_string" in joined or "market_empty" in joined or "market" in locs:
        return error_response(ErrorCode.INVALID_MARKET)
    if "symbol_bad_shape" in joined or "symbol" in locs:
        return error_response(ErrorCode.INVALID_SYMBOL)
    if "prompt_must_be_string" in joined:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "prompt must be a string", "fields": ["prompt"]},
            status_code=400,
        )
    if "prompt_too_long" in joined:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "promptは2000文字以内で入力してください", "fields": ["prompt"]},
            status_code=400,
        )
    return error_response(ErrorCode.MALFORMED_INPUT)


def _parse_chart_image_request(
    data: dict,
) -> tuple[AIAnalyzeChartImageRequest | None, tuple[Any, int] | None]:
    """Validate a chart-image body with ``AIAnalyzeChartImageRequest``."""
    try:
        return AIAnalyzeChartImageRequest.model_validate(data), None
    except ValidationError as exc:
        return None, _chart_image_error_from_validation(exc)


_CREDENTIALS_KEY_SPECS: dict[str, tuple[Any, int]] = {
    "mistral_api_key": (ErrorCode.INVALID_API_KEY, MISTRAL_API_KEY_MIN_LENGTH),
    "openai_api_key": (ErrorCode.UNSAFE_INPUT, 8),
    "langsearch_api_key": (ErrorCode.UNSAFE_INPUT, LANGSEARCH_API_KEY_MIN_LENGTH),
    "tavily_api_key": (ErrorCode.UNSAFE_INPUT, TAVILY_API_KEY_MIN_LENGTH),
    "alphavantage_api_key": (ErrorCode.UNSAFE_INPUT, 10),
}


def _credentials_error_from_validation(exc: ValidationError) -> tuple[Any, int]:
    """Map canonical credentials-save failures back to the endpoint's errors.

    The old ``api_credentials`` POST route checked fields sequentially:
    key types, key formats, prompt, API mode, agent id, preview flag, then
    (after the server-state-dependent Agents rules, which stay in the route)
    model shape, provider, base URL, and model names. The schema raises the
    first failure in that same order, so the adapter replays the single
    error the client used to see. Note the historic quirk: a malformed
    Mistral key reports ``INVALID_API_KEY`` while every other malformed key
    reports ``UNSAFE_INPUT``.
    """
    errors = exc.errors()
    markers = [
        str(candidate.get("msg", "")).split(",")[-1].strip() for candidate in errors
    ]
    # The schema raises the first failure only, so at most one marker is
    # present. Match markers exactly: several names share a suffix
    # (``openai_model_not_string`` contains ``model_not_string``).
    first = markers[0] if markers else ""
    kind, _, field = first.partition("|")
    if kind == "cred_not_string":
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"fields": [field], "reason": "APIキーは文字列で指定してください"},
            status_code=400,
        )
    if kind == "cred_bad_key":
        code, min_length = _CREDENTIALS_KEY_SPECS.get(field, (ErrorCode.UNSAFE_INPUT, 0))
        return error_response(
            code,
            details={"fields": [field], "min_length": min_length},
        )
    if first == "prompt_not_string":
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "custom_ai_promptは文字列で指定してください"},
            status_code=400,
        )
    if first == "prompt_too_long":
        return error_response(
            ErrorCode.UNSAFE_INPUT,
            details={"reason": "カスタムプロンプトは5000文字以内で入力してください"},
        )
    if first == "mode_invalid":
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "mistral_api_modeは chat または agents を指定してください"},
            status_code=400,
        )
    if first == "agent_not_string":
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "mistral_agent_idは文字列で指定してください"},
            status_code=400,
        )
    if first == "agent_bad_format":
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"fields": ["mistral_agent_id"], "reason": "Agent IDの形式が不正です"},
            status_code=400,
        )
    if first == "preview_not_bool":
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "preview_modeは真偽値で指定してください"},
            status_code=400,
        )
    if first == "model_not_string":
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "mistral_modelは有効な文字列で指定してください"},
            status_code=400,
        )
    if first == "provider_invalid":
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "ai_providerは mistral または openai_compatible を指定してください"},
            status_code=400,
        )
    if first == "url_not_string":
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "openai_base_urlは文字列で指定してください"},
            status_code=400,
        )
    if first == "url_bad_scheme":
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={
                "fields": ["openai_base_url"],
                "reason": "URLは http:// または https:// で始まっている必要があります",
            },
            status_code=400,
        )
    if first == "openai_model_not_string":
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "openai_modelは文字列で指定してください"},
            status_code=400,
        )
    if first == "openai_emb_model_not_string":
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "openai_embeddings_modelは文字列で指定してください"},
            status_code=400,
        )
    return error_response(ErrorCode.MALFORMED_INPUT)


def _parse_credentials_save_request(
    data: dict,
) -> tuple[CredentialsSaveRequest | None, tuple[Any, int] | None]:
    """Validate a credentials-save body with ``CredentialsSaveRequest``."""
    try:
        return CredentialsSaveRequest.model_validate(data), None
    except ValidationError as exc:
        return None, _credentials_error_from_validation(exc)


def _ai_generate_error_from_validation(exc: ValidationError) -> tuple[Any, int]:
    """Map canonical generate-schema failures to the historic errors."""
    joined = " ".join(str(candidate.get("msg", "")) for candidate in exc.errors())
    if "theme_must_be_string" in joined:
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "themeは文字列で指定してください", "fields": ["theme"]},
            status_code=400,
        )
    if any(
        candidate.get("type") == "string_too_long" for candidate in exc.errors()
    ):
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "themeは120文字以内で指定してください"},
            status_code=400,
        )
    return error_response(
        ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["theme"]}, status_code=400
    )


def _parse_ai_generate_request(
    data: dict,
) -> tuple[str | None, tuple[Any, int] | None]:
    """Validate a generate body with ``AIPortfolioGenerateRequest``."""
    try:
        return AIPortfolioGenerateRequest.model_validate(data).theme, None
    except ValidationError as exc:
        return None, _ai_generate_error_from_validation(exc)


def _parse_ai_rebalance_request(
    data: dict,
) -> tuple[str | None, tuple[Any, int] | None]:
    """Validate a rebalance body with ``AIPortfolioRebalanceRequest``."""
    try:
        req_obj = AIPortfolioRebalanceRequest.model_validate(data)
    except ValidationError as exc:
        joined = " ".join(str(candidate.get("msg", "")) for candidate in exc.errors())
        if "theme_must_be_string" in joined:
            return None, error_response(
                ErrorCode.INVALID_INPUT,
                details={"reason": "themeは文字列で指定してください", "fields": ["theme"]},
                status_code=400,
            )
        return None, error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "テーマは120文字以内で入力してください"},
            status_code=400,
        )
    theme = req_obj.theme.strip() if req_obj.theme else ""
    return (theme if theme else "tech"), None


def _parse_ai_delete_request(
    data: dict,
) -> tuple[str | None, tuple[Any, int] | None]:
    """Validate a delete body with ``AIPortfolioDeleteRequest``."""
    try:
        return AIPortfolioDeleteRequest.model_validate(data).id.strip(), None
    except ValidationError as exc:
        joined = " ".join(str(candidate.get("msg", "")) for candidate in exc.errors())
        if "id_must_be_string" in joined:
            return None, error_response(
                ErrorCode.INVALID_INPUT,
                details={"reason": "idは文字列で指定してください", "fields": ["id"]},
                status_code=400,
            )
        if any(
            candidate.get("type") == "string_too_long" for candidate in exc.errors()
        ):
            return None, error_response(
                ErrorCode.MALFORMED_INPUT,
                details={"reason": "IDは256文字以内で指定してください"},
                status_code=400,
            )
        return None, error_response(
            ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["id"]}, status_code=400
        )


def _parse_ai_save_request(
    data: dict,
) -> tuple[AIPortfolioSaveRequest | None, tuple[Any, int] | None]:
    """Validate a save body with ``AIPortfolioSaveRequest``."""
    try:
        return AIPortfolioSaveRequest.model_validate(data), None
    except ValidationError:
        return None, error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "無効なポートフォリオデータです"},
            status_code=400,
        )


def _copy_error_from_marker(marker: str) -> tuple[Any, int]:
    """Render one copy-to-my marker as the historic per-item envelope."""
    kind, _, rest = marker.partition("|")
    if kind == "copy_items_required":
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "itemsリストが必要です"},
            status_code=400,
        )
    if kind == "copy_too_many":
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": f"items は最大 {MAX_AI_PORTFOLIO_ITEMS} 件まで指定できます"},
            status_code=400,
        )
    idx_text, _, extra = rest.partition("|")
    try:
        idx = int(idx_text)
    except (TypeError, ValueError):
        idx = 0
    if kind == "copy_item_not_object":
        reason = f"items[{idx}] が無効です"
        return error_response(
            ErrorCode.MALFORMED_INPUT, details={"reason": reason}, status_code=400
        )
    if kind == "copy_bad_symbol_market":
        reason = f"items[{idx}] のシンボルまたは市場が無効です"
        return error_response(
            ErrorCode.INVALID_INPUT, details={"reason": reason}, status_code=400
        )
    if kind == "copy_mismatch":
        reason = f"items[{idx}]: {extra}"
        return error_response(
            ErrorCode.INVALID_MARKET, details={"reason": reason}, status_code=400
        )
    if kind == "copy_duplicate":
        dup_symbol, _, dup_market = extra.partition("|")
        reason = f"items[{idx}] の銘柄が重複しています: {dup_symbol} ({dup_market})"
        return error_response(
            ErrorCode.INVALID_INPUT, details={"reason": reason}, status_code=400
        )
    if kind == "Duplicate stock in items":
        # Sequential-gate marker:
        # "Duplicate stock in items: SYM (mkt) (item IDX)".
        # Here ``rest`` holds the whole marker text (no second "|").
        detail, _, idx_part = rest.rpartition(" (item ")
        try:
            dup_idx = int(idx_part.rstrip(")"))
        except (TypeError, ValueError):
            dup_idx = 0
        detail = detail.removeprefix("Duplicate stock in items: ")
        reason = f"items[{dup_idx}] の銘柄が重複しています: {detail}"
        return error_response(
            ErrorCode.INVALID_INPUT, details={"reason": reason}, status_code=400
        )
    if kind == "copy_bool_numeric":
        reason = f"items[{idx}] の数値が無効です（真偽値は不可）"
        return error_response(
            ErrorCode.INVALID_INPUT, details={"reason": reason}, status_code=400
        )
    if kind == "copy_bad_numeric":
        reason = f"items[{idx}] の数値が無効です"
        return error_response(
            ErrorCode.INVALID_INPUT, details={"reason": reason}, status_code=400
        )
    if kind == "copy_bad_target_range":
        reason = (
            f"items[{idx}] の target_price は 0 より大きく "
            f"{PORTFOLIO_AVG_PRICE_MAX:,.0f} 以下の値が必要です"
        )
        return error_response(
            ErrorCode.INVALID_INPUT, details={"reason": reason}, status_code=400
        )
    if kind == "copy_bad_weight_range":
        reason = f"items[{idx}] の weight_pct は 0〜100 の範囲が必要です"
        return error_response(
            ErrorCode.INVALID_INPUT, details={"reason": reason}, status_code=400
        )
    return error_response(
        ErrorCode.MALFORMED_INPUT,
        details={"reason": "無効なポートフォリオデータです"},
        status_code=400,
    )


def _parse_ai_copy_to_my_request(
    data: dict,
) -> tuple[list[tuple[str, str, float, float]] | None, tuple[Any, int] | None]:
    """Validate a copy-to-my body with ``AIPortfolioCopyToMyRequest``.

    Returns normalized ``(symbol, market, target_price, weight_pct)`` tuples
    in input order, exactly as the historic inline loop produced.
    """
    try:
        validated = AIPortfolioCopyToMyRequest.model_validate(data)
    except ValidationError as exc:
        errors = exc.errors()
        first_msg = str(errors[0].get("msg", "")) if errors else ""
        first_loc = errors[0].get("loc", ()) if errors else ()
        marker = first_msg
        # Pydantic v2 prefixes ``Value error, `` to before-validator messages.
        marker = marker.removeprefix("Value error, ")
        if marker.startswith(("copy_", "Total weight", "Duplicate stock in items")):
            if marker.startswith("Total weight"):
                return None, error_response(
                    ErrorCode.INVALID_INPUT,
                    details={"reason": "items の weight_pct 合計は100以下である必要があります"},
                    status_code=400,
                )
            if marker.startswith("Duplicate stock in items"):
                return None, _copy_error_from_marker(f"Duplicate stock in items|{marker}")
            return None, _copy_error_from_marker(marker)
        # Item-level or structural failures that the sequential gate already
        # excludes for route traffic; map conservatively without new text.
        loc = first_loc or ()
        if len(loc) >= 3 and loc[0] == "items" and isinstance(loc[1], int):
            field = str(loc[2])
            if field in ("symbol", "market"):
                return None, _copy_error_from_marker(f"copy_bad_symbol_market|{loc[1]}")
            return None, _copy_error_from_marker(f"copy_bad_numeric|{loc[1]}")
        return None, error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "itemsリストが必要です"},
            status_code=400,
        )
    return [
        (item.symbol, item.market, float(item.target_price or 0.0), float(item.weight_pct or 0.0))
        for item in validated.items
    ], None


def _invalidate_symbol_caches_internal(symbol: str) -> None:
    """Helper to invalidate all in-memory and disk caches for a single stock symbol."""
    # 1. Global in-memory cache (exact keys and delimited '_' prefixes)
    clear_cache_key(f"hist_{symbol}")
    clear_cache_prefix(f"hist_{symbol}_")
    clear_cache_prefix(f"research_context_{symbol}_")
    clear_cache_key(f"info_{symbol}")
    clear_cache_key(f"info_{symbol}__failed")
    clear_cache_prefix(f"info_{symbol}_")

    # 2. yfinance short cache (exact keys and delimited prefixes)
    clear_yfinance_short_cache_key(f"info_short_{symbol}")
    clear_yfinance_short_cache_key(f"info_short_{symbol}__failed")
    clear_yfinance_short_cache_prefix(f"info_short_{symbol}_")
    clear_yfinance_short_cache_key(f"fastinfo_{symbol}")
    clear_yfinance_short_cache_key(f"fastinfo_{symbol}__failed")
    clear_yfinance_short_cache_prefix(f"fastinfo_{symbol}_")
    clear_yfinance_short_cache_prefix(f"history_short_{symbol}_")
    clear_yfinance_short_cache_prefix(f"history_short_payload_{symbol}_")

    # 3. Disk caches (exact keys and delimited prefixes)
    try:
        app_state.stock_disk_cache.delete(f"hist_{symbol}")
        app_state.stock_disk_cache.delete_prefix(f"hist_{symbol}_")
        app_state.stock_disk_cache.delete(f"hist_df_{symbol}")
        app_state.stock_disk_cache.delete_prefix(f"hist_df_{symbol}_")
        app_state.stock_disk_cache.delete(f"info_disk_{symbol}")
        app_state.stock_disk_cache.delete(f"info_disk_{symbol}__failed")
        app_state.stock_disk_cache.delete_prefix(f"info_disk_{symbol}_")
        app_state.payload_disk_cache.delete(f"payload_{symbol}")
        app_state.payload_disk_cache.delete_prefix(f"payload_{symbol}_")
    except Exception as exc:
        logger.debug("Cache invalidation disk partially failed for %s: %s", symbol, exc)


def invalidate_stock_caches(symbol: str) -> None:
    """Invalidate all cache entries related to a specific symbol."""
    clear_cache_prefix("stocks")
    _invalidate_symbol_caches_internal(symbol)


def invalidate_single_stock_cache(symbol: str) -> None:
    """Invalidate only the caches for a single symbol (preserves stocks list)."""
    _invalidate_symbol_caches_internal(symbol)


def ensure_stock_placeholder_in_caches(
    symbol: str,
    name: str,
    market: str,
    shares: float | None = None,
    avg_price: float | None = None,
    avg_fx_rate: float | None = None,
) -> None:
    """Ensure a placeholder entry exists in the stock caches for a symbol and update holdings atomically."""
    with app_state.cache.sse_data_lock:
        for cache in (
            app_state.market.target_stocks_cache,
            app_state.market.current_stocks_cache,
        ):
            if market not in cache:
                cache[market] = []
            target_list = cache[market]
            entry = next((s for s in target_list if s.get("symbol") == symbol), None)
            if entry is None:
                new_entry: dict[str, Any] = {
                    "symbol": symbol,
                    "name": name,
                    "market": market,
                    "price": "--",
                    "change": "--",
                    "change_percent": "--",
                    "chart_data": [],
                    "shares": shares if shares is not None else 0,
                    "avg_price": avg_price if avg_price is not None else 0,
                }
                if market not in ("jp", "idx") and avg_fx_rate is not None:
                    new_entry["avg_fx_rate"] = avg_fx_rate
                target_list.append(new_entry)
            else:
                if shares is not None:
                    entry["shares"] = shares
                if avg_price is not None:
                    entry["avg_price"] = avg_price
                if market in ("jp", "idx"):
                    entry.pop("avg_fx_rate", None)
                elif avg_fx_rate is not None:
                    entry["avg_fx_rate"] = avg_fx_rate
                elif shares is not None or avg_price is not None:
                    entry.pop("avg_fx_rate", None)


def remove_stock_from_caches(symbol, market):
    """Remove a symbol from both in-memory and disk caches."""
    with app_state.cache.sse_data_lock:
        for cache in (
            app_state.market.target_stocks_cache,
            app_state.market.current_stocks_cache,
        ):
            if market not in cache:
                cache[market] = []
            cache[market] = [s for s in cache[market] if s.get("symbol") != symbol]
    _invalidate_symbol_caches_internal(symbol)
    try:
        app_state.payload_disk_cache.delete(f"payload_{symbol}_{market}")
    except Exception as exc:
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
