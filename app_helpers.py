import copy
import hashlib
import ipaddress
import json
import logging
import math
import os
import platform
import re
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

import pandas as pd
from cachetools import TTLCache
from flask import jsonify, request
from werkzeug.exceptions import BadRequest

from app_state import app_state
from config_utils import (
    protect_data,
    unprotect_data,
)
from constants import (
    _BASE_ALLOWED_CORS_ORIGINS,
    BASE_DIR,
    CACHE_DURATION,
    MAX_JSON_SIZE,
)
from error_codes import ErrorCode, get_error_message
from sectors import PREDEFINED_SECTORS, PREDEFINED_INDUSTRIES

USER_STOCKS_FILE = str(BASE_DIR / "user_stocks.json")

# Constants
VALID_MARKETS = {"us", "jp", "idx"}
VALID_HISTORY_PERIODS = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "max"}
MAX_STOCK_NAME_LENGTH = 200
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9^][A-Z0-9._\-^=]{0,14}$")

DEFAULT_US = {
    "NVDA": "NVIDIA",
    "TSLA": "Tesla",
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "META": "Meta",
    "GOOGL": "Alphabet",
    "AMZN": "Amazon",
    "AMD": "AMD",
}
DEFAULT_JP = {
    "7203.T": "トヨタ自動車",
    "6758.T": "ソニーグループ",
    "9984.T": "ソフトバンクグループ",
    "8306.T": "三菱UFJ FG",
    "6861.T": "キーエンス",
    "6098.T": "リクルートHD",
    "9432.T": "NTT",
    "8035.T": "東京エレクトロン",
}
DEFAULT_IDX = {
    "^N225": "日経平均",
    "^DJI": "NYダウ",
    "^IXIC": "NASDAQ",
    "^GSPC": "S&P500",
}


def get_default_symbols():
    """市場別のデフォルト銘柄一覧を返す"""
    return {
        "us": list(DEFAULT_US.keys()),
        "jp": list(DEFAULT_JP.keys()),
        "idx": list(DEFAULT_IDX.keys()),
    }


def normalize_market(market, default="us"):
    """Validates and normalizes market identifier."""
    value = str(market or default).strip().lower()
    return value if value in VALID_MARKETS else None


def normalize_symbol(symbol):
    """Clean up stock symbol string."""
    if symbol is None:
        return ""
    if not isinstance(symbol, str):
        symbol = str(symbol)
    return symbol.strip().upper()


def normalize_text(value, default=""):
    """テキスト値を正規化して返す。"""
    if value is None:
        return default
    return str(value).strip()


def normalize_symbol_for_market(symbol, market):
    """Adjusts symbol formatting based on market rules (e.g., .T for JP)."""
    s = normalize_symbol(symbol)
    if market == "jp" and s.isdigit():
        return f"{s}.T"
    return s


def _get_stock_container(market: Optional[str]):
    """Return the mutable user-stock container for a normalized market."""
    if market == "us":
        return app_state.user_us
    if market == "jp":
        return app_state.user_jp
    if market == "idx":
        return app_state.user_idx
    return None


def _default_stock_names(market: str) -> Dict[str, str]:
    if market == "us":
        return DEFAULT_US
    if market == "jp":
        return DEFAULT_JP
    if market == "idx":
        return DEFAULT_IDX
    return {}


def _stock_is_default_or_user(symbol: str, market: str) -> bool:
    container = _get_stock_container(market)
    return bool(
        container is not None
        and (symbol in container or symbol in _default_stock_names(market))
    )


def _short_text(value, limit=160):
    text = str(value or "").strip().replace("\n", " ")
    return text if len(text) <= limit else (text[:limit] + "...")


