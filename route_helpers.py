"""
route_helpers.py - Helper functions shared between app.py and routes/*.py
These are extracted from app.py to break the circular import.
"""
import re
import time
import threading
from functools import wraps
from typing import Dict, List, Tuple, Optional, Any

from flask import request, jsonify, g

from app_helpers import (
    normalize_market, normalize_symbol, normalize_symbol_for_market,
    normalize_text, is_valid_symbol, error_response,
    _get_stock_container, _default_stock_names, _token_fingerprint,
    clear_cache_prefix,
    MAX_STOCK_NAME_LENGTH as _MAX_STOCK_NAME_LENGTH,
)
from app_state import app_state
from config_utils import _env_int, get_mistral_api_key, get_langsearch_api_key, get_tavily_api_key
from error_codes import ErrorCode

MAX_STOCK_NAME_LENGTH = _MAX_STOCK_NAME_LENGTH

# ============================================================
# Rate Limiting
# ============================================================
_rate_limit_store: Dict[str, List[float]] = {}
_rate_limit_window_by_key: Dict[str, int] = {}
_rate_limit_lock = threading.Lock()
_RATE_LIMIT_CLEANUP_INTERVAL: int = _env_int("MNS_RATE_LIMIT_CLEANUP_INTERVAL", 60, 10, 3600)
_RATE_LIMIT_MAX_ENTRIES: int = _env_int("MNS_RATE_LIMIT_MAX_ENTRIES", 1000, 100, 50000)
_rate_limit_last_cleanup: float = time.time()


def _cleanup_rate_limit_store():
    """期限切れのレート制限エントリを削除してメモリリークを防止"""
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

    # ストアサイズが上限を超えた場合、最も古いエントリから削除
    if len(_rate_limit_store) > _RATE_LIMIT_MAX_ENTRIES:
        sorted_keys = sorted(
            _rate_limit_store.keys(),
            key=lambda k: _rate_limit_store[k][-1] if _rate_limit_store[k] else 0,
        )
        excess = len(_rate_limit_store) - _RATE_LIMIT_MAX_ENTRIES
        for old_key in sorted_keys[:excess]:
            del _rate_limit_store[old_key]
            _rate_limit_window_by_key.pop(old_key, None)


def _rate_limit_env_name(endpoint: str, suffix: str) -> str:
    safe_endpoint = re.sub(r"[^A-Za-z0-9]+", "_", (endpoint or "default")).upper()
    return f"MNS_RATE_LIMIT_{safe_endpoint}_{suffix}"


def _resolve_rate_limit(endpoint: str, default_max: int, default_window: int) -> Tuple[int, int]:
    resolved_max = _env_int("MNS_RATE_LIMIT_DEFAULT_MAX", default_max, 1, 100000)
    resolved_window = _env_int("MNS_RATE_LIMIT_DEFAULT_WINDOW", default_window, 1, 86400)
    resolved_max = _env_int(_rate_limit_env_name(endpoint, "MAX"), resolved_max, 1, 100000)
    resolved_window = _env_int(_rate_limit_env_name(endpoint, "WINDOW"), resolved_window, 1, 86400)
    return resolved_max, resolved_window


def rate_limit(max_requests=60, window_seconds=60):
    """シンプルなIPベースレート制限デコレータ（個人利用向け）"""
    LOCAL_HOST_MULTIPLE = 2
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            local_addrs = ("127.0.0.1", "localhost", "::1")
            remote_addr = request.remote_addr or ""
            is_local = remote_addr in local_addrs
            if is_local:
                forwarded_for = (request.headers.get("X-Forwarded-For") or "").strip()
                if forwarded_for:
                    forwarded_first = forwarded_for.split(",")[0].strip()
                    if forwarded_first and forwarded_first not in local_addrs:
                        remote_addr = forwarded_first
                        is_local = False

            current_time = time.time()
            endpoint = request.endpoint or getattr(f, "__name__", "default")
            effective_max_requests, effective_window_seconds = _resolve_rate_limit(
                endpoint, max_requests, window_seconds
            )
            # Apply higher limit for localhost to avoid blocking legitimate use
            # while still protecting against abuse from malicious browser tabs
            if is_local:
                effective_max_requests = max(1, effective_max_requests * LOCAL_HOST_MULTIPLE)
            key = f"{remote_addr}:{endpoint}"

            with _rate_limit_lock:
                _rate_limit_window_by_key[key] = effective_window_seconds
                global _rate_limit_last_cleanup
                if current_time - _rate_limit_last_cleanup > _RATE_LIMIT_CLEANUP_INTERVAL:
                    _cleanup_rate_limit_store()
                    _rate_limit_last_cleanup = current_time

                if key not in _rate_limit_store:
                    _rate_limit_store[key] = []

                _rate_limit_store[key] = [
                    t for t in _rate_limit_store[key]
                    if current_time - t < effective_window_seconds
                ]

                if len(_rate_limit_store[key]) >= effective_max_requests:
                    retry_after = max(
                        0,
                        int(effective_window_seconds - (current_time - _rate_limit_store[key][0])),
                    )
                    response = jsonify({
                        "error": "レート制限を超過しました。しばらく後にお試しください",
                        "error_flag": True,
                        "error_code": int(ErrorCode.API_RATE_LIMITED),
                        "message": "レート制限を超過しました。しばらく後にお試しください",
                        "details": {"retry_after": retry_after},
                    })
                    response.status_code = 429
                    response.headers["Retry-After"] = str(retry_after)
                    return response

                _rate_limit_store[key].append(current_time)

            return f(*args, **kwargs)
        return wrapper
    return decorator


