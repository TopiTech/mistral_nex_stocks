"""
route_helpers.py - Helper functions shared between app.py and routes/*.py
These are extracted from app.py to break the circular import.
"""

import re
import time
import threading
import logging
from functools import wraps
from typing import Any, Dict, List, Optional, Tuple

from flask import request, g

from constants import MAX_STOCK_NAME_LENGTH
from utils.caching import clear_cache_prefix
from utils.normalization import (
    normalize_market,
    normalize_symbol,
    normalize_symbol_for_market,
    normalize_text,
    is_valid_symbol,
)
from utils.networking import _is_loopback_ip
from utils.stock_payload import (
    _default_stock_names,
    _get_stock_container,
    clear_yfinance_short_cache_prefix,
    error_response,
)
from utils.text_utils import _token_fingerprint
from app_state import app_state
from credential_manager import get_mistral_api_key, get_langsearch_api_key, get_tavily_api_key
from error_codes import ErrorCode
from utils.env_helpers import _env_int

logger = logging.getLogger(__name__)


def _as_text(value: Any) -> str:
    return "" if value is None else str(value)


# ============================================================
# Rate Limiting
# ============================================================
_rate_limit_store: Dict[str, List[float]] = {}
_rate_limit_window_by_key: Dict[str, int] = {}
_rate_limit_lock = threading.Lock()
_RATE_LIMIT_CLEANUP_INTERVAL: int = _env_int("MNS_RATE_LIMIT_CLEANUP_INTERVAL", 60, 10, 3600)
_RATE_LIMIT_MAX_ENTRIES: int = _env_int("MNS_RATE_LIMIT_MAX_ENTRIES", 1000, 100, 50000)
_RATE_LIMIT_LOCAL_HOST_MULTIPLE: int = 2
_rate_limit_last_cleanup: float = time.time()
# M-4: This in-memory store is intentionally not persisted to disk.
# Rate limits reset on server restart. This is acceptable for a personal-use
# local app but would need a persistent backend (Redis, etc.) for production.


def _cleanup_rate_limit_store() -> None:
    """Remove expired rate-limit entries to prevent memory leaks."""
    current_time = time.time()
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


def _rate_limit_env_name(endpoint: str, suffix: str) -> str:
    safe_endpoint = re.sub(r"[^A-Za-z0-9]+", "_", (endpoint or "default")).upper()
    return f"MNS_RATE_LIMIT_{safe_endpoint}_{suffix}"


def _resolve_rate_limit(endpoint: str, default_max: int, default_window: int) -> Tuple[int, int]:
    # Precedence: endpoint-specific env > decorator argument (code default)
    # If endpoint-specific env is set, use it directly.
    # Otherwise, return the decorator's default value.
    resolved_max = _env_int(_rate_limit_env_name(endpoint, "MAX"), default_max, 1, 100000)
    resolved_window = _env_int(_rate_limit_env_name(endpoint, "WINDOW"), default_window, 1, 86400)
    return resolved_max, resolved_window