def _token_fingerprint(token):
    """トークンの安全なフィンガープリント生成（SHA256ハッシュ）"""
    t = (token or "").strip()
    if not t:
        return "none"
    digest = hashlib.sha256(t.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"sha256={digest}"


def _token_mask(token):
    """トークンのマスク表示（最初と最後の2文字のみ保持）"""
    t = (token or "").strip()
    if not t:
        return "none"
    if len(t) <= 4:
        return "*" * len(t)
    return f"{t[:2]}...{t[-2:]}"


def _is_valid_api_key(value, min_length=8):
    """Validate API key format for minimum length and no whitespace."""
    if not value or not isinstance(value, str):
        return False
    token = value.strip()
    if len(token) < min_length:
        return False
    if re.search(r"\s", token):
        return False
    return True


def _parse_json_request():
    """Parse a JSON request body and return an object or None for missing/malformed JSON."""
    content_length = request.content_length
    if content_length and content_length > MAX_JSON_SIZE:
        return None

    try:
        payload = request.get_json(force=False, silent=False)
    except (ValueError, TypeError, AttributeError):
        return None
    except BadRequest:
        return None

    if payload is None:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _sanitize_error_message(error_msg):
    """エラーメッセージから機密情報を削除"""
    if not error_msg:
        return ""
    sensitive_patterns = [
        r"api[_-]?key['\"]?\s*[:=]\s*['\"]?[^\s'\"]+",
        r"token['\"]?\s*[:=]\s*['\"]?[^\s'\"]+",
        r"password['\"]?\s*[:=]\s*['\"]?[^\s'\"]+",
        r"authorization['\"]?\s*[:=]\s*['\"]?[^\s'\"]+",
        r"bearer\s+[a-z0-9\._\-]{10,}",
        r"https?://[a-z0-9]+:[a-z0-9]+@",
        r"secret['\"]?\s*[:=]\s*['\"]?[^\s'\"]+",
    ]
    sanitized = str(error_msg)
    for pattern in sensitive_patterns:
        sanitized = re.sub(pattern, "[REDACTED]", sanitized, flags=re.IGNORECASE)
    return sanitized


def is_valid_symbol(symbol):
    """強化されたシンボル検証（SQLインジェクションやパストラバーサル対策）"""
    if not symbol or len(symbol) > 15:
        return False
    symbol_str = str(symbol)
    dangerous_chars = ["/", "\\", "..", "\0", "%", "\x00", "\n", "\r"]
    if any(char in symbol_str for char in dangerous_chars):
        return False
    symbol_normalized = unicodedata.normalize("NFKC", symbol_str)
    if not SYMBOL_PATTERN.match(symbol_normalized):
        return False
    return True


def parse_non_negative_float(value, field_name, max_value=None):
    """Safely parse a number and ensure it is non-negative and finite."""
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{field_name} must be finite")
    if parsed < 0:
        raise ValueError(f"{field_name} must be >= 0")
    if max_value is not None and parsed > max_value:
        raise ValueError(f"{field_name} must be <= {max_value}")
    return parsed


def _normalize_extension_origin(raw):
    if raw is None:
        return None
    value = str(raw).strip().rstrip("/")
    if not value:
        return None

    if value.startswith("chrome-extension://"):
        origin_id = value[len("chrome-extension://") :].lower()
        if re.fullmatch(r"[a-z0-9]{32}", origin_id):
            return f"chrome-extension://{origin_id}"
        return None

    normalized = value.lower()
    if re.fullmatch(r"[a-z0-9]{32}", normalized):
        return f"chrome-extension://{normalized}"
    return None


def _load_allowed_extension_origins():
    """Load extension origins from env and native host manifest (if available)."""
    now = time.time()
    with app_state._extension_origins_cache_lock:
        if (
            now - app_state._extension_origins_cache_ts
        ) < app_state._EXTENSION_ORIGINS_CACHE_TTL_SEC:
            return set(app_state._extension_origins_cache)

    origins = set()
    app_state._extension_manifest_status["ok"] = True
    app_state._extension_manifest_status["error"] = ""

    extension_origin = _normalize_extension_origin(
        os.environ.get("MNS_EXTENSION_ORIGIN", "")
    )
    if extension_origin:
        origins.add(extension_origin)

    env_origins = os.environ.get("MNS_ALLOWED_EXTENSION_ORIGINS", "")
    for raw in env_origins.split(","):
        origin = _normalize_extension_origin(raw)
        if origin:
            origins.add(origin)

    try:
        manifest_path = (
            Path(__file__).resolve().parent
            / "native_host"
            / "com.mistral_nex_stocks.host.json"
        )
        if manifest_path.exists():
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest_data = json.load(f) or {}
            for raw in manifest_data.get("allowed_origins", []) or []:
                origin = _normalize_extension_origin(str(raw or "").strip())
                if origin:
                    origins.add(origin)
    except FileNotFoundError:
        logger.debug("Extension manifest not found, skipping")
    except Exception as exc:
        app_state._extension_manifest_status["ok"] = False
        app_state._extension_manifest_status["error"] = f"manifest_load_error: {exc}"

    with app_state._extension_origins_cache_lock:
        app_state._extension_origins_cache.clear()
        app_state._extension_origins_cache.update(origins)
        app_state._extension_origins_cache_ts = now

    return origins


def get_allowed_cors_origins():
    """Retrieve the set of allowed CORS origins from constants and dynamic sources."""
    origins = {origin.rstrip("/") for origin in _BASE_ALLOWED_CORS_ORIGINS}
    origins.update(_load_allowed_extension_origins())
    return origins


def require_trusted_state_changing_request(req, require_origin=True):
    """Validate local state-changing API requests with a consistent origin policy."""
    if not _is_local_request(req):
        return False, "forbidden"
    if require_origin and not _is_allowed_shutdown_origin(req):
        return False, "untrusted origin"
    return True, ""


def _is_allowed_shutdown_origin(req):
    """シャットダウン要求の送信元オリジンが許可されているか判定"""
    allowed_origins = get_allowed_cors_origins()
    normalized_origins = {o.rstrip("/") for o in allowed_origins}

    origin = (req.headers.get("Origin") or "").strip().rstrip("/")
    if origin:
        return origin in normalized_origins

    referer = (req.headers.get("Referer") or "").strip()
    if referer:
        parsed = urlparse(referer)
        ref_origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        return ref_origin in normalized_origins
    return False


def _is_loopback_ip(ip_str: str) -> bool:
    if not ip_str:
        return False
    ip_str = ip_str.strip().lower()
    if ip_str in ("localhost", "localhost:5000", "localhost:80", "localhost:443"):
        return True

    # Handle IPv6 with port, e.g., [::1]:5000
    if ip_str.startswith("[") and "]" in ip_str:
        bracket_end = ip_str.index("]")
        inner = ip_str[1:bracket_end]
        try:
            addr = ipaddress.ip_address(inner)
            return addr.is_loopback
        except ValueError:
            return False

    # Strip port if present (e.g. 127.0.0.1:5000)
    if ":" in ip_str:
        parts = ip_str.split(":")
        if len(parts) == 2:
            ip_str = parts[0]

    try:
        addr = ipaddress.ip_address(ip_str)
        return addr.is_loopback
    except ValueError:
        return False


def _is_local_request(req):
    """Check if the request originates from localhost with 2026 security standards."""
    remote = (req.remote_addr or "").strip()
    if not _is_loopback_ip(remote):
        return False

    forwarded = req.headers.get("X-Forwarded-For", "")
    if forwarded:
        forwarded_ips = [x.strip() for x in forwarded.split(",")]
        for ip in forwarded_ips:
            if ip and not _is_loopback_ip(ip):
                return False

    host = req.headers.get("Host", "")
    parsed_host = host.split(":")[0].lower()
    if parsed_host not in ("localhost", "127.0.0.1", "[::1]"):
        return False
    return True


def sanitize_cache_key(key):
    """キャッシュキーを安全にサニタイズ"""
    if not isinstance(key, str):
        key = str(key)
    # 危険な文字を削除
    sanitized = re.sub(r"[^\w\-:._]", "_", key)
    # 長すぎるキーを制限
    return sanitized[:256]


# Caching Utilities


def get_cached(key, fetch_func, duration=CACHE_DURATION, valid_func=None):
    """キャッシュ取得かつスタンペード防止"""
    safe_key = sanitize_cache_key(key)

    with app_state.cache_lock:
        if duration not in app_state.caches:
            app_state.caches[duration] = TTLCache(maxsize=128, ttl=duration)
        if safe_key in app_state.caches[duration]:
            app_state.record_hit()
            return app_state.caches[duration][safe_key]

    app_state.record_miss()

    with app_state.fetch_events_lock:
        if safe_key in app_state.fetch_events:
            ev = app_state.fetch_events[safe_key]
            is_fetcher = False
        else:
            ev = threading.Event()
            app_state.fetch_events[safe_key] = ev
            is_fetcher = True

    if not is_fetcher:
        ev.wait(timeout=10)
        with app_state.cache_lock:
            cache = app_state.caches.get(duration, {})
            if safe_key in cache:
                return cache[safe_key]
        # Re-check: another thread may have populated while we waited above
        with app_state.cache_lock:
            cache = app_state.caches.get(duration, {})
            if safe_key in cache:
                return cache[safe_key]
        return fetch_func()

    try:
        result = fetch_func()
        if valid_func is None or valid_func(result):
            with app_state.cache_lock:
                if duration not in app_state.caches:
                    app_state.caches[duration] = TTLCache(maxsize=128, ttl=duration)
                app_state.caches[duration][safe_key] = result
        return result
    finally:
        with app_state.fetch_events_lock:
            app_state.fetch_events.pop(safe_key, None)
        ev.set()


def clear_cache_prefix(prefix):
    """Clears all cached items starting with the given prefix."""
    prefix_text = sanitize_cache_key(str(prefix))
    with app_state.cache_lock:
        for _duration, cache in app_state.caches.items():
            keys_to_delete = [
                k
                for k in list(cache.keys())
                if isinstance(k, str)
                and (k == prefix_text or k.startswith(prefix_text))
            ]
            for k in keys_to_delete:
                cache.pop(k, None)


def _ensure_cache_bucket(duration):
    """Ensures a TTLCache bucket exists for the given duration."""
    with app_state.cache_lock:
        if duration not in app_state.caches:
            app_state.caches[duration] = TTLCache(maxsize=128, ttl=duration)
        return app_state.caches[duration]


def _has_cached_key(key, duration):
    """Check if a specific key is present in the cache for a given duration."""
    with app_state.cache_lock:
        cache = app_state.caches.get(duration)
        return bool(cache and key in cache)


def _set_cached_value(key, value, duration):
    """Explicitly set a value in the cache bucket."""
    cache = _ensure_cache_bucket(duration)
    with app_state.cache_lock:
        cache[key] = value


def _get_cached_value(key, duration, default=None):
    """Retrieve a value from the cache bucket without triggering a fetch."""
    with app_state.cache_lock:
        cache = app_state.caches.get(duration)
        if cache is None:
            return default
        return cache.get(key, default)


def get_cached_context_with_negative_cache(
    key, fetch_func, success_ttl=600, negative_ttl=90, bypass_negative_cache=False
):
    """ネガティブキャッシュ付きでコンテキストを取得する。"""
    neg_key = f"{key}__negative"
    if not bypass_negative_cache and _has_cached_key(neg_key, negative_ttl):
        return ""

    result = get_cached(
        key,
        fetch_func,
        duration=success_ttl,
        valid_func=lambda x: bool(isinstance(x, str) and x.strip()),
    )
    text = result if isinstance(result, str) else ""
    if text.strip():
        return text

    if not bypass_negative_cache and negative_ttl > 0:
        _set_cached_value(neg_key, True, negative_ttl)
    return text


def _resolve_stocks_for_response():
    """Use current cache by default and fill empty markets from target cache.

    Returns a snapshot using list copy (shallow) instead of deepcopy for performance.
    SSE consumers receive serialized JSON anyway, so deep copying is unnecessary.
    """
    empty: dict[str, list[Any]] = {"us": [], "jp": [], "idx": []}
    current = (
        app_state.current_stocks_cache
        if isinstance(app_state.current_stocks_cache, dict)
        else empty
    )
    target = (
        app_state.target_stocks_cache
        if isinstance(app_state.target_stocks_cache, dict)
        else empty
    )
    resolved = {}
    for market in ("us", "jp", "idx"):
        current_rows = (
            current.get(market) if isinstance(current.get(market), list) else []
        )
        target_rows = target.get(market) if isinstance(target.get(market), list) else []
        # Use list() shallow copy instead of deepcopy to reduce GC pressure
        # on SSE hot paths. Callers serialize to JSON immediately.
        resolved[market] = list(current_rows if current_rows else target_rows)
    return resolved


def _resolve_indices_for_response():
    """Prefer current cache, but fall back to target cache for fast first paint.

    Returns a snapshot using dict() shallow copy instead of deepcopy.
    """
    current = (
        app_state.current_indices_cache
        if isinstance(app_state.current_indices_cache, dict)
        else {}
    )
    target = (
        app_state.target_indices_cache
        if isinstance(app_state.target_indices_cache, dict)
        else {}
    )
    # Use dict() shallow copy instead of deepcopy for performance
    if current:
        return dict(current)
    return dict(target)


def _has_ready_indices_snapshot() -> bool:
    current = (
        app_state.current_indices_cache
        if isinstance(app_state.current_indices_cache, dict)
        else {}
    )
    target = (
        app_state.target_indices_cache
        if isinstance(app_state.target_indices_cache, dict)
        else {}
    )
    return bool(current) or bool(target)


def _has_ready_stocks_snapshot() -> bool:
    empty: Dict[str, List] = {"us": [], "jp": [], "idx": []}
    current = (
        app_state.current_stocks_cache
        if isinstance(app_state.current_stocks_cache, dict)
        else empty
    )
    target = (
        app_state.target_stocks_cache
        if isinstance(app_state.target_stocks_cache, dict)
        else empty
    )
    for market in ("us", "jp", "idx"):
        current_rows = (
            current.get(market) if isinstance(current.get(market), list) else []
        )
        target_rows = target.get(market) if isinstance(target.get(market), list) else []
        if current_rows or target_rows:
            return True
    return False


def _wait_for_initial_market_snapshot(
    snapshot_type: str, timeout_sec: float = 6.0, poll_interval: float = 0.25
) -> bool:
    """Wait briefly for the first market snapshot so the initial page load does not look empty."""
    from app_bg import schedule_sync_all_stocks_now

    check_ready = (
        _has_ready_indices_snapshot
        if snapshot_type == "indices"
        else _has_ready_stocks_snapshot
    )
    if check_ready():
        return True

    schedule_sync_all_stocks_now()
    deadline = time.time() + max(0.0, timeout_sec)
    while time.time() < deadline:
        if check_ready():
            return True
        time.sleep(poll_interval)
    return False


def load_user_stocks(force=False):
    """ユーザーの銘柄設定をファイルから読み込む。"""
    if not os.path.exists(USER_STOCKS_FILE):
        return
    try:
        with app_state.user_stocks_lock:
            mtime_ns = os.stat(USER_STOCKS_FILE).st_mtime_ns
            if not force and mtime_ns <= app_state.last_modified_ns:
                return
            with open(USER_STOCKS_FILE, "r", encoding="utf-8") as f:
                raw_data = json.load(f)

            if (
                isinstance(raw_data, dict)
                and "scheme" in raw_data
                and "value" in raw_data
            ):
                unprotected = unprotect_data(raw_data, key_name="user_stocks")
                if unprotected:
                    data = json.loads(unprotected)
                else:
                    data = {}
            else:
                data = raw_data

            if not isinstance(data, dict):
                data = {}
            # スレッドロック内で一貫性を保って代入
            app_state.user_us = data.get("us", {}) or {}
            app_state.user_jp = data.get("jp", {}) or {}
            app_state.user_idx = data.get("idx", {}) or {}
            app_state.last_modified_ns = mtime_ns
    except (IOError, OSError, json.JSONDecodeError) as exc:
        logger.error("Failed to load user stocks: %s", exc)


def save_user_stocks():
    """ユーザーの銘柄設定をファイルに保存する。"""
    try:
        # データコピーからファイル書き込みまでを同一ロック内で行い、
        # 書き込み中の他スレッドによる変更の喪失を防止する
        with app_state.user_stocks_lock:
            data = {
                "us": copy.deepcopy(app_state.user_us),
                "jp": copy.deepcopy(app_state.user_jp),
                "idx": copy.deepcopy(app_state.user_idx),
            }
            encoded = json.dumps(data, ensure_ascii=False, indent=2)
            protected = protect_data(encoded, key_name="user_stocks")

            tmp_file = Path(USER_STOCKS_FILE).with_suffix(".tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(protected, f, ensure_ascii=False, indent=2)

            os.replace(tmp_file, USER_STOCKS_FILE)

            # Set restrictive file permissions on non-Windows
            if platform.system().lower() != "windows":
                try:
                    os.chmod(USER_STOCKS_FILE, 0o600)
                except OSError:
                    logger.debug(
                        "Failed to set restrictive permissions on %s", USER_STOCKS_FILE
                    )

            app_state.last_modified_ns = os.stat(USER_STOCKS_FILE).st_mtime_ns
    except (IOError, OSError, TypeError) as exc:
        logger.error("Failed to save user stocks: %s", exc)


def error_response(error_code: ErrorCode, status_code: int = 400, details: Optional[dict] = None):
    """統一されたエラーレスポンスを返す

    すべてのエラーレスポンスに ``ok`` フィールドを含めることで
    フロントエンドでのエラーハンドリングを統一する。
    """
    message = get_error_message(error_code, lang="ja")
    sanitized_details = {}
    if details:
        for k, v in details.items():
            sanitized_details[k] = (
                _sanitize_error_message(v) if isinstance(v, str) else v
            )
    return (
        jsonify(
            {
                "ok": False,
                "error": message,
                "error_flag": True,
                "error_code": int(error_code),
                "message": message,
                "details": sanitized_details,
            }
        ),
        status_code,
    )


def _is_market_session_open(
    t, morning_start, morning_end, afternoon_start=None, afternoon_end=None
):
    """セッションの開始・終了時刻に基づいて市場が開いているか判定する。"""
    if morning_start <= t <= morning_end:
        return True
    if afternoon_start and afternoon_end:
        if afternoon_start <= t <= afternoon_end:
            return True
    return False


def _market_status_symbol(market_type):
    if market_type == "jp":
        return "^N225"
    if market_type in ("us", "idx"):
        return "^GSPC"
    return None


def _market_state_from_metadata(metadata):
    if not isinstance(metadata, dict):
        return None

    raw_state = metadata.get("marketState") or metadata.get("market_state")
    if isinstance(raw_state, str):
        normalized_state = raw_state.strip().upper()
        if normalized_state == "REGULAR":
            return "REGULAR"
        if normalized_state:
            return "CLOSED"

    current_period = metadata.get("currentTradingPeriod")
    if isinstance(current_period, dict):
        regular_period = current_period.get("regular")
        if isinstance(regular_period, dict):
            regular_start_raw = regular_period.get("start")
            regular_end_raw = regular_period.get("end")
            if regular_start_raw is None or regular_end_raw is None:
                return None
            try:
                regular_start = float(regular_start_raw)
                regular_end = float(regular_end_raw)
            except (TypeError, ValueError):
                return None
            now_ts = time.time()
            return "REGULAR" if regular_start <= now_ts < regular_end else "CLOSED"

    return None


def _fetch_live_market_state(market_type):
    symbol = _market_status_symbol(market_type)
    if not symbol:
        return None

    try:
        ticker = safe_get_ticker(symbol)
        if not ticker:
            return None

        try:
            metadata = ticker.get_history_metadata()
        except Exception:
            metadata = getattr(ticker, "history_metadata", None)

        return _market_state_from_metadata(metadata)
    except Exception as exc:
        logger.debug(
            "Live market state fetch failed for %s (%s): %s",
            market_type,
            symbol,
            exc,
        )
        return None


def is_market_open(market_type, bypass_cache=False):
    """市場が現在開いているかを判定。Yahoo Financeのステータスを優先し、フォールバックとして時間ベースの判定を行う。"""
    from datetime import datetime, timedelta, timezone
    from datetime import time as dt_time
    from zoneinfo import ZoneInfo

    live_state = None
    if bypass_cache:
        live_state = _fetch_live_market_state(market_type)
    else:
        live_state = get_cached(
            f"market_state_{market_type}",
            lambda: _fetch_live_market_state(market_type),
            duration=5,
            valid_func=lambda value: value in ("REGULAR", "CLOSED"),
        )

    if live_state in ("REGULAR", "CLOSED"):
        app_state.update_market_status(market_type, live_state)
        return live_state == "REGULAR"

    now_utc = datetime.now(timezone.utc)
    if market_type == "jp":
        try:
            jst = now_utc.astimezone(ZoneInfo("Asia/Tokyo"))
        except (ImportError, ValueError, KeyError):
            jst = (now_utc + timedelta(hours=9)).replace(tzinfo=None)
        if jst.weekday() >= 5:
            return False
        return _is_market_session_open(
            jst.time(), dt_time(9, 0), dt_time(11, 30), dt_time(12, 30), dt_time(15, 0)
        )

    if market_type in ("us", "idx"):
        try:
            ny = now_utc.astimezone(ZoneInfo("America/New_York"))
        except Exception:
            year = now_utc.year
            mar_8 = datetime(year, 3, 8, tzinfo=timezone.utc)
            dst_start = mar_8 + timedelta(days=(6 - mar_8.weekday()) % 7)
            nov_1 = datetime(year, 11, 1, tzinfo=timezone.utc)
            dst_end = nov_1 + timedelta(days=(6 - nov_1.weekday()) % 7)
            offset = -4 if dst_start <= now_utc < dst_end else -5
            ny = (now_utc + timedelta(hours=offset)).replace(tzinfo=None)
        if ny.weekday() >= 5:
            return False
        return _is_market_session_open(ny.time(), dt_time(9, 30), dt_time(16, 0))

    return True


# Additional migrated helper functions
def acquire_yfinance_slot() -> bool:
    """yfinance のリクエスト用スロットを取得する。"""
    wait_time = 0.0
    with app_state.yfinance_lock:
        if app_state.is_yfinance_rate_limited and (
            time.time() < app_state.yfinance_rate_limit_until
        ):
            return False

        if app_state.is_yfinance_rate_limited:
            app_state.is_yfinance_rate_limited = False

        now = time.time()
        elapsed = now - app_state.yfinance_last_request_ts
        if elapsed < app_state.yfinance_min_interval_sec:
            wait_time = app_state.yfinance_min_interval_sec - elapsed
        app_state.yfinance_last_request_ts = now + wait_time

    if wait_time > 0.0:
        time.sleep(wait_time)
    return True


def safe_get_ticker(symbol):
    """
    Wrap yf.Ticker instantiation with defensive error handling via stock_provider.
    """
    return app_state.stock_provider.get_ticker(symbol)


def get_stock_info_cached(symbol: str) -> dict:
    """Retrieve basic stock info with yfinance rate-limit protection and caching."""
    neg_key = f"info_{symbol}__failed"
    if _has_cached_key(neg_key, 600):
        return {}

    def _fetch() -> dict:
        try:
            if not acquire_yfinance_slot():
                return {}

            info = app_state.stock_provider.get_fast_info(symbol)
            if not info:
                _set_cached_value(neg_key, True, 600)
                return {}
            return dict(info)
        except Exception as exc:
            logger.debug("yfinance info fetch failed for %s: %s", symbol, exc)
            _set_cached_value(neg_key, True, 600)
            return {}

    cached = get_cached(f"info_{symbol}", _fetch, duration=86400, valid_func=bool)
    return dict(cached) if isinstance(cached, dict) else {}


def choose_display_name(symbol, fallback_name, info):
    """表示名を優先順位に従って選択する"""
    if isinstance(fallback_name, dict):
        fallback_name = fallback_name.get("name", "")
    info = info or {}
    return (
        info.get("shortName")
        or info.get("longName")
        or info.get("displayName")
        or fallback_name
        or symbol
    )


def normalize_optional_number(value):
    """Noneや不正値を除外して数値に変換する"""
    try:
        if value is None:
            return None
        num = float(value)
        if pd.isna(num) or num <= 0:
            return None
        return num
    except (ValueError, TypeError):
        return None


def _fmt(v):
    """Round to 2 decimal places; return None for NaN/None."""
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _fmt_vol(v):
    """Convert to int volume; return None for NaN/None."""
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return int(float(v))
    except (TypeError, ValueError):
        return None


def normalize_history_frame(hist, inplace=False):
    """
    データフレームを正規化：インデックスを DatetimeIndex に変換、Close 列をチェック
    入力検証：非 DataFrame/None 入力に対応
    """
    if hist is None or getattr(hist, "empty", True):
        return pd.DataFrame()

    if not isinstance(hist, pd.DataFrame):
        logger.warning(
            "normalize_history_frame: non-DataFrame input: type=%s",
            type(hist).__name__,
        )
        return pd.DataFrame()

    try:
        frame = hist if inplace else hist.copy()
        if not isinstance(frame.index, pd.DatetimeIndex):
            try:
                frame.index = pd.to_datetime(frame.index)
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "Failed to convert history index to DatetimeIndex: %s", exc
                )
                return pd.DataFrame()

        if "Close" not in frame.columns:
            logger.warning(
                "normalize_history_frame: 'Close' column not found in DataFrame"
            )
            return pd.DataFrame()

        frame = frame.dropna(subset=["Close"])
        return frame
    except (AttributeError, KeyError, TypeError, ValueError) as norm_exc:
        logger.error(
            "normalize_history_frame error: %s", norm_exc, exc_info=True
        )
        return pd.DataFrame()




def _extract_portfolio_fields(name_or_dict):
    """Extract portfolio-related fields from name_or_dict (dict or str)."""
    shares = 0.0
    avg_price = 0.0
    avg_fx_rate = None
    name = name_or_dict.get("name", "") if isinstance(name_or_dict, dict) else name_or_dict

    if isinstance(name_or_dict, dict):
        try:
            shares = float(name_or_dict.get("shares", 0.0))
        except (TypeError, ValueError):
            shares = 0.0
        try:
            avg_price = float(name_or_dict.get("avg_price", 0.0))
        except (TypeError, ValueError):
            avg_price = 0.0
        fx_val = name_or_dict.get("avg_fx_rate")
        if fx_val is not None:
            try:
                avg_fx_rate = float(fx_val)
            except (TypeError, ValueError):
                avg_fx_rate = None
    return name, shares, avg_price, avg_fx_rate


def _compute_price_metrics(hist, symbol):
    """Extract price, change, and percentage from history DataFrame."""
    price = float(hist["Close"].iloc[-1])
    if len(hist) == 1:
        prev = price
    else:
        prev = float(hist["Close"].iloc[-2])

    if pd.isna(price) or pd.isna(prev) or price <= 0 or prev <= 0:
        logger.warning(
            "Stock %s: invalid non-positive close price (price=%s, prev=%s)",
            symbol, price, prev
        )
        return None, None, None

    change = price - prev
    pct = (change / prev) * 100 if prev else 0
    return _fmt(price), _fmt(change), _fmt(pct)


def _build_chart_ohlc_data(df, chart_data_limit=100, ohlc_data_limit=365):
    """Build chart_data and ohlc_data arrays from a DataFrame with MA columns."""
    def _safe_ohlc(val, fallback=0.0):
        try:
            f = float(val)
            return f if pd.notna(f) else fallback
        except (TypeError, ValueError):
            return fallback

    recent_df = df.reset_index()
    # Determine the date column by checking common names or the first datetime-like column
    _DATE_COLUMN_CANDIDATES = ("Date", "date", "timestamp", "Time", "time", "Datetime")
    date_col = "Date"
    for col in recent_df.columns:
        col_str = str(col)
        if col_str in _DATE_COLUMN_CANDIDATES:
            date_col = col_str
            break
    else:
        # Fallback: use the first object/datetime-like column or first column
        for col in recent_df.columns:
            if hasattr(recent_df[col], "dtype") and "datetime" in str(recent_df[col].dtype).lower():
                date_col = col
                break
        else:
            date_col = recent_df.columns[0]

    chart = []
    ohlc_data = []
    chart_records = recent_df.to_dict("records")
    target_records = chart_records[-ohlc_data_limit:]
    num_records = len(target_records)

    for i, rd in enumerate(target_records):
        dt = rd.get(date_col)
        ts_ms = dt.timestamp() * 1000 if hasattr(dt, "timestamp") else str(dt)
        c_val = _safe_ohlc(rd.get("Close"))

        try:
            vol = (
                int(float(rd.get("Volume", 0)))
                if rd.get("Volume") is not None and pd.notna(rd.get("Volume"))
                else 0
            )
        except (ValueError, TypeError):
            vol = 0

        ohlc_data.append({
            "x": ts_ms, "o": _safe_ohlc(rd.get("Open")),
            "h": _safe_ohlc(rd.get("High")), "l": _safe_ohlc(rd.get("Low")),
            "c": c_val, "v": vol,
        })

        if num_records - i <= chart_data_limit:
            label = dt.strftime("%m/%d") if hasattr(dt, "strftime") else str(dt)
            ma5_val = _safe_ohlc(rd.get("MA5"), fallback=None)
            ma25_val = _safe_ohlc(rd.get("MA25"), fallback=None)
            chart.append({"x": ts_ms, "date": label, "price": c_val,
                          "ma5": ma5_val, "ma25": ma25_val})
    return chart, ohlc_data


def _build_portfolio_metrics(shares, avg_price, avg_fx_rate, currency, current_price):
    """Calculate portfolio value and P&L in JPY."""
    portfolio_val_raw = shares * current_price
    portfolio_pl_raw = (current_price - avg_price) * shares

    if currency == "USD":
        usdjpy_info = app_state.current_indices_cache.get("USDJPY", {})
        current_fx = 150.0
        try:
            if usdjpy_info and usdjpy_info.get("price") not in (None, "--", ""):
                current_fx = float(usdjpy_info["price"])
        except (ValueError, TypeError):
            pass
        value_jpy = portfolio_val_raw * current_fx
        cost_jpy = (shares * avg_price) * (avg_fx_rate if avg_fx_rate is not None else current_fx)
        pl_jpy = value_jpy - cost_jpy
    else:
        value_jpy = portfolio_val_raw
        pl_jpy = portfolio_pl_raw
    return _fmt(value_jpy), _fmt(pl_jpy)


def build_stock_payload(symbol, name_or_dict, market, hist, snapshot_ts_ms=None):
    """銘柄のペイロード辞書を構築する"""
    hist = normalize_history_frame(hist, inplace=True)
    if len(hist) < 1:
        logger.warning(
            "Stock %s: insufficient historical data (len=%d)", symbol, len(hist)
        )
        return None

    name, shares, avg_price, avg_fx_rate = _extract_portfolio_fields(name_or_dict)

    try:
        # Price metrics
        price_fmt, change_fmt, pct_fmt = _compute_price_metrics(hist, symbol)
        if price_fmt is None:
            return None

        # Build MA and chart/ohlc data
        df = hist.copy()
        df["MA5"] = df["Close"].rolling(window=5, min_periods=1).mean()
        df["MA25"] = df["Close"].rolling(window=25, min_periods=1).mean()
        chart, ohlc_data = _build_chart_ohlc_data(df)

        # Stock info
        info = get_stock_info_cached(symbol) or {}
        market_state = "REGULAR" if is_market_open(market) else "CLOSED"
        currency = info.get("currency") or ("JPY" if market == "jp" else "USD")

        # Snapshot timestamp
        snapshot_value = int(snapshot_ts_ms if snapshot_ts_ms is not None else time.time() * 1000)

        # Current price for portfolio calculation
        current_price = float(price_fmt if price_fmt else 0)
        pf_value, pf_pl = _build_portfolio_metrics(
            shares, avg_price, avg_fx_rate, currency, current_price
        )

        return {
            "symbol": symbol,
            "name": choose_display_name(symbol, name, info),
            "market": market,
            "snapshot_ts_ms": snapshot_value,
            "price": price_fmt,
            "change": change_fmt,
            "change_percent": pct_fmt,
            "chart_data": chart,
            "ohlc_data": ohlc_data,
            "high": _fmt(hist["High"].iloc[-1]) if "High" in hist.columns else None,
            "low": _fmt(hist["Low"].iloc[-1]) if "Low" in hist.columns else None,
            "open": _fmt(hist["Open"].iloc[-1]) if "Open" in hist.columns else None,
            "volume": _fmt_vol(hist["Volume"].iloc[-1]) if "Volume" in hist.columns else None,
            "currency": currency,
            "market_state": market_state,
            "shares": shares,
            "avg_price": avg_price,
            "avg_fx_rate": avg_fx_rate,
            "portfolio_value": pf_value,
            "portfolio_pl": pf_pl,
            "sector": info.get("sector") or PREDEFINED_SECTORS.get(symbol, "Other"),
            "industry": info.get("industry") or PREDEFINED_INDUSTRIES.get(symbol, "Other"),
        }
    except (
        KeyError, AttributeError, TypeError, ValueError, pd.errors.EmptyDataError,
    ) as exc:
        logger.error(
            "Stock payload build failed (%s): %s", symbol, exc
        )
        return None