# ============================================================
# API Key Extraction
# ============================================================
def extract_api_key(req):
    """リクエストからMistral APIキーを抽出する。"""
    from flask import current_app
    stored = get_mistral_api_key()
    if stored:
        current_app.logger.debug(
            "Mistral key source=stored fp=%s id=%s",
            _token_fingerprint(stored),
            getattr(g, "request_id", "-"),
        )
        return stored
    try:
        auth = req.headers.get("Authorization", "")
        if not auth:
            current_app.logger.warning("Mistral key missing id=%s", getattr(g, "request_id", "-"))
            return ""
        if not auth.startswith("Bearer "):
            current_app.logger.warning("Mistral key invalid auth scheme id=%s", getattr(g, "request_id", "-"))
            return ""
        token = auth[7:].strip()
        if token:
            current_app.logger.debug(
                "Mistral key source=header fp=%s id=%s",
                _token_fingerprint(token),
                getattr(g, "request_id", "-"),
            )
        else:
            current_app.logger.warning("Mistral key empty bearer token id=%s", getattr(g, "request_id", "-"))
        return token
    except (KeyError, AttributeError, ValueError) as exc:
        current_app.logger.error("Mistral key extraction error id=%s: %s", getattr(g, "request_id", "-"), exc)
        return ""


def extract_langsearch_api_key(req):
    """Extract LangSearch API key from stored config or custom header."""
    from flask import current_app
    stored = get_langsearch_api_key()
    if stored:
        current_app.logger.debug(
            "LangSearch key source=stored fp=%s id=%s",
            _token_fingerprint(stored),
            getattr(g, "request_id", "-"),
        )
        return stored
    token = (req.headers.get("X-LangSearch-Key") or "").strip()
    if token:
        current_app.logger.debug(
            "LangSearch key source=header fp=%s id=%s",
            _token_fingerprint(token),
            getattr(g, "request_id", "-"),
        )
    return token


def extract_tavily_api_key(req):
    """Extract Tavily API key from stored config or custom header."""
    from flask import current_app
    stored = get_tavily_api_key()
    if stored:
        current_app.logger.debug(
            "Tavily key source=stored fp=%s id=%s",
            _token_fingerprint(stored),
            getattr(g, "request_id", "-"),
        )
        return stored
    token = (req.headers.get("X-Tavily-Key") or "").strip()
    if token:
        current_app.logger.debug(
            "Tavily key source=header fp=%s id=%s",
            _token_fingerprint(token),
            getattr(g, "request_id", "-"),
        )
    return token


# ============================================================
# Stock Cache Helpers
# ============================================================
def cleanup_history_circuit_state(now_ts=None, stale_after_sec=600):
    """Remove expired circuit breaker states to free up memory."""
    now_value = time.time() if now_ts is None else float(now_ts)
    with app_state.history_circuit_lock:
        stale_symbols = []
        for sym, state in list(app_state.history_circuit_state.items()):
            open_until = float((state or {}).get("open_until", 0.0) or 0.0)
            status = (state or {}).get("status", "CLOSED")
            if status == "OPEN" and open_until > 0.0 and open_until <= now_value - stale_after_sec:
                stale_symbols.append(sym)
            elif status == "CLOSED" and state.get("timeout_streak", 0) == 0:
                stale_symbols.append(sym)
        for sym in stale_symbols:
            app_state.history_circuit_state.pop(sym, None)


def _stock_display_name(symbol: str, market: str) -> str:
    container = _get_stock_container(market)
    if container and symbol in container:
        value = container[symbol]
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return normalize_text(value.get("name"), symbol)
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
        return None, error_response(ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["symbol"]})
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

    return {"raw_symbol": raw_symbol, "name": name, "market": market, "symbol": symbol}, None


def invalidate_stock_caches(symbol):
    """銘柄関連キャッシュを無効化する"""
    clear_cache_prefix("stocks")
    clear_cache_prefix(f"hist_{symbol}")
    clear_cache_prefix(f"research_context_{symbol}_")


def ensure_stock_placeholder_in_caches(symbol, name, market):
    """キャッシュに銘柄プレースホルダーを確保する"""
    with app_state.sse_data_lock:
        for cache in (app_state.current_stocks_cache, app_state.target_stocks_cache):
            if market not in cache:
                cache[market] = []
            target_list = cache[market]
            if not any(s.get("symbol") == symbol for s in target_list):
                target_list.append({
                    "symbol": symbol, "name": name, "market": market,
                    "price": "--", "change": "--", "change_percent": "--",
                    "chart_data": [], "shares": 0, "avg_price": 0,
                })


def remove_stock_from_caches(symbol, market):
    """キャッシュから銘柄を削除する"""
    with app_state.sse_data_lock:
        for cache in (app_state.current_stocks_cache, app_state.target_stocks_cache):
            if market not in cache:
                cache[market] = []
            cache[market] = [s for s in cache[market] if s.get("symbol") != symbol]


# ============================================================
# Text / Mistral Helpers
# ============================================================
def _extract_text_from_mistral_content(content):
    """Mistral APIの複数形式のcontentからテキストのみを抽出する。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        texts = []
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


def _seconds_until(timestamp: float) -> float:
    """Return seconds until a UNIX timestamp, clamped at zero."""
    return round(max(0.0, (timestamp or 0.0) - time.time()), 2)