def rate_limit(max_requests: int = 60, window_seconds: int = 60):
    """Simple IP-based rate limiting decorator (designed for personal use).

    Uses an in-memory store (not persisted). Rate limits reset on server restart.
    For production deployments, replace with a persistent backend (Redis, etc.).
    """

    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            remote_addr = request.remote_addr or ""
            is_local = _is_loopback_ip(remote_addr)
            if is_local:
                # Local requests bypass rate limiting entirely for personal use
                return f(*args, **kwargs)

            current_time = time.time()
            endpoint = str(request.endpoint or getattr(f, "__name__", "default"))
            effective_max_requests, effective_window_seconds = _resolve_rate_limit(
                endpoint, max_requests, window_seconds
            )
            key = f"{remote_addr}:{endpoint}"

            with _rate_limit_lock:
                _rate_limit_window_by_key[key] = effective_window_seconds
                global _rate_limit_last_cleanup
                if current_time - _rate_limit_last_cleanup > _RATE_LIMIT_CLEANUP_INTERVAL:
                    _cleanup_rate_limit_store()
                    _rate_limit_last_cleanup = current_time

                if key not in _rate_limit_store:
                    # Proactive eviction if store is full to prevent unbounded memory growth under flood
                    if len(_rate_limit_store) >= _RATE_LIMIT_MAX_ENTRIES:
                        _cleanup_rate_limit_store()
                        if len(_rate_limit_store) >= _RATE_LIMIT_MAX_ENTRIES:
                            sorted_keys = sorted(
                                _rate_limit_store.keys(),
                                key=lambda k: _rate_limit_store[k][0] if _rate_limit_store[k] else 0.0,
                            )
                            excess = len(_rate_limit_store) - _RATE_LIMIT_MAX_ENTRIES + 1
                            for old_key in sorted_keys[:excess]:
                                _rate_limit_store.pop(old_key, None)
                                _rate_limit_window_by_key.pop(old_key, None)
                    _rate_limit_store[key] = []

                _rate_limit_store[key] = [
                    t for t in _rate_limit_store[key] if current_time - t < effective_window_seconds
                ]

                if len(_rate_limit_store[key]) >= effective_max_requests:
                    retry_after = max(
                        0,
                        int(effective_window_seconds - (current_time - _rate_limit_store[key][0])),
                    )
                    resp, _ = error_response(
                        ErrorCode.API_RATE_LIMITED,
                        status_code=429,
                        details={"retry_after": retry_after},
                    )
                    resp.headers["Retry-After"] = str(retry_after)
                    return resp, 429

                _rate_limit_store[key].append(current_time)

            return f(*args, **kwargs)

        return wrapper

    return decorator


# ============================================================
# API Key Extraction
# ============================================================
def extract_api_key(req: Any) -> str:
    """Extract the Mistral API key from secure server-side storage.

    Always uses the server-stored key. Client-provided keys are only accepted
    in TESTING mode for test compatibility.
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

    if current_app.config.get("TESTING"):
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
    """Extract LangSearch API key from stored config. Always uses secure storage."""
    from flask import current_app

    stored: str = _as_text(get_langsearch_api_key())
    if stored:
        current_app.logger.debug(
            "LangSearch key source=stored fp=%s id=%s",
            _token_fingerprint(stored),
            getattr(g, "request_id", "-"),
        )
        return stored

    if current_app.config.get("TESTING"):
        hdr: str = str(req.headers.get("X-LangSearch-Key", ""))
        if hdr:
            return hdr
    return ""


def extract_tavily_api_key(req: Any) -> str:
    """Extract Tavily API key from stored config. Always uses secure storage."""
    from flask import current_app

    stored: str = _as_text(get_tavily_api_key())
    if stored:
        current_app.logger.debug(
            "Tavily key source=stored fp=%s id=%s",
            _token_fingerprint(stored),
            getattr(g, "request_id", "-"),
        )
        return stored

    if current_app.config.get("TESTING"):
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


def cleanup_history_circuit_state(
    now_ts: Optional[float] = None, stale_after_sec: int = 600
) -> None:
    """Remove expired circuit breaker states to free up memory.

    Uses a time-based guard to avoid running cleanup on every request.
    """
    global _circuit_cleanup_ts
    now_value = time.time() if now_ts is None else now_ts
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
            if status == "OPEN" and open_until > 0.0 and open_until <= now_value - stale_after_sec:
                stale_symbols.append(sym)
            elif status == "CLOSED" and state.timeout_streak == 0:
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
) -> Tuple[Optional[dict], Optional[Tuple[Any, int]]]:
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
            elif hasattr(chunk, "type"):
                if chunk.type == "text" and hasattr(chunk, "text"):
                    if isinstance(chunk.text, str) and chunk.text.strip():
                        texts.append(chunk.text.strip())
        return "\n".join(texts) if texts else ""
    return ""


def _seconds_until(timestamp: Optional[float]) -> float:
    """Return seconds until a UNIX timestamp, clamped at zero."""
    return round(max(0.0, (timestamp or 0.0) - time.time()), 2)
