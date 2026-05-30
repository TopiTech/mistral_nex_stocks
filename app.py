# pylint: disable=missing-class-docstring,missing-function-docstring,too-many-branches,too-many-locals,too-many-statements,too-many-return-statements,too-many-arguments,too-many-positional-arguments
"""Backend application for Mistral NeX Stocks."""

# #region Imports

import copy
import hashlib
import json
import ipaddress
import logging
import math
import unicodedata
import os
import queue
import random
import re
import shutil
import atexit
import secrets
import sys
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, time as dt_time, timezone
from email.utils import parsedate_to_datetime
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Union, Tuple, Set

import pandas as pd
import requests
import yfinance as yf
from cachetools import TTLCache

# ddgs v9.x (deedy5/ddgs) - DuckDuckGo Search
from ddgs import DDGS
from flask import (
    Flask,
    Response,
    g,
    jsonify,
    render_template,
    request,
    stream_with_context,
)
from requests.exceptions import RequestException, Timeout as RequestsTimeout
from curl_cffi.requests.exceptions import Timeout as CurlRequestsTimeout
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)
from werkzeug.exceptions import BadRequest

from config_utils import (
    get_api_credential_state,
    get_langsearch_api_key,
    get_model_badge,
    get_model_name,
    get_mistral_api_key,
    save_api_credentials,
    clear_api_credentials,
)
from error_codes import ErrorCode, get_error_message
import trend_sources as ts

# #endregion Imports

# Reusable HTTP sessions for external APIs (connection pooling)
_mistral_session = requests.Session()

# #region yfinance Session Management

try:
    from keyring.errors import KeyringError
except ImportError:

    class KeyringError(Exception):  # type: ignore[no-redef]
        """Fallback if keyring is not installed."""


# --- yfinance アクセス制限対策 ---
# 複数のユーザーエージェントをローテーションして使用
YFINANCE_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:137.0) Gecko/20100101 Firefox/137.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Safari/605.1.15",
]

# セッション管理クラス（yfinance 1.x系対応）


class YFinanceSessionManager:
    """yfinance用のセッションを管理し、ユーザーエージェントをローテーション

    yfinance 1.x系ではcurl_cffiベースのセッション管理に移行したため、
    直接requests.Sessionを渡す方式から、User-Agentヘッダーのみ管理する方式に変更
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        # Prevent re-initialization on subsequent calls
        if not hasattr(self, "_initialized"):
            with self._lock:
                if not hasattr(self, "_initialized"):
                    self._excluded_until = (
                        {}
                    )  # 429 を受けたセッションの除外期間 {key: float}
                    self._ua_index = 0
                    self._initialized = True

    def get_user_agent(self):
        """ユーザーエージェントをローテーションして取得"""
        with self._lock:
            ua = YFINANCE_USER_AGENTS[self._ua_index]
            self._ua_index = (self._ua_index + 1) % len(YFINANCE_USER_AGENTS)
            return ua

    def mark_rate_limited(self, key="default", duration=300):
        """429エラーが発生したことを記録し、一定期間除外する"""
        with self._lock:
            self._excluded_until[key] = time.time() + duration

    def is_rate_limited(self, key="default"):
        """指定キーがレート制限中かチェック"""
        with self._lock:
            if key in self._excluded_until:
                return time.time() < self._excluded_until[key]
            return False

    def clear_rate_limit(self, key="default"):
        """レート制限状態を解除"""
        with self._lock:
            if key in self._excluded_until:
                self._excluded_until[key] = 0

    def close_all(self):
        """リソースをクリーンアップ"""
        with self._lock:
            self._excluded_until.clear()


# グローバルセッションマネージャー
yf_session_manager = YFinanceSessionManager()

# Ensure global HTTP sessions and managers are closed on process exit to avoid ResourceWarning
def _cleanup_on_exit():
    try:
        if hasattr(_mistral_session, 'close'):
            _mistral_session.close()
    except Exception:
        pass
    try:
        yf_session_manager.close_all()
    except Exception:
        pass

atexit.register(_cleanup_on_exit)

# #endregion yfinance Session Management

# #region Logging Configuration

app = Flask(__name__)

# セッション暗号化用のシークレットキー（個人利用向けに環境変数または自動生成）
# 本番環境では環境変数 FLASK_SECRET_KEY を設定することを強く推奨
_flask_secret = os.environ.get("FLASK_SECRET_KEY")
if _flask_secret:
    if len(_flask_secret) < 32:
        raise ValueError("FLASK_SECRET_KEY must be at least 32 characters for security")
    app.secret_key = _flask_secret
else:
    app.logger.warning(
        "FLASK_SECRET_KEY not set. Using auto-generated key. "
        "Sessions will not persist across server restarts."
    )
    app.secret_key = secrets.token_hex(32)

# セッション設定の強化（個人利用向け）
# 個人利用向けでlocalhostのみの利用を想定しているため、SESSION_COOKIE_SECUREは
# ローカル開発環境対応とする（HTTP接続許可）。本番環境ではTrue推奨
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,  # JavaScriptからアクセス不可
    SESSION_COOKIE_SAMESITE="Lax",  # CSRF対策
    SESSION_COOKIE_SECURE=os.environ.get("MNS_PROD", "").lower() in ("1", "true", "yes"),  # Production: enable Secure cookie when MNS_PROD is set
    PERMANENT_SESSION_LIFETIME=timedelta(seconds=3600),  # 1時間で期限切れ
)

# Content Security Policy: default to Report-Only so we can monitor before enforcing.
# Toggle enforcement with the CSP_ENFORCE environment variable (true/1/yes to enforce).
CSP_DEFAULT_POLICY = os.environ.get(
    "CSP_DEFAULT_POLICY",
    "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net https:; object-src 'none'; frame-ancestors 'none'; base-uri 'self';",
)
CSP_ENFORCE = os.environ.get("CSP_ENFORCE", "false").lower() in ("1", "true", "yes")

@app.after_request
def add_csp_headers(response):
    try:
        header_name = "Content-Security-Policy" if CSP_ENFORCE else "Content-Security-Policy-Report-Only"
        # Don't overwrite an existing CSP header set by other parts of the app
        if header_name not in response.headers:
            response.headers[header_name] = CSP_DEFAULT_POLICY
    except Exception:
        app.logger.exception("Failed to set CSP header")
    return response

if sys.version_info < (3, 9):
    raise RuntimeError("Python 3.9+ is required for this application")

# --- ログローテーション設定 (5MB × 最大3ファイル) ---
LOG_LEVEL_NAME = str(os.environ.get("BACKEND_LOG_LEVEL", "INFO") or "INFO").upper()
LOG_LEVEL = getattr(logging, LOG_LEVEL_NAME, logging.INFO)

# --- セキュリティ設定 ---
# 本番環境では必ず環境変数から強力なシークレットキーを設定すること
# 個人利用向け: 自動生成キーはセッションがサーバー再起動時に失われるため注意
if not os.environ.get("FLASK_SECRET_KEY"):
    import warnings

    warnings.warn(
        "FLASK_SECRET_KEY is not set. Using auto-generated key for development only. "
        "Set a strong secret key in production to prevent session tampering.",
        RuntimeWarning,
        stacklevel=2,
    )


def _get_backend_port(default=5000):
    port_text = os.environ.get("MNS_BACKEND_PORT", "").strip()
    if not port_text:
        return default
    try:
        port = int(port_text)
        if 1 <= port <= 65535:
            return port
    except ValueError:
        pass
    app.logger.warning(
        "Invalid MNS_BACKEND_PORT value %r; falling back to default %s",
        port_text,
        default,
    )
    return default


BACKEND_PORT = _get_backend_port()
IMPORTANT_INFO_PATTERNS = (
    "REQ start",
    "REQ end",
    "api_news start",
    "api_analyze input",
    "News bundle refresh",
    "LangSearch used:",
    "DDGS fallback used:",
    "DDGS results:",
    "News trends async refresh completed",
)


class BackendLogFilter(logging.Filter):
    def filter(self, record):
        if record.levelno >= logging.WARNING:
            return True
        if record.levelno < logging.INFO:
            return LOG_LEVEL <= record.levelno
        msg = record.getMessage()
        return any(pattern in msg for pattern in IMPORTANT_INFO_PATTERNS)


_log_file = Path(__file__).resolve().parent / "backend.log"
_rotating_handler = RotatingFileHandler(
    str(_log_file),
    maxBytes=5 * 1024 * 1024,  # 5MB
    backupCount=3,
    encoding="utf-8",
)
_rotating_handler.setLevel(LOG_LEVEL)
_rotating_handler.addFilter(BackendLogFilter())
_rotating_handler.setFormatter(
    logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
)
logging.getLogger().addHandler(_rotating_handler)
logging.getLogger().setLevel(LOG_LEVEL)
app.logger.addHandler(_rotating_handler)
app.logger.setLevel(LOG_LEVEL)
app.logger.propagate = False


class PollingFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        # Filter out successful polling logs
        if " 200 -" in msg and any(
            x in msg for x in ["GET /api/indices", "GET /api/health", "GET /api/stocks"]
        ):
            return False
        return True


logging.getLogger("werkzeug").addFilter(PollingFilter())


# --- Application State Groups ---
class ExecutionState:
    """スレッドプールとバックグラウンドタスクの実行を管理するクラス。"""

    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=5)
        self.news_executor = ThreadPoolExecutor(max_workers=4)
        self.sync_refresh_executor = ThreadPoolExecutor(max_workers=1)

    def shutdown(self):
        """Shut down all executors safely."""
        for ex in [
            self.executor,
            self.news_executor,
            self.sync_refresh_executor,
        ]:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                ex.shutdown(wait=False)


# #endregion Application State Management


class MarketDataState:
    """銘柄データ、市場状況、およびyfinanceのレート制限を管理するクラス。"""

    def __init__(self):
        self.user_us = {}
        self.user_jp = {}
        self.user_idx = {}
        self.user_stocks_lock = threading.RLock()
        self.last_modified_ns = 0
        self.current_stocks_cache = {"us": [], "jp": [], "idx": []}
        self.target_stocks_cache = {"us": [], "jp": [], "idx": []}
        self.current_indices_cache = {}
        self.target_indices_cache = {}
        self.is_syncing = False
        self.is_syncing_lock = threading.Lock()
        self.sync_scheduled = False
        self.sync_schedule_lock = threading.Lock()
        self.market_status_cache = {"us": None, "jp": None, "idx": None}
        self.market_status_lock = threading.Lock()

        # History circuit breaker
        self.history_circuit_lock = threading.Lock()
        self.history_circuit_state = (
            {}
        )  # {symbol: {"timeout_streak": int, "open_until": float}}

        # yfinance rate limiting
        self.yfinance_lock = threading.Lock()
        self.is_yfinance_rate_limited = False
        self.yfinance_rate_limit_until = 0.0
        self.yfinance_last_request_ts = 0.0
        self.yfinance_min_interval_sec = 0.8
        self.yfinance_429_streak = 0
        self.yfinance_429_backoff_multiplier = 2.0
        self.yfinance_max_backoff_sec = 60.0


class AIState:
    """Mistral, LangSearch, およびチャット履歴の状態を管理するクラス。"""

    def __init__(self):
        self.mistral_call_semaphore = threading.Semaphore(1)
        self.mistral_cooldown_lock = threading.Lock()
        self.mistral_next_allowed_ts = 0.0
        self.mistral_429_streak = 0
        self.mistral_last_call_ts = 0.0
        self.mistral_response_cache = TTLCache(maxsize=128, ttl=240)
        self.mistral_response_lock = threading.Lock()

        self.langsearch_rate_lock = threading.Lock()
        self.langsearch_next_allowed_ts = 0.0
        self.langsearch_min_interval_sec = 2.0  # Increased from 1.25 to be safer
        self.langsearch_429_cooldown_sec = 90.0  # Increased from 60.0 to ensure window reset

        self.trends_refresh_inflight = set()
        self.trends_refresh_lock = threading.Lock()

        self.chat_history = OrderedDict()
        self.chat_history_lock = threading.Lock()
        self.max_history = 50

    def add_chat_history(self, key, message):
        """チャット履歴を追加（最大50エントリ制限）"""
        with self.chat_history_lock:
            if key not in self.chat_history:
                if len(self.chat_history) >= self.max_history:
                    self.chat_history.popitem(last=False)
            self.chat_history[key] = message
            self.chat_history.move_to_end(key)


class CacheState:
    """グローバルなTTLCacheとフェッチイベントを管理するクラス。"""

    def __init__(self):
        self.caches = {}  # Map of duration -> TTLCache
        self.cache_lock = threading.Lock()
        self.file_lock = threading.Lock()
        self.fetch_events = {}
        self.fetch_events_lock = threading.Lock()
        self.sse_data_lock = threading.Lock()


class AppState:
    """論理的にグループ化されたレガシープロキシをサポートする分散型アプリケーション状態管理クラス。"""

    execution: ExecutionState
    market: MarketDataState
    ai: AIState
    cache: CacheState

    def __init__(self):
        self.execution = ExecutionState()
        self.market = MarketDataState()
        self.ai = AIState()
        self.cache = CacheState()

        # Consolidated globals
        self.sse_announcer = None
        self._langsearch_session = None
        self._extension_origins_cache = set()
        self._extension_origins_cache_ts = 0.0
        self._extension_origins_cache_lock = threading.Lock()
        self._extension_manifest_status = {"ok": True, "error": ""}
        self.EXTENSION_MANIFEST_ERROR_LOGGED = False
        self._EXTENSION_ORIGINS_CACHE_TTL_SEC = 30.0

    def __getattr__(self, name: str):
        """Legacy attribute proxy to sub-groups for backward compatibility."""
        for group in [self.execution, self.market, self.ai, self.cache]:
            if hasattr(group, name):
                return getattr(group, name)
        raise AttributeError(
            f"'{self.__class__.__name__}' object has no attribute '{name}'"
        )

    def __setattr__(self, name: str, value: any):
        """Direct assignment for groups, otherwise try to proxy to the correct group."""
        if name in ("execution", "market", "ai", "cache"):
            super().__setattr__(name, value)
            return

        for group in [self.execution, self.market, self.ai, self.cache]:
            if hasattr(group, name):
                setattr(group, name, value)
                return
        super().__setattr__(name, value)

    def __delattr__(self, name: str):
        """Proxy deletion of legacy attributes to the proper state group."""
        if name in ("execution", "market", "ai", "cache"):
            super().__delattr__(name)
            return

        for group in [self.execution, self.market, self.ai, self.cache]:
            if hasattr(group, name):
                try:
                    delattr(group, name)
                    return
                except AttributeError:
                    break
        super().__delattr__(name)

    def shutdown_executors(self):
        """Clean up background resources."""
        self.execution.shutdown()
        try:
            yf_session_manager.close_all()
        except (RuntimeError, AttributeError, IOError):
            pass


app_state = AppState()
app_state._langsearch_session = requests.Session()
atexit.register(app_state.shutdown_executors)


DETAILED_API_LOG_PATHS = {
    "/api/chat",
    "/api/news",
    "/api/analyze-v2",
    "/api/credentials",
    "/api/shutdown",
}


def _short_text(value, limit=160):
    text = str(value or "").strip().replace("\n", " ")
    return text if len(text) <= limit else (text[:limit] + "...")


def _token_fingerprint(token):
    """トークンの安全なフィンガープリント生成（SHA256ハッシュ）"""
    t = (token or "").strip()
    if not t:
        return "none"
    # ハッシュ長を16文字に増やし、衝突耐性を向上
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


MAX_JSON_SIZE = 1024 * 1024  # 1MB


def _parse_json_request():
    """Parse a JSON request body and return an object or None for malformed JSON."""
    content_length = request.content_length
    if content_length and content_length > MAX_JSON_SIZE:
        app.logger.warning(
            "JSON payload too large id=%s size=%s max=%s",
            getattr(g, "request_id", "-"),
            content_length,
            MAX_JSON_SIZE,
        )
        return None

    try:
        payload = request.get_json(force=False, silent=False)
    except BadRequest as exc:
        app.logger.warning(
            "Invalid JSON payload id=%s err=%s",
            getattr(g, "request_id", "-"),
            _short_text(exc),
        )
        return None

    if payload is None:
        return {}
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
    ]
    sanitized = str(error_msg)
    for pattern in sensitive_patterns:
        sanitized = re.sub(pattern, "[REDACTED]", sanitized, flags=re.IGNORECASE)
    return sanitized


def sanitize_cache_key(key):
    """キャッシュキーを安全にサニタイズ"""
    if not isinstance(key, str):
        key = str(key)
    # 危険な文字を削除
    sanitized = re.sub(r"[^\w\-:._]", "_", key)
    # 長すぎるキーを制限
    return sanitized[:256]


@app.before_request
def _log_request_start():
    """Log the start of an incoming request with a unique request ID."""
    g.request_start_ts = time.time()
    g.request_id = uuid.uuid4().hex[:10]

    # Quiet by default: only emit request traces when INFO logging is explicitly enabled.
    if LOG_LEVEL <= logging.INFO and request.path in DETAILED_API_LOG_PATHS:
        app.logger.info(
            "REQ start id=%s method=%s path=%s remote=%s origin=%s ua=%s",
            g.request_id,
            request.method,
            request.path,
            request.remote_addr,
            _short_text(request.headers.get("Origin"), 80),
            _short_text(request.headers.get("User-Agent"), 120),
        )


def _normalize_extension_origin(raw):
    """Normalize a raw extension origin or bare extension ID.

    Supports both full chrome-extension:// URLs and raw 32-character IDs.
    Normalizes IDs to lowercase for consistent matching.
    """
    if not raw:
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
        if (now - app_state._extension_origins_cache_ts) < app_state._EXTENSION_ORIGINS_CACHE_TTL_SEC:
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
        pass
    except json.JSONDecodeError as exc:
        app_state._extension_manifest_status["ok"] = False
        app_state._extension_manifest_status["error"] = f"manifest_json_decode_error: {exc}"
        if not app_state.EXTENSION_MANIFEST_ERROR_LOGGED:
            app.logger.warning("Manifest JSON decode error: %s", exc)
            app_state.EXTENSION_MANIFEST_ERROR_LOGGED = True
    except (IOError, PermissionError) as exc:
        app_state._extension_manifest_status["ok"] = False
        app_state._extension_manifest_status["error"] = f"manifest_load_error: {exc}"
        if not app_state.EXTENSION_MANIFEST_ERROR_LOGGED:
            app.logger.warning("Failed to load extension origins: %s", exc)
            app_state.EXTENSION_MANIFEST_ERROR_LOGGED = True

    # Success path: clear one-time error suppression and refresh cache.
    if app_state._extension_manifest_status.get("ok"):
        app_state.EXTENSION_MANIFEST_ERROR_LOGGED = False
    with app_state._extension_origins_cache_lock:
        app_state._extension_origins_cache.clear()
        app_state._extension_origins_cache.update(origins)
        app_state._extension_origins_cache_ts = now

    return origins


_BASE_ALLOWED_CORS_ORIGINS = {
    f"http://localhost:{BACKEND_PORT}",
    f"http://127.0.0.1:{BACKEND_PORT}",
}


def get_allowed_cors_origins():
    """Retrieve the set of allowed CORS origins from constants and dynamic sources."""
    origins = {origin.rstrip("/") for origin in _BASE_ALLOWED_CORS_ORIGINS}
    origins.update(_load_allowed_extension_origins())
    return origins


SYMBOL_PATTERN = re.compile(r"^[A-Z0-9^][A-Z0-9._\-^=]{0,14}$")


@app.after_request
def add_extension_cors_headers(response):
    """Inject CORS and security headers into outgoing responses."""
    allowed_origins = {origin.rstrip("/") for origin in get_allowed_cors_origins()}
    origin = (request.headers.get("Origin") or "").strip().rstrip("/")
    if origin and origin in allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = origin

    response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Headers"] = (
        "Content-Type, Authorization, X-LangSearch-Key"
    )
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    response.headers["Access-Control-Max-Age"] = "600"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # HSTSはHTTPS接続時のみ適用（localhostでは送信しない）
    if request.is_secure or (request.headers.get("X-Forwarded-Proto") == "https"):
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )

    # Content Security Policy: Prevent XSS attacks
    # Allow Chart.js and fonts from trusted CDNs
    # Note: SSE requires EventSource which connects to same origin
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' https://cdn.jsdelivr.net https:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "img-src 'self' data: https:; "
        "font-src 'self' https://fonts.gstatic.com; "
        f"connect-src 'self' http://localhost:{BACKEND_PORT} http://127.0.0.1:{BACKEND_PORT} https://api.mistral.ai https://api.langsearch.com; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )

    req_id = getattr(g, "request_id", "-")
    started = getattr(g, "request_start_ts", None)
    elapsed_ms = (
        int((time.time() - started) * 1000) if isinstance(started, (int, float)) else -1
    )
    status_code = int(response.status_code or 0)
    if status_code >= 400:
        app.logger.warning(
            "REQ end id=%s method=%s path=%s status=%s elapsed_ms=%s",
            req_id,
            request.method,
            request.path,
            response.status_code,
            elapsed_ms,
        )
    elif LOG_LEVEL <= logging.INFO and request.path in DETAILED_API_LOG_PATHS:
        app.logger.info(
            "REQ end id=%s method=%s path=%s status=%s elapsed_ms=%s",
            req_id,
            request.method,
            request.path,
            response.status_code,
            elapsed_ms,
        )

    return response


@app.route("/api/credentials", methods=["GET", "POST", "DELETE", "OPTIONS"])
def api_credentials():
    """Handles API credential retrieval, updating, and removal."""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    if not _is_local_request(request):
        app.logger.warning(
            "Credentials access denied (non-local) id=%s remote=%s",
            getattr(g, "request_id", "-"),
            request.remote_addr,
        )
        return jsonify({"ok": False, "error": "forbidden"}), 403

    if request.method == "GET":
        app.logger.info(
            "Credentials state requested id=%s", getattr(g, "request_id", "-")
        )
        return jsonify({"ok": True, **get_api_credential_state()})

    if request.method == "DELETE":
        clear_api_credentials()
        app.logger.info("Credentials cleared id=%s", getattr(g, "request_id", "-"))
        return jsonify({"ok": True, **get_api_credential_state()})

    data = _parse_json_request()
    if data is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )
    mistral_api_key = (data.get("mistral_api_key") or "").strip()
    langsearch_api_key = (data.get("langsearch_api_key") or "").strip()

    if not _is_valid_api_key(mistral_api_key):
        app.logger.warning(
            "Credentials save rejected id=%s reason=invalid_mistral_key len=%s",
            getattr(g, "request_id", "-"),
            len(mistral_api_key or ""),
        )
        return error_response(
            ErrorCode.MISSING_REQUIRED_FIELD,
            details={"fields": ["mistral_api_key"]},
        )

    if langsearch_api_key and not _is_valid_api_key(langsearch_api_key):
        app.logger.warning(
            "Credentials save rejected id=%s reason=invalid_langsearch_key len=%s",
            getattr(g, "request_id", "-"),
            len(langsearch_api_key or ""),
        )
        return error_response(
            ErrorCode.UNSAFE_INPUT,
            details={"fields": ["langsearch_api_key"]},
        )

    try:
        save_api_credentials(
            mistral_api_key=mistral_api_key,
            langsearch_api_key=langsearch_api_key,
        )
    except RuntimeError as exc:
        app.logger.warning(
            "Credentials save failed id=%s reason=%s",
            getattr(g, "request_id", "-"),
            exc,
        )
        return error_response(
            ErrorCode.CONFIG_ERROR,
            status_code=500,
            details={"reason": str(exc)},
        )

    app.logger.info(
        "Credentials saved id=%s mistral=%s langsearch=%s",
        getattr(g, "request_id", "-"),
        _token_fingerprint(mistral_api_key),
        _token_fingerprint(langsearch_api_key),
    )
    return jsonify({"ok": True, **get_api_credential_state()})


# ------------------------------
# Base Directory & Settings
# ------------------------------
BASE_DIR = Path(__file__).resolve().parent

# Mistral API Settings
MISTRAL_BASE_URL = "https://api.mistral.ai/v1"
LANGSEARCH_BASE_URL = "https://api.langsearch.com"
LANGSEARCH_WEB_SEARCH_ENDPOINT = f"{LANGSEARCH_BASE_URL}/v1/web-search"
LANGSEARCH_TIMEOUT = (5.0, 10.0)
USER_STOCKS_FILE = str(BASE_DIR / "user_stocks.json")
CACHE_DURATION = 30
# Executor は AppState 側で一元管理し、終了処理の漏れを防ぐ


# Per-key events to prevent cache stampede (multiple threads fetching same key simultaneously)
# --- エラーレスポンスヘルパー ---
def error_response(error_code: ErrorCode, status_code: int = 400, details: dict = None):
    """統一されたエラーレスポンスを返す"""
    message = get_error_message(error_code, lang="ja")
    return (
        jsonify(
            {
                "error": message,
                "error_flag": True,
                "error_code": int(error_code),
                "message": message,
                "details": details or {},
            }
        ),
        status_code,
    )


def _parse_datetime_to_utc(value):
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    # Unix timestamp
    if text.isdigit():
        try:
            return datetime.fromtimestamp(int(text), timezone.utc)
        except (ValueError, OverflowError):
            pass

    # RFC 2822 / RFC 1123 and other common formats
    try:
        parsed = parsedate_to_datetime(text)
        if parsed is not None:
            return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        pass

    # Basic UTC timestamp format without separators
    try:
        return datetime.strptime(text, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        pass

    # ISO 8601 variants
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _market_news_freshness_policy(market="us"):
    """Returns (max_age_hours, allow_undated_limit) for news filtering."""
    if str(market).strip().lower() == "jp":
        return 24, 1
    return 48, 3


def _filter_recent_market_news_items(
    items, max_age_hours=48, allow_undated_limit=2, max_items=10
):
    """Filters news items based on age and limits results."""
    if not isinstance(items, list):
        return []

    now = datetime.now(timezone.utc)
    filtered = []
    undated_remaining = max(0, int(allow_undated_limit))
    max_items = max(1, int(max_items))

    for item in items:
        if len(filtered) >= max_items:
            break

        if not isinstance(item, dict):
            continue

        date_text = str(item.get("date") or "").strip()
        dt = _parse_datetime_to_utc(date_text)
        if dt is not None:
            age = now - dt
            if age.total_seconds() <= max_age_hours * 3600:
                filtered.append(item)
            continue

        if undated_remaining > 0:
            filtered.append(item)
            undated_remaining -= 1

    return filtered


# #region Constants
# #--- タイムアウト設定 ---
YFINANCE_TIMEOUT_BATCH = 20  # 20秒（batch download用）
YFINANCE_TIMEOUT_SINGLE = 6  # 6秒（単一取得用：フォールバック時の短縮）
YFINANCE_MAX_RETRIES = 2  # 最大リトライ回数
YFINANCE_RETRY_WAIT = 1  # リトライ前の待機秒数
HISTORY_CIRCUIT_BREAKER_THRESHOLD = 3  # timeout連続回数で開放
HISTORY_CIRCUIT_BREAKER_OPEN_SEC = 20  # circuit open継続秒
NEWS_CONTEXT_WAIT_TIMEOUT = 40  # /api/news 収集待機秒数（初回更新の取りこぼしを減らす）
NEWS_PARSE_LOG_SNIPPET_CHARS = 1200
ANALYZE_RESEARCH_CONTEXT_MAX_CHARS = 2200  # mistral-smallでも安定しやすい範囲で拡張
PORTFOLIO_SHARES_MAX = 1_000_000_000
PORTFOLIO_AVG_PRICE_MAX = 1_000_000_000
PORTFOLIO_TOTAL_VALUE_MAX = 1_000_000_000_000

# #endregion Constants

# #region Rate Limiting
# シンプルなIPベースレート制限（メモリ内）
_rate_limit_store = {}
_rate_limit_lock = threading.Lock()
_RATE_LIMIT_CLEANUP_INTERVAL = 300  # 5分ごとに期限切れエントリをクリーンアップ
_rate_limit_last_cleanup = time.time()


def _cleanup_rate_limit_store():
    """期限切れのレート制限エントリを削除してメモリリークを防止"""
    current_time = time.time()
    keys_to_delete = []
    for key, timestamps in _rate_limit_store.items():
        # ウィンドウ外のタイムスタンプを除去
        filtered = [
            t for t in timestamps if current_time - t < 120
        ]  # 最大2分ウィンドウを想定
        if filtered:
            _rate_limit_store[key] = filtered
        else:
            keys_to_delete.append(key)
    for key in keys_to_delete:
        del _rate_limit_store[key]


def rate_limit(max_requests=60, window_seconds=60):
    """シンプルなIPベースレート制限デコレータ（個人利用向け）"""

    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            # localhostからのアクセスは制限しない
            remote_addr = request.remote_addr or ""
            if remote_addr in ("127.0.0.1", "localhost", "::1"):
                return f(*args, **kwargs)

            current_time = time.time()
            key = f"{remote_addr}:{request.endpoint}"

            with _rate_limit_lock:
                # 定期的にクリーンアップしてメモリリークを防止
                global _rate_limit_last_cleanup
                if (
                    current_time - _rate_limit_last_cleanup
                    > _RATE_LIMIT_CLEANUP_INTERVAL
                ):
                    _cleanup_rate_limit_store()
                    _rate_limit_last_cleanup = current_time

                if key not in _rate_limit_store:
                    _rate_limit_store[key] = []

                # ウィンドウ内のリクエストのみ保持
                _rate_limit_store[key] = [
                    t
                    for t in _rate_limit_store[key]
                    if current_time - t < window_seconds
                ]

                if len(_rate_limit_store[key]) >= max_requests:
                    retry_after = max(
                        0,
                        int(
                            window_seconds - (current_time - _rate_limit_store[key][0])
                        ),
                    )
                    return (
                        jsonify(
                            {
                                "error": "レート制限を超過しました。しばらく後にお試しください",
                                "error_flag": True,
                                "error_code": int(ErrorCode.API_RATE_LIMITED),
                                "message": "レート制限を超過しました。しばらく後にお試しください",
                                "details": {"retry_after": retry_after},
                            }
                        ),
                        429,
                    )

                _rate_limit_store[key].append(current_time)

            return f(*args, **kwargs)

        return wrapper

    return decorator


# #endregion Rate Limiting


# #region yfinance Safety Wrappers
def safe_get_ticker(symbol):
    """
    Wrap yf.Ticker instantiation with defensive error handling.
    Request timeouts are enforced where yfinance performs network I/O.

    yfinance 1.x系対応: user_agentパラメータを明示的に渡す
    """
    try:
        # yfinance 1.x系(1.3.0以降)ではcurl_cffiによる自動ブラウザ偽装が
        # 標準となったため、user_agentパラメータは削除されました。
        return yf.Ticker(symbol)
    except (ValueError, TypeError, AttributeError, RuntimeError, OSError) as exc:
        app.logger.debug("yf.Ticker creation failed for %s: %s", symbol, exc)
        return None


# #endregion yfinance Safety Wrappers


def schedule_news_warmup():
    """Warm up news/trends caches in background to reduce first refresh failures after startup."""
    try:
        langsearch_api_key = get_langsearch_api_key() or ""
    except (KeyringError, RuntimeError):
        langsearch_api_key = ""
    search_source_hint = "ls" if langsearch_api_key else "ddgs"

    def _job():
        try:
            get_cached_context_with_negative_cache(
                f"market_news_context_us_{search_source_hint}",
                lambda: collect_market_news_context(
                    "us", langsearch_api_key=langsearch_api_key
                ),
                300,
                90,
                True,
            )
            get_cached_context_with_negative_cache(
                f"market_news_context_jp_{search_source_hint}",
                lambda: collect_market_news_context(
                    "jp", langsearch_api_key=langsearch_api_key
                ),
                300,
                90,
                True,
            )
            collect_market_trending_titles("us", 8, langsearch_api_key)
            collect_market_trending_titles("jp", 8, langsearch_api_key)
        except (IOError, RuntimeError, RequestException) as exc:
            app.logger.warning("News warmup failed: %s", exc)

    try:
        app_state.execution.news_executor.submit(_job)
    except (RuntimeError, AttributeError) as exc:
        app.logger.warning("Failed to schedule news warmup: %s", exc)


# ------------------------------
# Default Stocks
# ------------------------------
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


# ------------------------------
# Popular Stocks for Heatmap
# ------------------------------
POPULAR_US = [
    "AAPL",
    "MSFT",
    "GOOGL",
    "AMZN",
    "NVDA",
    "TSLA",
    "META",
    "NFLX",
    "AVGO",
    "ADBE",
    "COST",
    "PEP",
    "CSCO",
    "INTC",
    "TMUS",
    "CMCSA",
    "AMD",
    "TXN",
    "HON",
    "QCOM",
    "BRK-B",
    "V",
    "JNJ",
    "WMT",
    "JPM",
    "PG",
    "MA",
    "UNH",
    "HD",
    "XOM",
]

POPULAR_JP = [
    "7203.T",
    "6758.T",
    "9984.T",
    "8306.T",
    "6861.T",
    "6098.T",
    "9432.T",
    "8035.T",
    "4502.T",
    "7974.T",
    "6501.T",
    "6954.T",
    "8001.T",
    "8058.T",
    "8316.T",
    "4063.T",
    "6702.T",
    "6902.T",
    "6367.T",
    "4568.T",
    "6503.T",
    "8766.T",
    "6273.T",
    "6178.T",
    "9022.T",
    "7267.T",
    "8591.T",
    "6301.T",
    "4519.T",
    "6701.T",
]


# ------------------------------
# Cache Utilities
# ------------------------------
def get_cached(key, fetch_func, duration=CACHE_DURATION, valid_func=None):
    """
    キャッシュ取得かつスタンペード防止

    複数の同時リクエストが同じキーを要求した場合、最初の1つだけ fetch を実行し、
    他は結果を待機。これにより上流 API への多重呼び出しを防止。

    現在の実装: 多段階タイムアウト (60s → 30s → 15s → fallback)
    - 複雑さ: ★★★★☆ 複数段階の待機で予測困難
    - 利点: 遅いバックエンド対応、transient 遅延の耐性

    将来の改善案 (シンプル設計):
      단일 5秒タイムアウト → fallback fetch
      メリット: 予測可能、デバッグ簡易
      デメリット: 遅いバックエンド時に fallback が増加
    """
    # キャッシュキーのサニタイズ（セキュリティ強化）
    safe_key = sanitize_cache_key(key)

    # Fast path: check if already cached
    with app_state.cache_lock:
        if duration not in app_state.caches:
            app_state.caches[duration] = TTLCache(maxsize=128, ttl=duration)
        if safe_key in app_state.caches[duration]:
            return app_state.caches[duration][safe_key]

    # Slow path: serialize concurrent fetches for the same key (stampede prevention)
    with app_state.fetch_events_lock:
        if safe_key in app_state.fetch_events:
            ev = app_state.fetch_events[safe_key]
            is_fetcher = False
        else:
            ev = threading.Event()
            app_state.fetch_events[safe_key] = ev
            is_fetcher = True

    if not is_fetcher:
        # Wait briefly for the primary fetcher, then fall back to an independent fetch.
        ev.wait(timeout=10)
        with app_state.cache_lock:
            cache = app_state.caches.get(duration, {})
            if safe_key in cache:
                return cache[safe_key]
        app.logger.debug(
            "Cache stampede fallback fetch for key=%s duration=%s", safe_key, duration
        )
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


def _market_trends_cache_key(market: str, search_source_hint: str) -> str:
    return f"market_trends_{market}_{search_source_hint}"


def _build_market_trending_titles(market: str, langsearch_api_key: str) -> list[str]:
    try:
        trend_target = 12
        region, queries = _market_ddgs_queries(market)
        ts_titles = ts.collect_market_trending_titles(market, count=trend_target)
        search_items = _collect_langsearch_items(
            queries,
            api_key=langsearch_api_key,
            timelimit="d",
            max_results=4,
            limit=12,
            query_limit=4,
        )
        if search_items:
            app.logger.info(
                "LangSearch used: context=market_trending market=%s items=%s",
                market,
                len(search_items),
            )
        else:
            reason = "missing_api_key" if not langsearch_api_key else "empty_or_error"
            app.logger.info(
                "DDGS fallback used: context=market_trending market=%s reason=%s",
                market,
                reason,
            )
            search_items = _collect_ddgs_items(
                queries, region, "d", news_n=3, text_n=2, limit=12, query_limit=4
            )
            app.logger.info(
                "DDGS results: context=market_trending market=%s items=%s",
                market,
                len(search_items),
            )

        search_titles = _extract_trending_titles_from_items(
            search_items, count=trend_target
        )
        merged_titles = []
        seen = set()
        for title in list(ts_titles) + list(search_titles):
            t = str(title or "").strip()
            key = t.lower()
            if not t or key in seen:
                continue
            seen.add(key)
            merged_titles.append(t)
            if len(merged_titles) >= trend_target:
                break
        return merged_titles
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        app.logger.error("Trend format error for AI: %s", exc)
        return []


def _schedule_market_trends_refresh_async(
    market: str, search_source_hint: str, langsearch_api_key: str
) -> bool:
    cache_key = _market_trends_cache_key(market, search_source_hint)

    with app_state.trends_refresh_lock:
        if cache_key in app_state.trends_refresh_inflight:
            return False
        app_state.trends_refresh_inflight.add(cache_key)

    def _job():
        try:
            trend_titles = _build_market_trending_titles(market, langsearch_api_key)
            _set_cached_value(cache_key, trend_titles, duration=300)
            app.logger.info(
                "News trends async refresh completed: market=%s source=%s cache_key=%s items=%s",
                market,
                search_source_hint,
                cache_key,
                len(trend_titles),
            )
        except (RuntimeError, RequestException, ValueError) as exc:
            app.logger.warning(
                "News trends async refresh failed: market=%s source=%s error=%s",
                market,
                search_source_hint,
                exc,
            )
        finally:
            with app_state.trends_refresh_lock:
                app_state.trends_refresh_inflight.discard(cache_key)

    app_state.executor.submit(_job)
    return True


def _get_market_trending_titles(
    market: str, search_source_hint: str, langsearch_api_key: str
) -> list[str]:
    cache_key = _market_trends_cache_key(market, search_source_hint)
    cached = _get_cached_value(cache_key, duration=300, default=None)

    if isinstance(cached, list) and cached:
        return cached
    if isinstance(cached, str) and cached.strip():
        return [t.strip() for t in cached.split("、") if t.strip()]

    app.logger.info(
        "Market trending cache miss, building synchronously: market=%s source=%s",
        market,
        search_source_hint,
    )
    trend_titles = _build_market_trending_titles(market, langsearch_api_key)
    if trend_titles:
        _set_cached_value(cache_key, trend_titles, duration=300)
        return trend_titles

    started = _schedule_market_trends_refresh_async(
        market, search_source_hint, langsearch_api_key
    )
    app.logger.info(
        "Market trending refresh %s after cache miss: market=%s source=%s",
        "started" if started else "already-running",
        market,
        search_source_hint,
    )
    return []


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


# ------------------------------
# User Stock Save/Load
# ------------------------------
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
                data = json.load(f)
            if not isinstance(data, dict):
                app.logger.error(
                    "Failed to load user stocks: unexpected JSON root type %s",
                    type(data).__name__,
                )
                data = {}
            app_state.user_us = data.get("us", {}) or {}
            app_state.user_jp = data.get("jp", {}) or {}
            app_state.user_idx = data.get("idx", {}) or {}
            app_state.last_modified_ns = mtime_ns
    except (IOError, OSError, json.JSONDecodeError) as exc:
        app.logger.error("Failed to load user stocks: %s", exc)


def save_user_stocks():
    """ユーザーの銘柄設定をファイルに保存する。"""
    with app_state.user_stocks_lock:
        data = {
            "us": copy.deepcopy(app_state.user_us),
            "jp": copy.deepcopy(app_state.user_jp),
            "idx": copy.deepcopy(app_state.user_idx),
        }
        with app_state.file_lock:
            # 既存のデータがあればバックアップを作成 (.bak)
            if os.path.exists(USER_STOCKS_FILE):
                try:
                    shutil.copy2(USER_STOCKS_FILE, USER_STOCKS_FILE + ".bak")
                except (IOError, PermissionError, OSError) as e:
                    app.logger.warning(
                        "User stocks backup failed; continuing save without backup: %s",
                        e,
                    )

            tmp = USER_STOCKS_FILE + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, USER_STOCKS_FILE)
                app_state.last_modified_ns = os.stat(USER_STOCKS_FILE).st_mtime_ns
            except (IOError, OSError) as e:
                app.logger.error("Failed to save user stocks to %s: %s", tmp, e)
                raise


load_user_stocks()


# ------------------------------
# API Key / Response Helpers
# ------------------------------
def extract_api_key(req):
    """リクエストからMistral APIキーを抽出する。"""
    # Persisted key is authoritative to avoid stale browser-side tokens
    # overriding valid backend state.
    stored = get_mistral_api_key()
    if stored:
        app.logger.info(
            "Mistral key source=stored fp=%s id=%s",
            _token_fingerprint(stored),
            getattr(g, "request_id", "-"),
        )
        return stored

    try:
        auth = req.headers.get("Authorization", "")
        if not auth:
            app.logger.warning(
                "Mistral key missing id=%s", getattr(g, "request_id", "-")
            )
            return ""

        # Safely parse Bearer token
        if not auth.startswith("Bearer "):
            app.logger.warning(
                "Mistral key invalid auth scheme id=%s", getattr(g, "request_id", "-")
            )
            return ""

        token = auth[7:].strip()
        if token:
            app.logger.info(
                "Mistral key source=header fp=%s id=%s",
                _token_fingerprint(token),
                getattr(g, "request_id", "-"),
            )
        else:
            app.logger.warning(
                "Mistral key empty bearer token id=%s", getattr(g, "request_id", "-")
            )
        return token
    except (KeyError, AttributeError, ValueError) as exc:
        app.logger.error(
            "Mistral key extraction error id=%s: %s", getattr(g, "request_id", "-"), exc
        )
        return ""


def extract_langsearch_api_key(req):
    """Extract LangSearch API key from stored config or custom header."""
    stored = get_langsearch_api_key()
    if stored:
        app.logger.info(
            "LangSearch key source=stored fp=%s id=%s",
            _token_fingerprint(stored),
            getattr(g, "request_id", "-"),
        )
        return stored

    token = (req.headers.get("X-LangSearch-Key") or "").strip()
    if token:
        app.logger.info(
            "LangSearch key source=header fp=%s id=%s",
            _token_fingerprint(token),
            getattr(g, "request_id", "-"),
        )
    return token


VALID_MARKETS = {"us", "jp", "idx"}
VALID_HISTORY_PERIODS = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "max"}


def cleanup_history_circuit_state(now_ts=None, stale_after_sec=600):
    """Remove expired circuit breaker states to free up memory."""
    now_value = time.time() if now_ts is None else float(now_ts)
    threshold = now_value - max(0.0, float(stale_after_sec))
    with app_state.history_circuit_lock:
        stale_symbols = [
            sym
            for sym, state in app_state.history_circuit_state.items()
            if float((state or {}).get("open_until", 0.0) or 0.0) <= threshold
        ]
        for sym in stale_symbols:
            app_state.history_circuit_state.pop(sym, None)


def normalize_market(market, default="us"):
    """Validates and normalizes market identifier."""
    value = (market or default).strip().lower()
    return value if value in VALID_MARKETS else None


def normalize_symbol(symbol):
    """Clean up stock symbol string."""
    return (symbol or "").strip().upper()


def normalize_text(value, default=""):
    """テキスト値を正規化して返す。"""
    if value is None:
        return default
    return str(value).strip()


def normalize_symbol_for_market(symbol, market):
    """Adjusts symbol formatting based on market rules (e.g., .T for JP)."""
    s = normalize_symbol(symbol)
    # JP market frequently uses 4-digit codes in UI; map to Yahoo suffix format.
    if market == "jp" and s.isdigit():
        return f"{s}.T"
    return s


def validate_portfolio_input(shares, avg_price, avg_fx_rate=None):
    """ポートフォリオ入力の厳格な検証"""
    errors = []

    # 株数の検証
    if not isinstance(shares, (int, float)) or shares < 0:
        errors.append("sharesは非負の数値である必要があります")
    elif shares > PORTFOLIO_SHARES_MAX:
        errors.append(f"sharesは{PORTFOLIO_SHARES_MAX:,}以下である必要があります")

    # 平均価格の検証
    if not isinstance(avg_price, (int, float)) or avg_price < 0:
        errors.append("avg_priceは非負の数値である必要があります")
    elif avg_price > PORTFOLIO_AVG_PRICE_MAX:
        errors.append(f"avg_priceは{PORTFOLIO_AVG_PRICE_MAX:,}以下である必要があります")

    # 為替レートの検証
    if avg_fx_rate is not None:
        if not isinstance(avg_fx_rate, (int, float)) or avg_fx_rate <= 0:
            errors.append("avg_fx_rateは正の数値である必要があります")
        elif avg_fx_rate > 1_000_000:
            errors.append("avg_fx_rateは1,000,000以下である必要があります")

    # 総額の検証
    if not errors:
        total_value = shares * avg_price
        if total_value > PORTFOLIO_TOTAL_VALUE_MAX:
            errors.append(
                f"ポートフォリオ総額は{PORTFOLIO_TOTAL_VALUE_MAX:,}以下である必要があります"
            )

    return errors if errors else None


def is_valid_symbol(symbol):
    """強化されたシンボル検証（SQLインジェクションやパストラバーサル対策）"""
    if not symbol or len(symbol) > 15:
        return False
    symbol_str = str(symbol)
    # 危険な文字のチェックを強化
    dangerous_chars = ["/", "\\", "..", "\0", "%", "\x00", "\n", "\r"]
    if any(char in symbol_str for char in dangerous_chars):
        return False
    # Unicode正規化と制御文字の除去
    symbol_normalized = unicodedata.normalize("NFKC", symbol_str)
    if not SYMBOL_PATTERN.match(symbol_normalized):
        return False
    return True


def parse_non_negative_float(value, field_name, max_value=None):
    """Safely parse a number and ensure it is non-negative and finite."""
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


def extract_chat_content(response):
    """
    Chat Completions レスポンス用（/v1/chat/completions）。
    content が string / list-chunks の両方に対応。
    """
    if not response:
        return "応答が空です"
    if isinstance(response, dict) and response.get("object") == "error":
        return response.get("message") or json.dumps(response, ensure_ascii=False)
    if isinstance(response, dict) and "error" in response:
        err = response["error"]
        if isinstance(err, dict):
            return err.get("message") or json.dumps(err, ensure_ascii=False)
        return str(err)
    try:
        choices = response.get("choices")
        if not choices:
            return (
                f"Unexpected response: {json.dumps(response, ensure_ascii=False)[:500]}"
            )
        message = choices[0].get("message", {})
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            if content.get("type") == "text" and isinstance(content.get("text"), str):
                return content.get("text").strip()
            if content.get("type") in ("json", "json_object"):
                value = content.get("value", content.get("content", content))
                try:
                    return json.dumps(value, ensure_ascii=False)
                except (TypeError, ValueError):
                    return json.dumps(content, ensure_ascii=False)
            if "text" in content and isinstance(content.get("text"), str):
                return content.get("text").strip()
            return json.dumps(content, ensure_ascii=False)
        if isinstance(content, list):
            texts = []
            for chunk in content:
                if isinstance(chunk, dict):
                    if chunk.get("type") == "text":
                        texts.append(chunk.get("text", ""))
                    elif chunk.get("type") in ("json", "json_object"):
                        try:
                            texts.append(
                                json.dumps(
                                    chunk.get("value", chunk), ensure_ascii=False
                                )
                            )
                        except (TypeError, ValueError):
                            texts.append(str(chunk))
                    elif "text" in chunk:
                        texts.append(chunk.get("text", ""))
            return "".join(texts).strip() or "テキスト応答を抽出できませんでした"
        return json.dumps(content, ensure_ascii=False)
    except (ValueError, TypeError, KeyError) as exc:
        return f"解析に失敗しました: {exc}"


def extract_json_payload(content, required_fields=None):
    """
    AIの出力から最初の JSON オブジェクトを抽出。
    1. Markdown fence (```json ... ```) から抽出試行
    2. 深さ追跡で最初の {} ペアを探索
    3. 末尾截断対応：最後の } がない場合は修復試行
    """
    if isinstance(content, (dict, list)):
        return json.dumps(content, ensure_ascii=False)
    text = (content or "").strip()
    if not text:
        raise ValueError("空の応答です")

    def _try_json_parse(s):
        s = s.strip()
        try:
            return json.loads(s), s
        except json.JSONDecodeError:
            # 末尾カンマの削除を試行 (..., } -> ... })
            fixed = re.sub(r",\s*([\]}])", r"\1", s)
            try:
                return json.loads(fixed), fixed
            except json.JSONDecodeError:
                return None, s

    # Stage 1: Markdown fence
    match_fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if match_fence:
        candidate = match_fence.group(1).strip()
        obj, fixed_s = _try_json_parse(candidate)
        if obj is not None:
            return fixed_s
        text = candidate

    # Stage 2: 深さ追跡
    first_brace = text.find("{")
    if first_brace == -1:
        raise ValueError("JSONブロックが見つかりません")

    depth = 0
    in_str = False
    escape = False
    candidate = text[first_brace:]  # 初期値を設定（locals()アンチパターンを回避）

    for i, ch in enumerate(text[first_brace:], start=first_brace):
        if escape:
            escape = False
        elif ch == "\\" and in_str:
            escape = True
        elif ch == '"':
            in_str = not in_str
        elif not in_str:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[first_brace : i + 1]
                    obj, fixed_s = _try_json_parse(candidate)
                    if obj is not None:
                        return fixed_s

    # Stage 3: 末尾截断対応
    if depth > 0:
        # 末尾が開きっぱなしの場合、閉じ括弧を追加して修復
        # 文字列リテラル内の場合はまず引用符を閉じる
        salvage_text = candidate if candidate != text[first_brace:] else text[first_brace:].rstrip()
        if in_str:
            salvage_text += '"'
        salvage_text += "}" * depth
        obj, fixed_s = _try_json_parse(salvage_text)
        if obj is not None:
            app.logger.info("JSON salvaged by adding %d closing braces", depth)
            return fixed_s

    # Final Stage: Token-by-token salvage (fallback for highly malformed LLM responses)
    # Search for common fields to attempt a partial recovery
    if required_fields is None:
        required_fields = ["recommendation", "sentiment", "target_price_3m"]
    if all(f'"{f}"' in text for f in required_fields):
        # We might have enough to build a manual JSON
        try:
            recovered = {}
            for f in required_fields + ["analysis_summary"]:
                m = re.search(rf'"{f}"\s*:\s*"([^"]*)"', text)
                if m:
                    recovered[f] = m.group(1)
            if recovered:
                app.logger.info("JSON salvaged by manual field extraction")
                return json.dumps(recovered, ensure_ascii=False)
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    snippet = text.replace("\n", " ").replace("\r", " ").strip()
    if len(snippet) > 200:
        snippet = snippet[:200] + "..."
    raise ValueError(
        f"JSONブロックの抽出に失敗しました (構文エラーの可能性あり)。入力先頭: {snippet}"
    )


def normalize_analysis_result(result):
    """Ensures all expected keys are present in the AI analysis result dictionary."""
    normalized = dict(result or {})
    defaults = {
        "recommendation": "中立",
        "sentiment": "中立",
        "target_price_3m": 0,
        "upside_3m": "0%",
        "confidence": "低",
        "analysis_summary": "データが不足しているため保守的に中立判定",
        "key_catalysts": [],
        "risk_factors": [],
        "technical_analysis": "データ不足",
        "fundamental_analysis": "データ不足",
        "latest_news_impact": "データ不足",
    }
    for key, value in defaults.items():
        if key not in normalized or normalized[key] in (None, ""):
            normalized[key] = value

    if not isinstance(normalized.get("key_catalysts"), list):
        normalized["key_catalysts"] = [str(normalized.get("key_catalysts"))]
    if not isinstance(normalized.get("risk_factors"), list):
        normalized["risk_factors"] = [str(normalized.get("risk_factors"))]

    return normalized


def validate_analysis_result(result):
    """Lightweight validation for AI analysis results.

    Accepts partial results (LLM function-calling may return a subset). Returns
    (True, "") when the result is considered usable; otherwise (False, reason).
    """
    if not isinstance(result, dict):
        return False, "result is not an object"

    # Require at least one core field to consider the result usable.
    core_fields = ("analysis_summary", "recommendation", "sentiment", "target_price_3m")
    if not any(k in result for k in core_fields):
        return False, "missing core analysis fields"

    # If specific fields are present, sanity-check their types (non-blocking)
    if "target_price_3m" in result:
        tpm = result.get("target_price_3m")
        if not isinstance(tpm, (int, float)):
            try:
                float(str(tpm))
            except Exception:
                return False, "target_price_3m must be numeric"

    if "key_catalysts" in result and not isinstance(result.get("key_catalysts"), list):
        return False, "key_catalysts must be an array"
    if "risk_factors" in result and not isinstance(result.get("risk_factors"), list):
        return False, "risk_factors must be an array"

    return True, ""


def build_fallback_analysis_result(reason: str = ""):
    """Builds a neutral fallback result when AI analysis fails."""
    base = normalize_analysis_result({})
    if reason:
        base["analysis_summary"] = f"構造化出力に失敗したため保守的判定: {reason[:80]}"
    base["fallback_used"] = True
    return base


def repair_analysis_json_with_llm(api_key, raw_content):
    """Asks the LLM to fix a malformed analysis JSON string."""
    repair_prompt = (
        "次のテキストを指定スキーマのJSONオブジェクトに変換してください。"
        "必須キー: recommendation,sentiment,target_price_3m,upside_3m,confidence,"
        "analysis_summary,key_catalysts,risk_factors,technical_analysis,fundamental_analysis,latest_news_impact\n"
        "入力テキスト:\n"
        f"{raw_content}"
    )
    response = call_mistral_chat(
        api_key,
        [
            {
                "role": "system",
                "content": "あなたは厳密なJSONフォーマッターです。必ず有効なJSONオブジェクトのみを返してください。"
                "マークダウンコードブロックや追加のテキストを含めず、JSONのみを出力してください。",
            },
            {"role": "user", "content": repair_prompt},
        ],
        max_tokens=700,
        response_format={"type": "json_object"},
    )

    repaired_content = extract_chat_content(response)
    repaired_json_str = extract_json_payload(repaired_content)
    return json.loads(repaired_json_str), repaired_content


def repair_news_json_with_llm(api_key, raw_content):
    """Asks the LLM to fix a malformed news JSON string."""
    repair_prompt = (
        "次のテキストをニュース要約用のJSONオブジェクトに変換してください。"
        "必須キー: us,jp,trends\n"
        "各値は改行区切りの文字列。見出しの生引用/source/date/url/HTML/URL文字列は含めないこと。\n"
        "入力テキスト:\n"
        f"{raw_content}"
    )
    response = call_mistral_chat(
        api_key,
        [
            {
                "role": "system",
                "content": "あなたは厳密なJSONフォーマッターです。必ず有効なJSONオブジェクトのみを返してください。"
                "マークダウンコードブロックや追加のテキストを含めず、JSONのみを出力してください。",
            },
            {"role": "user", "content": repair_prompt},
        ],
        max_tokens=1000,
        response_format={"type": "json_object"},
    )

    repaired_content = extract_chat_content(response)
    repaired_json_str = extract_json_payload(repaired_content)
    payload = json.loads(repaired_json_str)
    return {
        "us": str(payload.get("us") or ""),
        "jp": str(payload.get("jp") or ""),
        "trends": str(payload.get("trends") or ""),
    }, repaired_content


# ------------------------------
# Mistral API Callers
# ------------------------------


def _get_mistral_model_name():
    """配置されたモデル名を取得し、許可されたリストにない場合はデフォルトの小モデルを返す。"""
    configured_model = (get_model_name() or "").strip()
    allowed_models = {
        "mistral-small-latest",
        "mistral-medium-latest",
        "mistral-medium-3-5",
        "mistral-large-latest",
        "open-mistral-nemo",
        "ministral-8b-latest",
        "ministral-3b-latest",
        "pixtral-large-latest",
    }
    if configured_model in allowed_models:
        return configured_model
    if configured_model:
        app.logger.warning(
            "Unknown configured Mistral model: %s. Falling back to mistral-small-latest.",
            configured_model,
        )
    return "mistral-small-latest"


def _build_mistral_cache_key(
    model_name, msgs, token_limit, response_format_value, tools=None, tool_choice=None
):
    """キャッシュ用のユニークなキーを生成。"""
    payload = json.dumps(
        {
            "model": model_name,
            "messages": msgs,
            "max_tokens": int(token_limit),
            "response_format": response_format_value,
            "tools": tools,
            "tool_choice": tool_choice,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    digest = hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()
    return f"mistral_chat_{digest}"


def _to_mistral_error_payload(payload, status_code=None):
    """APIレスポンスから統一されたエラーペイロードを生成。"""
    if isinstance(payload, dict):
        if payload.get("object") == "error":
            return {
                "error": {
                    "message": payload.get("message") or "Mistral error",
                    "type": payload.get("type"),
                    "code": payload.get("code"),
                    "status_code": status_code,
                }
            }
        if "error" in payload:
            err = payload.get("error")
            if isinstance(err, dict):
                err.setdefault("status_code", status_code)
                return {"error": err}
            return {"error": {"message": str(err), "status_code": status_code}}
        return {
            "error": {
                "message": json.dumps(payload, ensure_ascii=False)[:500],
                "status_code": status_code,
            }
        }
    return {"error": {"message": str(payload), "status_code": status_code}}


def _is_mistral_capacity_error(err_payload):
    """429や容量制限エラーかどうかを判定。"""
    err = (err_payload or {}).get("error", {})
    if not isinstance(err, dict):
        return False
    return (
        err.get("type") == "service_tier_capacity_exceeded"
        or str(err.get("code")) == "3505"
        or int(err.get("status_code") or 0) == 429
    )


def _extract_mistral_wait_seconds(response):
    """レスポンスヘッダから待機秒数を抽出。"""
    headers = getattr(response, "headers", {}) or {}
    waits = []

    def _parse_sec(value):
        text = str(value or "").strip()
        if not text:
            return 0.0
        try:
            return max(0.0, float(text))
        except (ValueError, TypeError):
            pass
        lower = text.lower()
        if lower.endswith("ms"):
            try:
                return max(0.0, float(lower[:-2].strip()) / 1000.0)
            except (ValueError, TypeError):
                return 0.0
        try:
            dt = parsedate_to_datetime(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, AttributeError):
            return 0.0

    waits.append(_parse_sec(headers.get("Retry-After")))
    waits.append(_parse_sec(headers.get("retry-after")))

    for key in ["X-RateLimit-Reset", "x-ratelimit-reset", "x-ratelimit-reset-requests"]:
        raw = headers.get(key)
        if raw:
            try:
                epoch = float(str(raw).strip())
                waits.append(max(0.0, epoch - time.time()))
            except (ValueError, TypeError):
                waits.append(_parse_sec(raw))

    return max((w for w in waits if w and w > 0.0), default=0.0)


def call_mistral_chat(
    api_key,
    messages,
    max_tokens=600,
    use_cache=True,
    response_format=None,
    tools=None,
    tool_choice=None,
    cache_key_override=None,
):
    """通常の Chat Completions 呼び出し（/v1/chat/completions）"""
    model = _get_mistral_model_name()
    token_limit = max(64, min(int(max_tokens or 600), 2000))
    min_interval_sec = 1.35

    cache_key = (
        _build_mistral_cache_key(
            model, messages, token_limit, response_format, tools, tool_choice
        )
        if use_cache
        else None
    )
    if use_cache:
        with app_state.mistral_response_lock:
            cached = app_state.mistral_response_cache.get(cache_key)
        if cached is not None:
            return copy.deepcopy(cached)

    last_error = None
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            with app_state.mistral_cooldown_lock:
                now_ts = time.time()
                wait_before = max(
                    app_state.mistral_next_allowed_ts - now_ts,
                    (app_state.mistral_last_call_ts + min_interval_sec) - now_ts,
                    0.0,
                )
            if wait_before > 0:
                time.sleep(wait_before)

            with app_state.mistral_call_semaphore:
                app.logger.info(
                    "Mistral call start id=%s attempt=%d model=%s max_tokens=%d key=%s",
                    getattr(g, "request_id", "-"),
                    attempt + 1,
                    model,
                    token_limit,
                    _token_fingerprint(api_key),
                )
                res = _mistral_session.post(
                    f"{MISTRAL_BASE_URL}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": messages,
                        "temperature": 0.1,
                        "max_tokens": token_limit,
                        **(
                            {"response_format": response_format}
                            if isinstance(response_format, dict)
                            else {}
                        ),
                        **({"tools": tools} if tools else {}),
                        **({"tool_choice": tool_choice} if tool_choice else {}),
                        **(
                            {"prompt_cache_key": cache_key_override}
                            if cache_key_override
                            else {}
                        ),
                    },
                    timeout=45,
                )
                with app_state.mistral_cooldown_lock:
                    app_state.mistral_last_call_ts = time.time()

            try:
                data = res.json()
            except Exception as parse_exc:  # pylint: disable=broad-exception-caught
                app.logger.debug(
                    "Mistral response JSON parse failed id=%s status=%s: %s",
                    getattr(g, "request_id", "-"),
                    res.status_code,
                    parse_exc,
                )
                data = {
                    "object": "error",
                    "message": f"HTTP {res.status_code}: 非JSONレスポンス",
                }

            if res.ok and isinstance(data, dict) and data.get("choices"):
                app.logger.info(
                    "Mistral call success id=%s status=%s model=%s attempt=%d",
                    getattr(g, "request_id", "-"),
                    res.status_code,
                    model,
                    attempt + 1,
                )
                with app_state.mistral_cooldown_lock:
                    app_state.mistral_429_streak = 0
                    app_state.mistral_next_allowed_ts = 0.0
                if use_cache:
                    with app_state.mistral_response_lock:
                        app_state.mistral_response_cache[cache_key] = copy.deepcopy(
                            data
                        )
                try:
                    # Ensure HTTP response is closed to release connection resources
                    res.close()
                except Exception:
                    pass
                return data

            err_payload = _to_mistral_error_payload(data, status_code=res.status_code)
            last_error = err_payload
            app.logger.warning(
                "Mistral call non-ok id=%s status=%s model=%s attempt=%d error=%s",
                getattr(g, "request_id", "-"),
                res.status_code,
                model,
                attempt + 1,
                _short_text((err_payload.get("error") or {}).get("message"), 240),
            )

            if res.status_code == 401:
                try:
                    res.close()
                except Exception:
                    pass
                return {
                    "error": {
                        "message": "Mistral API認証に失敗しました。保存済みAPIキーを再登録してください。",
                        "type": "authentication_error",
                        "code": "unauthorized",
                        "status_code": 401,
                        "details": data,
                    }
                }

            if _is_mistral_capacity_error(err_payload):
                provider_wait = _extract_mistral_wait_seconds(res)
                wait_time = max(
                    provider_wait,
                    min(2 ** min(attempt + 1, 7), 60) + random.uniform(0.2, 1.0),
                )

                with app_state.mistral_cooldown_lock:
                    app_state.mistral_429_streak = min(
                        app_state.mistral_429_streak + 1, 10
                    )
                    penalty = min(2 ** min(app_state.mistral_429_streak, 7), 60)
                    shared_wait = max(wait_time, penalty)

                    if attempt >= 2:
                        try:
                            res.close()
                        except Exception:
                            pass
                        return {
                            "error": {
                                "message": "API capacity exceeded. Please try again later.",
                                "type": "service_tier_capacity_exceeded",
                                "code": "3505",
                                "status_code": 429,
                            }
                        }

                    app_state.mistral_next_allowed_ts = max(
                        app_state.mistral_next_allowed_ts, time.time() + shared_wait
                    )

                app.logger.warning(
                    "Mistral capacity exceeded (attempt %d/%d). Wait=%ds model=%s provider_wait=%.2f",
                    attempt + 1,
                    max_attempts,
                    int(shared_wait),
                    model,
                    provider_wait,
                )
                continue

            if res.status_code == 408 or res.status_code >= 500:
                if attempt < max_attempts - 1:
                    wait_time = min(2 ** (attempt + 1), 8) + random.uniform(0.1, 0.5)
                    time.sleep(wait_time)
                    continue
            break
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt < max_attempts - 1:
                time.sleep(min(2 ** (attempt + 1), 8) + random.uniform(0.1, 0.5))
                continue
            last_error = {"error": {"message": f"Mistral call failed: {exc}"}}
            break
        except Exception as exc:  # pylint: disable=broad-exception-caught
            app.logger.exception(
                "Mistral call exception id=%s: %s", getattr(g, "request_id", "-"), exc
            )
            last_error = {"error": {"message": f"Mistral call failed: {exc}"}}
            break

    try:
        if 'res' in locals() and res is not None:
            try:
                res.close()
            except Exception:
                pass
    except Exception:
        pass
    return last_error or {"error": {"message": "Mistral call failed"}}


# ------------------------------
# DDGS Helpers
# ------------------------------
def ddgs_news_search(
    query,
    region="us-en",
    timelimit="d",
    max_results=8,
    backend="auto",
    ddgs_session=None,
):
    """DuckDuckGoでニュース検索を実行する。

    ddgs v9.x (deedy5/ddgs)対応版。
    最新版ではパラメータ名が変更され、戻り値は辞書のリスト。
    """

    def do_search(session, q, b, t):
        # ddgs v9.x: keywords -> query, verifyパラメータ削除
        kwargs = {
            "query": q,
            "region": region,
            "safesearch": "moderate",
            "max_results": max_results,
            "backend": b,
        }
        if t:
            kwargs["timelimit"] = t
        # ddgs v9.x: news()は既にリストを返す
        return session.news(**kwargs) or []

    normalized_query = " ".join(str(query or "").split())
    short_query = " ".join(normalized_query.split()[:3]).strip()
    attempts = [
        (normalized_query, backend, timelimit),
        (normalized_query, backend, None),
        (normalized_query, "duckduckgo", timelimit),
        (normalized_query, "duckduckgo", None),
    ]
    if short_query and short_query != normalized_query:
        attempts.extend(
            [
                (short_query, "auto", timelimit),
                (short_query, "duckduckgo", None),
            ]
        )

    last_error_message = ""
    session_owned = False
    ddgs = ddgs_session
    if ddgs is None:
        # ddgs v9.x: timeoutパラメータはそのまま使用可能
        ddgs = DDGS(timeout=int(os.environ.get("DDGS_TIMEOUT", "10")))
        session_owned = True

    try:
        seen = set()
        for q, b, t in attempts:
            key = (q, b, t)
            if key in seen or not q:
                continue
            seen.add(key)
            try:
                results = do_search(ddgs, q, b, t)
                if results:
                    return results
            except Exception as exc:  # pylint: disable=broad-exception-caught
                message = str(exc)
                last_error_message = message
                if "No results found" in message:
                    app.logger.debug(
                        "DDGS news no result (%s, region=%s, backend=%s, timelimit=%s)",
                        q,
                        region,
                        b,
                        t,
                    )
                    continue
                if "DecodeError" in message:
                    app.logger.debug(
                        "DDGS news decode error (%s, region=%s, backend=%s): %s",
                        q,
                        region,
                        b,
                        message,
                    )
                    continue
                app.logger.warning(
                    "DDGS news search failed (%s, region=%s, backend=%s, timelimit=%s): %s",
                    q,
                    region,
                    b,
                    t,
                    exc,
                )
                continue
        if last_error_message:
            app.logger.debug(
                "DDGS news exhausted fallback attempts (%s, region=%s): %s",
                normalized_query,
                region,
                last_error_message,
            )
        return []
    finally:
        if session_owned:
            try:
                ddgs.close()
            except Exception as close_exc:  # pylint: disable=broad-exception-caught
                app.logger.debug("DDGS close failed: %s", close_exc)


def ddgs_text_search(
    query,
    region="us-en",
    timelimit="w",
    max_results=8,
    backend="auto",
    ddgs_session=None,
):
    """DuckDuckGoでテキスト検索を実行する。

    ddgs v9.x (deedy5/ddgs)対応:
    - queryパラメータを使用
    - 戻り値はリスト形式
    """
    try:

        def do_search(session):
            # ddgs v9.x: queryパラメータを使用、戻り値はリスト
            return (
                session.text(
                    query=query,
                    region=region,
                    safesearch="moderate",
                    timelimit=timelimit,
                    max_results=max_results,
                    backend=backend,
                )
                or []
            )

        if ddgs_session:
            return do_search(ddgs_session)
        # ddgs v9.x: timeoutとverifyパラメータ
        with DDGS(
            timeout=int(os.environ.get("DDGS_TIMEOUT", "10")),
        ) as ddgs:
            return do_search(ddgs)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        message = str(exc)
        if "No results found" in message:
            app.logger.debug("DDGS text no result (%s, region=%s)", query, region)
        elif "DecodeError" in message:
            app.logger.debug(
                "DDGS text decode error (%s, region=%s): %s", query, region, message
            )
        else:
            app.logger.error("DDGS text search failed (%s): %s", query, exc)
        return []


def _dedupe_items(items):
    return ts.dedupe_items(items)


def _format_ddgs_news_items(items):
    rows = []
    for x in items:
        rows.append(
            {
                "title": x.get("title", ""),
                "summary": x.get("body", ""),
                "url": x.get("url", ""),
                "source": x.get("source", "ddgs_news"),
                "date": x.get("date", ""),
            }
        )
    return rows


def _format_ddgs_text_items(items):
    rows = []
    for x in items:
        rows.append(
            {
                "title": x.get("title", ""),
                "summary": x.get("body", ""),
                "url": x.get("href", ""),
                "source": "ddgs_text",
                "date": "",
            }
        )
    return rows


def _request_json_post(url, payload, headers, timeout=LANGSEARCH_TIMEOUT):
    """Helper to perform a JSON POST request and validate the response."""
    response = app_state._langsearch_session.post(
        url, json=payload, headers=headers, timeout=timeout
    )
    
    # Try to parse JSON even on failure to get descriptive error messages
    parsed = {}
    try:
        parsed = response.json()
    except (ValueError, TypeError, json.JSONDecodeError):
        pass

    if not response.ok:
        status_code = response.status_code
        error_msg = "Unknown LangSearch error"
        if isinstance(parsed, dict):
            # Try to get 'msg' from LangSearch's standard error format
            error_msg = str(parsed.get("msg") or parsed.get("message") or f"HTTP {status_code}")
            code = parsed.get("code")
            if code is not None:
                error_msg = f"LangSearch code={code} msg={error_msg}"
        
        # Raise HTTPError with the detailed message
        raise requests.HTTPError(error_msg, response=response)

    # If status is 200, still check for app-level error codes if present
    if isinstance(parsed, dict):
        code = parsed.get("code")
        if code is not None:
            try:
                code_int = int(code)
            except (ValueError, TypeError):
                code_int = None
            if code_int is not None and code_int != 200:
                msg = str(parsed.get("msg") or "LangSearch application-level error")
                raise requests.HTTPError(
                    f"LangSearch code={code_int} msg={msg}", response=response
                )
    return parsed


def _langsearch_request_retryable(exc: Exception) -> bool:
    """Predicate to determine if a LangSearch error should be retried."""
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(exc, requests.HTTPError):
        msg = str(exc).lower()
        # Do not retry if it looks like a quota or balance issue
        if any(x in msg for x in ["insufficient balance", "quota exceeded", "balance not enough"]):
            return False
            
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        return status in (429, 503)
    return False


def _langsearch_acquire_slot():
    """Acquires a rate-limit slot for LangSearch calls."""
    with app_state.langsearch_rate_lock:
        now = time.time()
        wait_seconds = max(0.0, app_state.langsearch_next_allowed_ts - now)
        app_state.langsearch_next_allowed_ts = (
            max(app_state.langsearch_next_allowed_ts, now)
            + app_state.langsearch_min_interval_sec
        )
    if wait_seconds > 0:
        time.sleep(wait_seconds)


def _langsearch_mark_retry_after_429(retry_after_sec: float = None):
    """Flags that LangSearch has rate-limited our requests.

    If the server provides a Retry-After header, use that value;
    otherwise fall back to the default cooldown.
    """
    cooldown = (
        float(retry_after_sec)
        if retry_after_sec is not None
        else app_state.langsearch_429_cooldown_sec
    )
    with app_state.langsearch_rate_lock:
        app_state.langsearch_next_allowed_ts = max(
            app_state.langsearch_next_allowed_ts,
            time.time() + max(0.0, cooldown),
        )


@retry(
    retry=retry_if_exception(_langsearch_request_retryable),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=16),
    reraise=True,
    before_sleep=before_sleep_log(app.logger, logging.WARNING),
)
def _langsearch_post_json(endpoint, payload, headers):
    """Execution wrapper for LangSearch POST with retry logic."""
    _langsearch_acquire_slot()
    try:
        return _request_json_post(
            endpoint, payload, headers, timeout=LANGSEARCH_TIMEOUT
        )
    except requests.HTTPError as exc:
        response = getattr(exc, "response", None)
        if getattr(response, "status_code", None) == 429:
            # Log the detailed message from the exception (which now includes the body msg)
            app.logger.warning("LangSearch rate limited (429): %s", exc)
            
            retry_after = None
            if response is not None:
                retry_after_raw = response.headers.get(
                    "Retry-After"
                ) or response.headers.get("retry-after")
                if retry_after_raw:
                    try:
                        retry_after = float(retry_after_raw)
                    except (ValueError, TypeError):
                        retry_after = None
            _langsearch_mark_retry_after_429(retry_after)
        raise


def _summarize_http_error(exc: Exception) -> str:
    """Extracts a human-readable summary from a requests exception."""
    response = getattr(exc, "response", None)
    if response is None:
        return str(exc)
    status = getattr(response, "status_code", "?")
    body = ""
    try:
        body = (response.text or "").strip()
    except (IOError, ValueError, TypeError):
        body = ""
    if len(body) > 300:
        body = body[:300] + "..."
    return f"status={status} body={body or '<empty>'}"


def _extract_langsearch_entries(payload):
    """Locates the list of search results within a LangSearch response."""
    if not isinstance(payload, dict):
        return []

    data = payload.get("data")
    if isinstance(data, dict):
        web_pages = data.get("webPages")
        if isinstance(web_pages, dict) and isinstance(web_pages.get("value"), list):
            return web_pages.get("value")

    candidates = []
    if isinstance(data, dict):
        candidates.extend(
            [
                data.get("results"),
                data.get("items"),
                (
                    data.get("webPages", {}).get("value")
                    if isinstance(data.get("webPages"), dict)
                    else None
                ),
            ]
        )
    candidates.extend(
        [
            payload.get("results"),
            payload.get("items"),
            (
                payload.get("webPages", {}).get("value")
                if isinstance(payload.get("webPages"), dict)
                else None
            ),
        ]
    )

    for candidate in candidates:
        if isinstance(candidate, list):
            return candidate
    return []


def _format_langsearch_items(items):
    """Normalizes LangSearch result items into a common internal format."""
    rows = []
    for x in items:
        if not isinstance(x, dict):
            continue
        rows.append(
            {
                "title": x.get("title") or x.get("name") or "",
                "summary": x.get("snippet")
                or x.get("summary")
                or x.get("description")
                or x.get("body")
                or "",
                "url": x.get("url") or x.get("link") or x.get("href") or "",
                "source": x.get("source")
                or x.get("siteName")
                or x.get("site")
                or x.get("displayUrl")
                or "langsearch",
                "date": x.get("datePublished")
                or x.get("published_at")
                or x.get("publishedAt")
                or x.get("date")
                or x.get("time")
                or "",
            }
        )
    return rows


def _map_langsearch_freshness(timelimit):
    """Maps internal freshness identifiers to LangSearch strings."""
    mapping = {
        "d": "oneDay",
        "w": "oneWeek",
        "m": "oneMonth",
        "y": "oneYear",
        "none": "noLimit",
        "": "noLimit",
        None: "noLimit",
    }
    return mapping.get(str(timelimit).lower(), "noLimit")


def langsearch_search(query, api_key, max_results=8, timelimit="d"):
    """Performs a web search via LangSearch API."""
    normalized_query = " ".join(str(query or "").split())
    if not normalized_query:
        return []
    if not api_key:
        raise ValueError("LangSearch API key is required")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "query": normalized_query,
        "freshness": _map_langsearch_freshness(timelimit),
        "summary": True,
        "count": max(1, int(max_results or 8)),
    }
    try:
        return _extract_langsearch_entries(
            _langsearch_post_json(LANGSEARCH_WEB_SEARCH_ENDPOINT, payload, headers)
        )
    except requests.HTTPError as exc:
        response = getattr(exc, "response", None)
        if getattr(response, "status_code", None) == 429:
            _langsearch_mark_retry_after_429()
        raise


def langsearch_rerank(query, documents, api_key):
    """LangSearch Semantic Rerank APIを使用してドキュメントを再評価し、関連性の高い順にソートする"""
    if not api_key or not documents or len(documents) < 2:
        return documents

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    # Rerank API accepts model, query and documents (array of strings)
    payload = {
        "model": "langsearch-reranker-v1",
        "query": query,
        "documents": [
            (d.get("summary") or d.get("title") or "")[:1000] for d in documents[:50]
        ],
    }

    try:
        parsed = _langsearch_post_json(
            f"{LANGSEARCH_BASE_URL}/v1/rerank", payload, headers
        )
        results = parsed.get("results", [])

        # スコアに基づいてドキュメントをマッピング
        scored_docs = []
        for result in results:
            idx = result.get("index")
            if idx is not None and idx < len(documents):
                doc = documents[idx].copy()
                doc["relevance_score"] = result.get("relevance_score", 0)
                scored_docs.append(doc)

        if not scored_docs:
            return documents

        # スコア降順でソート
        return sorted(scored_docs, key=lambda x: x.get("relevance_score", 0), reverse=True)
    except Exception as exc:
        app.logger.warning("LangSearch rerank failed: %s", exc)
        return documents


def _collect_langsearch_items(
    queries, api_key, timelimit, max_results=6, limit=10, query_limit=3
):
    """Sequentially searches multiple queries and collects unique results."""
    if not api_key:
        return []

    items = []
    for q in queries[: max(1, int(query_limit))]:
        if len(items) >= limit * 2:
            break
        try:
            results = langsearch_search(
                q,
                api_key=api_key,
                max_results=max_results,
                timelimit=timelimit,
            )
            items.extend(_format_langsearch_items(results))
        except (ValueError, RuntimeError, RequestException) as exc:
            app.logger.warning(
                "LangSearch search failed (%s): %s", q, _summarize_http_error(exc)
            )
            continue

    unique_items = _dedupe_items(items)

    # 項目数が多い場合は、最初のクエリを基準にリランクを実行して精度を高める
    if len(unique_items) > 5 and queries:
        unique_items = langsearch_rerank(queries[0], unique_items, api_key)

    return unique_items[:limit]


def _market_ddgs_queries(market="us"):
    """Returns search queries for market-wide news via DDGS."""
    key = "jp" if str(market).lower() == "jp" else "us"
    region = "jp-ja" if key == "jp" else "us-en"
    return region, ts.market_queries(key)


def _symbol_ddgs_queries(symbol, name, market="us"):
    """Returns search queries for specific stock research via DDGS."""
    key = "jp" if str(market).lower() == "jp" else "us"
    region = "jp-ja" if key == "jp" else "us-en"
    return region, ts.symbol_queries(symbol, name, key)


def _collect_ddgs_items(
    queries, region, timelimit, news_n, text_n, limit=10, query_limit=3
):
    """Uses DuckDuckGo Search to collect news and text snippets."""
    items = []
    backend_pref = "auto"
    try:
        # ddgs v9.x: verifyパラメータは削除、timeoutのみ使用
        with DDGS(timeout=6) as ddgs:
            for q in queries[: max(1, int(query_limit))]:
                if len(items) >= limit * 2:
                    break
                items.extend(
                    _format_ddgs_news_items(
                        ddgs_news_search(
                            q,
                            region=region,
                            timelimit=timelimit,
                            max_results=news_n,
                            backend=backend_pref,
                            ddgs_session=ddgs,
                        )
                    )
                )
                items.extend(
                    _format_ddgs_text_items(
                        ddgs_text_search(
                            q,
                            region=region,
                            timelimit=timelimit,
                            max_results=text_n,
                            backend=backend_pref,
                            ddgs_session=ddgs,
                        )
                    )
                )
    except (IOError, ValueError, RuntimeError, RequestException) as exc:
        app.logger.error("DDGS context collection failed: %s", exc)
    return _dedupe_items(items)[:limit]


def _extract_trending_titles_from_items(items, count=15):
    """Extracts unique titles from a list of search result items."""
    titles = []
    for item in _dedupe_items(items):
        title = str(item.get("title", "") or "").strip()
        if title:
            titles.append(title)
        if len(titles) >= count:
            break
    return titles


def _compact_small_model_context(items, limit=7, max_chars=1800):
    """Trims search context to fit within LLM token constraints."""
    text = ts.compact_context(items, limit=limit)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip()


def collect_market_news_context(market="us", langsearch_api_key=""):
    """Fetches and merges market-wide context from multiple sources."""
    region, queries = _market_ddgs_queries(market)
    ts_items = ts.collect_market_news_items_fast(market)
    search_items = _collect_langsearch_items(
        queries,
        api_key=langsearch_api_key,
        timelimit="d",
        max_results=2,
        limit=6,
        query_limit=2,
    )
    if search_items:
        app.logger.info(
            "LangSearch used: context=market_news market=%s items=%s",
            market,
            len(search_items),
        )
    else:
        reason = "missing_api_key" if not langsearch_api_key else "empty_or_error"
        app.logger.info(
            "DDGS fallback used: context=market_news market=%s reason=%s",
            market,
            reason,
        )
        search_items = _collect_ddgs_items(
            queries, region, "d", news_n=1, text_n=1, limit=6, query_limit=2
        )
        app.logger.info(
            "DDGS results: context=market_news market=%s items=%s",
            market,
            len(search_items),
        )
    merged = ts.dedupe_items(list(ts_items) + list(search_items))
    return _compact_small_model_context(merged, limit=6, max_chars=1400)


def collect_symbol_research_context(symbol, name, market="us", langsearch_api_key=""):
    """Collects deep research context for a specific stock ticker."""
    region, queries = _symbol_ddgs_queries(symbol, name, market)
    ts_items = ts.collect_symbol_research_items(symbol, name, market)
    search_items = _collect_langsearch_items(
        queries,
        api_key=langsearch_api_key,
        timelimit="m",
        max_results=3,
        limit=8,
    )
    if search_items:
        app.logger.info(
            "LangSearch used: context=symbol_research market=%s symbol=%s items=%s",
            market,
            symbol,
            len(search_items),
        )
    else:
        reason = "missing_api_key" if not langsearch_api_key else "empty_or_error"
        app.logger.info(
            "DDGS fallback used: context=symbol_research market=%s symbol=%s reason=%s",
            market,
            symbol,
            reason,
        )
        search_items = _collect_ddgs_items(
            queries, region, "m", news_n=2, text_n=1, limit=8
        )
        app.logger.info(
            "DDGS results: context=symbol_research market=%s symbol=%s items=%s",
            market,
            symbol,
            len(search_items),
        )
    merged = ts.dedupe_items(list(ts_items) + list(search_items))
    return _compact_small_model_context(merged, limit=8, max_chars=2200)


def collect_market_trending_titles(market="us", count=10, langsearch_api_key=""):
    """Retrieve trending market titles for UI display."""
    capped = min(count, 15)
    search_source_hint = "ls" if langsearch_api_key else "ddgs"
    return _get_market_trending_titles(market, search_source_hint, langsearch_api_key)[
        :capped
    ]


# ------------------------------
# Stock Info Helpers
# ------------------------------
def acquire_yfinance_slot() -> bool:
    """yfinance のリクエスト用スロットを取得する。

    レート制限中の場合は False を返す。
    スロットが取得でき、必要であればロック外で time.sleep を呼び出し、True を返す。
    """
    wait_time = 0.0
    with app_state.yfinance_lock:
        if app_state.is_yfinance_rate_limited:
            if time.time() < app_state.yfinance_rate_limit_until:
                return False
            app_state.is_yfinance_rate_limited = False

        now = time.time()
        elapsed = now - app_state.yfinance_last_request_ts
        if elapsed < app_state.yfinance_min_interval_sec:
            wait_time = app_state.yfinance_min_interval_sec - elapsed
        app_state.yfinance_last_request_ts = now + wait_time

    if wait_time > 0.0:
        time.sleep(wait_time)
    return True


def get_stock_info_cached(symbol: str) -> dict:
    """Retrieve basic stock info with yfinance rate-limit protection and caching."""

    def _fetch() -> dict:
        try:
            if not acquire_yfinance_slot():
                return {}

            # yfinance 1.2.2以降ではsessionパラメータにrequests.Sessionを使用できないため、sessionを指定せずに使用
            ticker = safe_get_ticker(symbol)
            if not ticker:
                return {}

            try:
                info = ticker.info
                if info:
                    return info
            except Exception as exc:
                app.logger.debug(
                    "yfinance ticker.info failed for %s, trying fast_info fallback: %s",
                    symbol,
                    exc,
                )

            # Fallback to fast_info
            try:
                fast = ticker.fast_info
                short_name = getattr(fast, "shortName", None) or getattr(fast, "displayName", None) or symbol
                prev_close = getattr(fast, "previousClose", None)
                mapped_info = {
                    "shortName": short_name,
                    "regularMarketPreviousClose": prev_close,
                    "previousClose": prev_close,
                    "currency": getattr(fast, "currency", None),
                    "marketCap": getattr(fast, "marketCap", None),
                    "exchange": getattr(fast, "exchange", None),
                    "quoteType": getattr(fast, "quoteType", None),
                    "symbol": symbol,
                }
                return {k: v for k, v in mapped_info.items() if v is not None}
            except Exception as exc:
                app.logger.debug(
                    "yfinance ticker.fast_info fallback failed for %s: %s", symbol, exc
                )
            return {}
        except Exception as exc:  # pylint: disable=broad-exception-caught
            app.logger.debug("yfinance info fetch failed for %s: %s", symbol, exc)
            return {}

    return get_cached(f"info_{symbol}", _fetch, duration=86400, valid_func=bool)



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

    # 非DataFrame入力の検出
    if not isinstance(hist, pd.DataFrame):
        app.logger.warning(
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
                app.logger.warning(
                    "Failed to convert history index to DatetimeIndex: %s", exc
                )
                return pd.DataFrame()

        if "Close" not in frame.columns:
            app.logger.warning(
                "normalize_history_frame: 'Close' column not found in DataFrame"
            )
            return pd.DataFrame()

        frame = frame.dropna(subset=["Close"])
        return frame
    except (AttributeError, KeyError, TypeError, ValueError) as norm_exc:
        app.logger.error("normalize_history_frame error: %s", norm_exc, exc_info=True)
        return pd.DataFrame()


def build_stock_payload(symbol, name_or_dict, market, hist, snapshot_ts_ms=None):
    """銘柄のペイロード辞書を構築する"""
    hist = normalize_history_frame(hist, inplace=True)
    if len(hist) < 1:
        app.logger.warning(
            "Stock %s: insufficient historical data (len=%d)", symbol, len(hist)
        )
        return None

    name = (
        name_or_dict.get("name", "") if isinstance(name_or_dict, dict) else name_or_dict
    )

    def _safe_float_field(field_name, default=0.0):
        if not isinstance(name_or_dict, dict):
            return default
        try:
            return float(name_or_dict.get(field_name, default))
        except (TypeError, ValueError):
            return default

    shares = _safe_float_field("shares", 0.0)
    avg_price = _safe_float_field("avg_price", 0.0)
    avg_fx_rate_val = (
        name_or_dict.get("avg_fx_rate") if isinstance(name_or_dict, dict) else None
    )
    try:
        avg_fx_rate = float(avg_fx_rate_val) if avg_fx_rate_val is not None else None
    except (TypeError, ValueError):
        avg_fx_rate = None
    try:
        price = float(hist["Close"].iloc[-1])
        # 履歴データが1行のみの場合、前日終値を現在値として使用
        if len(hist) == 1:
            prev = price
        else:
            prev = float(hist["Close"].iloc[-2])
        change = price - prev
        pct = (change / prev) * 100 if prev else 0

        # NaN check for price and prev
        if pd.isna(price) or pd.isna(prev):
            return None

        df = hist.copy()
        df["MA5"] = df["Close"].rolling(window=5, min_periods=1).mean()
        df["MA25"] = df["Close"].rolling(window=25, min_periods=1).mean()

        # 3ヶ月分の全データを返すように修正 (tail(30)による切り詰めを削除)
        recent_df = df.reset_index()
        date_col = "Date" if "Date" in recent_df.columns else recent_df.columns[0]

        # P1修正: DataFrame を一度だけ records に変換し、chart/ohlc を同一ループで構築
        def _safe_ohlc(val, fallback=0.0):
            try:
                f = float(val)
                return f if pd.notna(f) else fallback
            except (TypeError, ValueError):
                return fallback

        chart = []
        ohlc_data = []
        # 3mo の営業日データを欠損なく扱えるように余裕を持たせる
        chart_data_limit = 100
        ohlc_data_limit = 365  # OHLC データは詳細用に多めに保持

        # chart_data は軽量維持しつつ、ポートフォリオ3ヶ月集計に十分な点数を保持
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

            ohlc_data.append(
                {
                    "x": ts_ms,
                    "o": _safe_ohlc(rd.get("Open")),
                    "h": _safe_ohlc(rd.get("High")),
                    "l": _safe_ohlc(rd.get("Low")),
                    "c": c_val,
                    "v": vol,
                }
            )

            # 後ろから chart_data_limit 件以内なら chart にも追加
            if num_records - i <= chart_data_limit:
                label = dt.strftime("%m/%d") if hasattr(dt, "strftime") else str(dt)
                ma5_val = _safe_ohlc(rd.get("MA5"), fallback=None)
                ma25_val = _safe_ohlc(rd.get("MA25"), fallback=None)
                chart.append(
                    {
                        "x": ts_ms,
                        "date": label,
                        "price": c_val,
                        "ma5": ma5_val,
                        "ma25": ma25_val,
                    }
                )

        info = get_stock_info_cached(symbol) or {}
        market_state = info.get("marketState", "UNKNOWN")
        currency = info.get("currency") or ("JPY" if market == "jp" else "USD")
        open_val = hist["Open"].iloc[-1] if "Open" in hist.columns else None
        high_val = hist["High"].iloc[-1] if "High" in hist.columns else None
        low_val = hist["Low"].iloc[-1] if "Low" in hist.columns else None
        vol_val = hist["Volume"].iloc[-1] if "Volume" in hist.columns else None

        snapshot_value = int(
            snapshot_ts_ms if snapshot_ts_ms is not None else time.time() * 1000
        )

        return {
            "symbol": symbol,
            "name": choose_display_name(symbol, name, info),
            "market": market,
            "snapshot_ts_ms": snapshot_value,
            "price": _fmt(price),
            "change": _fmt(change),
            "change_percent": _fmt(pct),
            "chart_data": chart,
            "ohlc_data": ohlc_data,
            "high": _fmt(high_val),
            "low": _fmt(low_val),
            "open": _fmt(open_val),
            "volume": _fmt_vol(vol_val),
            "currency": currency,
            "market_state": market_state,
            "shares": shares,
            "avg_price": avg_price,
            "avg_fx_rate": avg_fx_rate,
            "portfolio_value": _fmt(shares * float(price if price else 0)),
            "portfolio_pl": _fmt((float(price if price else 0) - avg_price) * shares),
            "sector": info.get("sector", "Other"),
            "industry": info.get("industry", "Other"),
        }
    except (
        KeyError,
        AttributeError,
        TypeError,
        ValueError,
        pd.errors.EmptyDataError,
    ) as exc:
        app.logger.error("Stock payload build failed (%s): %s", symbol, exc)
        return None


def _handle_yfinance_error(exc, symbol=""):
    """yfinanceのリクエストエラーを解析し、429などのレート制限を検知する"""
    msg = str(exc).lower()
    if "429" in msg or "too many requests" in msg:
        with app_state.yfinance_lock:
            app_state.is_yfinance_rate_limited = True
            # エクスポネンシャルバックオフで待機時間を計算
            app_state.yfinance_429_streak += 1
            backoff_time = min(
                app_state.yfinance_max_backoff_sec,
                (2**app_state.yfinance_429_streak)
                * app_state.yfinance_429_backoff_multiplier,
            )
            app_state.yfinance_rate_limit_until = time.time() + backoff_time
            # セッションを一定期間除外
            yf_session_manager.mark_rate_limited(duration=int(backoff_time))
        app.logger.warning(
            "yfinance rate limit (429) detected! "
            "Backoff initiated. symbol=%s streak=%d backoff=%.1fs",
            symbol,
            app_state.yfinance_429_streak,
            backoff_time,
        )
    elif "timeout" in msg or "timed out" in msg:
        # タイムアウトエラーの場合も軽微なバックオフ
        with app_state.yfinance_lock:
            app_state.yfinance_429_streak = min(app_state.yfinance_429_streak + 1, 3)
        app.logger.debug("yfinance timeout detected. symbol=%s", symbol)
    else:
        # その他のエラーの場合はストリークをリセット
        with app_state.yfinance_lock:
            app_state.yfinance_429_streak = 0


def fetch_stock(
    symbol: str,
    name_or_dict: Union[str, dict],
    market: str,
    snapshot_ts_ms: Optional[int] = None,
) -> Optional[dict]:
    """単一銘柄のデータを取得する"""
    if not acquire_yfinance_slot():
        return None

    try:
        t = safe_get_ticker(symbol)
        if not t:
            return None

        # 多層的な期間フォールバック (3mo -> 5d -> 1d)
        # 週末や上場直後でも「とにかく最新の終値」を確保するための戦略
        hist = pd.DataFrame()
        for p in ["3mo", "5d", "1d"]:
            try:
                hist = normalize_history_frame(
                    t.history(
                        period=p, auto_adjust=True, timeout=YFINANCE_TIMEOUT_SINGLE
                    )
                )
                if len(hist) >= 2:
                    break
            except Exception as e:  # pylint: disable=broad-exception-caught
                app.logger.debug("Fetch failed for %s with period %s: %s", symbol, p, e)
                continue

        # 1行しかなく前日比が出せない場合、さらに期間を広げてでも2行確保を試みる (特殊ケース)
        if 0 < len(hist) < 2:
            try:
                hist = normalize_history_frame(
                    t.history(
                        period="1mo", auto_adjust=True, timeout=YFINANCE_TIMEOUT_SINGLE
                    )
                )
            except Exception as _hst_exc:  # pylint: disable=broad-exception-caught
                app.logger.debug(
                    "Extended history fetch failed for %s: %s", symbol, _hst_exc
                )

        if hist.empty:
            app.logger.warning(
                "No history data found for %s after multiple period attempts", symbol
            )
            return None

        return build_stock_payload(
            symbol, name_or_dict, market, hist, snapshot_ts_ms=snapshot_ts_ms
        )
    except (requests.RequestException, ValueError, KeyError, AttributeError) as exc:
        _handle_yfinance_error(exc, symbol)
        app.logger.error("Stock fetch failed (%s): %s", symbol, exc)
        return None


def extract_batch_history(downloaded, symbol, single_symbol=False):
    """
    バッチ取得されたDataFrameから単一銘柄の履歴を抽出
    MultiIndex/フラット両方の列構造に対応
    """
    if downloaded is None or getattr(downloaded, "empty", True):
        return pd.DataFrame()
    try:
        if not isinstance(downloaded, pd.DataFrame):
            return pd.DataFrame()

        if isinstance(downloaded.columns, pd.MultiIndex):
            # MultiIndex: (symbol, OHLC) スタイル
            try:
                return normalize_history_frame(downloaded[symbol])
            except (KeyError, IndexError):
                return pd.DataFrame()
        elif single_symbol:
            # フラット列: 単一銘柄の場合
            return normalize_history_frame(downloaded)
        else:
            # フラット列なのに複数銘柄: 想定外
            return pd.DataFrame()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        app.logger.debug("extract_batch_history error for %s: %s", symbol, exc)
        return pd.DataFrame()


def fetch_stocks_batch(
    items: List[Tuple[str, str, str]], snapshot_ts_ms: Optional[int] = None
) -> List[dict]:
    """
    複数銘柄をバッチで取得
    タイムアウト対策：失敗時は単一取得にフォールバック
    リトライロジック：失敗時は1回スキップして次回取得を試みる
    """
    if not items:
        return []

    unique_symbols = list(dict.fromkeys(s for s, _, _ in items))

    # 情報キャッシュを事前にwarm-up（7秒タイムアウト）
    # 目的: build_stock_payload() 内の get_stock_info_cached() がキャッシュHITになるよう事前取得する。
    # 戻り値は使用しない（副作用 = TTLCache への書き込みが目的）。
    # cancel() してもタスク実行中はキャンセルできないが、バックグラウンドでwarm-upされれば十分。
    info_futures = [
        app_state.executor.submit(get_stock_info_cached, sym) for sym in unique_symbols
    ]
    done, not_done = wait(info_futures, timeout=7)
    for fut in not_done:
        fut.cancel()
    for fut in done:
        try:
            fut.result()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            app.logger.debug("Error in info fetch: %s", exc)

    batch_histories = {}
    max_retries = 1  # バッチ失敗時は単一フェッチフォールバックのため、リトライは1回のみ
    retry_count = 0

    while retry_count < max_retries:
        if not acquire_yfinance_slot():
            return []

        try:
            # yfinance 1.x系対応: User-Agentを明示的に設定
            # session引数は使用せず、yfinance内部のcurl_cffiベースセッションを使用
            downloaded = yf.download(
                tickers=(
                    unique_symbols if len(unique_symbols) > 1 else unique_symbols[0]
                ),
                period="3mo",
                auto_adjust=True,
                group_by="ticker",
                threads=True,
                progress=False,
                timeout=YFINANCE_TIMEOUT_BATCH,
            )
            single_symbol = len(unique_symbols) == 1
            for symbol in unique_symbols:
                batch_histories[symbol] = extract_batch_history(
                    downloaded, symbol, single_symbol=single_symbol
                )
            break  # 成功時はループを抜ける
        except Exception as exc:  # pylint: disable=broad-exception-caught
            retry_count += 1
            _handle_yfinance_error(exc, "batch")
            if retry_count >= max_retries:
                app.logger.warning(
                    "Batch stock download failed: %s. Will use single fetch fallback.",
                    exc,
                )
                batch_histories = {symbol: pd.DataFrame() for symbol in unique_symbols}
            else:
                app.logger.info(
                    "Batch download attempt %d failed, retrying: %s", retry_count, exc
                )
                time.sleep(YFINANCE_RETRY_WAIT)  # 待機後リトライ

    results = []
    payload_ts_ms = int(
        snapshot_ts_ms if snapshot_ts_ms is not None else time.time() * 1000
    )

    # バッチ取得が完全に失敗した場合、全銘柄を単一取得でフォールバック
    batch_success = False
    for symbol in unique_symbols:
        hist = batch_histories.get(symbol, pd.DataFrame())
        if len(hist) >= 2:
            batch_success = True
            break

    if not batch_success:
        app.logger.warning(
            "Batch fetch failed for all stocks, falling back to single fetch"
        )
        for symbol, name_or_dict, market in items:
            payload = fetch_stock(
                symbol, name_or_dict, market, snapshot_ts_ms=payload_ts_ms
            )
            results.append(payload)
        return results

    # バッチ取得結果の整合性チェックと個別フォールバック
    for symbol, name_or_dict, market in items:
        hist = batch_histories.get(symbol, pd.DataFrame())
        # レコード不足（空データ）は週末の指数等でよく発生するため、即座に個別フェッチを試みる
        if len(hist) < 2:
            app.logger.info(
                "Batch result insufficient for %s (%d rows), attempting robust single fetch.",
                symbol,
                len(hist),
            )
            payload = fetch_stock(
                symbol, name_or_dict, market, snapshot_ts_ms=payload_ts_ms
            )
        else:
            payload = build_stock_payload(
                symbol, name_or_dict, market, hist, snapshot_ts_ms=payload_ts_ms
            )
            if payload is None:
                app.logger.info(
                    "Payload build failed for %s, attempting single fetch fallback.",
                    symbol,
                )
                payload = fetch_stock(
                    symbol, name_or_dict, market, snapshot_ts_ms=payload_ts_ms
                )
        results.append(payload)

    return results


def fetch_index_data(key: str, symbol: str) -> Optional[dict]:
    """
    指数データ取得（タイムアウト・リトライ対策付き）
    """
    max_retries = YFINANCE_MAX_RETRIES

    for attempt in range(max_retries):
        if not acquire_yfinance_slot():
            return None

        try:
            t = safe_get_ticker(symbol)
            if not t:
                continue

            # 多層的な期間フォールバック (3mo -> 5d -> 1d)
            hist = pd.DataFrame()
            for p in ["3mo", "5d", "1d"]:
                try:
                    hist = t.history(
                        period=p, auto_adjust=True, timeout=YFINANCE_TIMEOUT_SINGLE
                    )
                    if len(hist) >= 2:
                        break
                except Exception:  # pylint: disable=broad-exception-caught
                    continue

            if len(hist) < 2:
                # それでも不足する場合、1moで最終試行
                hist = t.history(
                    period="1mo", auto_adjust=True, timeout=YFINANCE_TIMEOUT_SINGLE
                )
                if len(hist) < 2:
                    continue

            # Latest row data
            last_row = hist.iloc[-1]
            prev_close = hist["Close"].iloc[-2]

            price = float(last_row["Close"])
            change = price - float(prev_close)
            pct = (change / float(prev_close) * 100) if prev_close else 0.0

            market_state = "UNKNOWN"
            try:
                info = t.info or {}
                market_state = info.get("marketState", "UNKNOWN")
            except Exception as info_exc:  # pylint: disable=broad-exception-caught
                app.logger.debug("Index info fetch failed for %s: %s", key, info_exc)

            return key, {
                "price": _fmt(price),
                "change": _fmt(change),
                "percent": _fmt(pct),
                "high": _fmt(last_row.get("High")),
                "low": _fmt(last_row.get("Low")),
                "open": _fmt(last_row.get("Open")),
                "volume": _fmt_vol(last_row.get("Volume")),
                "market_state": market_state,
            }
        except Exception as exc:  # pylint: disable=broad-exception-caught
            if attempt < max_retries - 1:
                app.logger.debug(
                    "Index fetch attempt %d failed for %s, retrying: %s",
                    attempt + 1,
                    key,
                    exc,
                )
                time.sleep(YFINANCE_RETRY_WAIT)  # 待機後リトライ
            else:
                app.logger.error(
                    "Index fetch failed for %s after %d attempts: %s",
                    key,
                    max_retries,
                    exc,
                    exc_info=True,
                )

    # 全てのリトライが失敗した後の最終フォールバック: yf.download コールを直接試行
    try:
        app.logger.debug("Index final fallback to yf.download for %s", symbol)
        # downloadは内部で詳細な制御を行っており、historyよりも頑健な場合がある
        try:
            # yfinance 1.x系: downloadも同様にuser_agent不要（内部で自動処理）
            df_dl = yf.download(
                symbol,
                period="5d",
                interval="1d",
                auto_adjust=True,
                progress=False,
                timeout=10,
            )
        except Exception as exc:
            app.logger.debug("yf.download failed for %s: %s", symbol, exc)
            df_dl = pd.DataFrame()
        if not df_dl.empty and len(df_dl) >= 2:
            last_r = df_dl.iloc[-1]
            prev_c = df_dl["Close"].iloc[-2]
            val = float(last_r["Close"])
            chg = val - float(prev_c)
            pct = (chg / float(prev_c) * 100) if prev_c else 0.0
            return key, {
                "price": _fmt(val),
                "change": _fmt(chg),
                "percent": _fmt(pct),
                "high": _fmt(last_r.get("High")),
                "low": _fmt(last_r.get("Low")),
                "open": _fmt(last_r.get("Open")),
                "volume": _fmt_vol(last_r.get("Volume")),
            }
    except Exception as dl_exc:  # pylint: disable=broad-exception-caught
        app.logger.warning("Index absolute fallback failed for %s: %s", symbol, dl_exc)

    return None


# ------------------------------
# Routes
# ------------------------------
@app.route("/api/trending")
def get_trending():
    """トレンド情報を返すAPIエンドポイント"""
    market = normalize_market(request.args.get("market"), default="us") or "us"
    langsearch_api_key = extract_langsearch_api_key(request)
    search_source_hint = "ls" if langsearch_api_key else "ddgs"

    def _fetch():
        try:
            return {
                "trending": _get_market_trending_titles(
                    market, search_source_hint, langsearch_api_key
                )
            }
        except Exception as e:  # pylint: disable=broad-exception-caught
            app.logger.error("Trending fetch error: %s", e)
            return {"trending": []}

    result = get_cached(
        f"trending_list_{market}_{search_source_hint}",
        _fetch,
        duration=300,
        valid_func=lambda payload: bool(
            isinstance(payload, dict) and payload.get("trending")
        ),
    )
    return jsonify(result)


# ------------------------------
def get_default_symbols():
    """市場別のデフォルト銘柄一覧を返す"""
    return {
        "us": list(DEFAULT_US.keys()),
        "jp": list(DEFAULT_JP.keys()),
        "idx": list(DEFAULT_IDX.keys()),
    }


@app.route("/")
@app.route("/setup")
def setup():
    """セットアップページを表示する"""
    return render_template(
        "setup.html",
        model_badge=get_model_badge(),
        default_symbols=get_default_symbols(),
        app_config=get_api_credential_state(),
    )


@app.route("/main")
def main_page():
    """メインページを表示する"""
    return render_template(
        "index.html",
        model_badge=get_model_badge(),
        default_symbols=get_default_symbols(),
        app_config=get_api_credential_state(),
    )


@app.route("/heatmap")
def heatmap_page():
    """ヒートマップページを表示する"""
    return render_template(
        "heatmap.html",
        model_badge=get_model_badge(),
        default_symbols=get_default_symbols(),
        app_config=get_api_credential_state(),
    )


@app.route("/settings")
def settings_page():
    """設定ページを表示する"""
    return render_template(
        "settings.html",
        model_badge=get_model_badge(),
        default_symbols=get_default_symbols(),
        app_config=get_api_credential_state(),
    )


# ------------------------------
# /api/indices
# ------------------------------
def _resolve_indices_for_response():
    """Prefer current cache, but fall back to target cache for fast first paint."""
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
    if current:
        return copy.deepcopy(current)
    return copy.deepcopy(target)


def _resolve_stocks_for_response():
    """Use current cache by default and fill empty markets from target cache."""
    empty = {"us": [], "jp": [], "idx": []}
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
        resolved[market] = copy.deepcopy(current_rows if current_rows else target_rows)
    return resolved


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
    empty = {"us": [], "jp": [], "idx": []}
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


# #region Market Data API Routes
@app.route("/api/indices")
def api_indices():
    """指数データAPIエンドポイント"""
    force = request.args.get("force") == "true"
    if force:
        schedule_sync_all_stocks_now()
    # キャッシュ済みのデータを即座に返す（バックグラウンドスレッドで更新される）
    with app_state.sse_data_lock:
        data = _resolve_indices_for_response()
    if not data:
        _wait_for_initial_market_snapshot("indices", timeout_sec=6.0)
        with app_state.sse_data_lock:
            data = _resolve_indices_for_response()
    return jsonify(data)


# ------------------------------
# /api/stocks
# ------------------------------
@app.route("/api/stocks")
def api_stocks():
    """銘柄データAPIエンドポイント"""
    force = request.args.get("force") == "true"
    if force:
        schedule_sync_all_stocks_now()
    # キャッシュ済みのデータを即座に返す（バックグラウンドスレッドで更新される）
    with app_state.sse_data_lock:
        stocks = _resolve_stocks_for_response()
        indices = _resolve_indices_for_response()
    if not any(stocks.get(m) for m in ("us", "jp", "idx")) and not indices:
        _wait_for_initial_market_snapshot("stocks", timeout_sec=6.0)
        with app_state.sse_data_lock:
            stocks = _resolve_stocks_for_response()
            indices = _resolve_indices_for_response()
    return jsonify(
        {
            "stocks": stocks,
            "indices": indices,
        }
    )


# ------------------------------
# /api/stock-details
# ------------------------------
@app.route("/api/stock-details")
def api_stock_details():
    """銘柄詳細情報APIエンドポイント"""
    symbol = normalize_symbol(request.args.get("symbol"))
    market = normalize_market(request.args.get("market"), default="us")
    if not symbol:
        return error_response(ErrorCode.INVALID_SYMBOL)
    if not market:
        return error_response(ErrorCode.INVALID_MARKET)

    symbol = normalize_symbol_for_market(symbol, market)
    if not is_valid_symbol(symbol):
        return error_response(ErrorCode.INVALID_SYMBOL)

    info = get_stock_info_cached(symbol)
    return jsonify(
        {
            "symbol": symbol,
            "sector": info.get("sector") or None,
            "industry": info.get("industry") or None,
            "market_cap": normalize_optional_number(info.get("marketCap")),
            "pe_ratio": normalize_optional_number(info.get("trailingPE")),
        }
    )


# ------------------------------
# /api/stock-history
# ------------------------------
@app.route("/api/stock-history")
def api_stock_history():
    """銘柄履歴データAPIエンドポイント"""
    symbol = normalize_symbol(request.args.get("symbol"))
    market = normalize_market(request.args.get("market"), default="us")
    period = (request.args.get("period") or "3mo").strip().lower()

    if not symbol:
        return error_response(ErrorCode.INVALID_SYMBOL)
    if not market:
        return error_response(ErrorCode.INVALID_MARKET)
    if period not in VALID_HISTORY_PERIODS:
        return error_response(ErrorCode.INVALID_PERIOD)
    symbol = normalize_symbol_for_market(symbol, market)

    def _history_with_timeout(ticker_obj, period_value, interval_value):
        now = time.time()
        cleanup_history_circuit_state(now_ts=now)
        with app_state.history_circuit_lock:
            state = app_state.history_circuit_state.get(symbol, {})
            open_until = state.get("open_until", 0.0)
        if now < open_until:
            app.logger.info(
                "stock-history circuit open symbol=%s wait_left=%.1fs",
                symbol,
                max(0.0, open_until - now),
            )
            return pd.DataFrame()

        try:
            result = ticker_obj.history(
                period=period_value,
                interval=interval_value,
                auto_adjust=True,
                timeout=YFINANCE_TIMEOUT_SINGLE,
            )
        except (TimeoutError, RequestsTimeout, CurlRequestsTimeout) as timeout_exc:
            with app_state.history_circuit_lock:
                if symbol not in app_state.history_circuit_state:
                    app_state.history_circuit_state[symbol] = {
                        "timeout_streak": 0,
                        "open_until": 0.0,
                    }
                state = app_state.history_circuit_state[symbol]
                state["timeout_streak"] = state.get("timeout_streak", 0) + 1
                if state["timeout_streak"] >= HISTORY_CIRCUIT_BREAKER_THRESHOLD:
                    state["open_until"] = now + HISTORY_CIRCUIT_BREAKER_OPEN_SEC
                    state["timeout_streak"] = 0
            app.logger.debug(
                "stock-history timeout exception symbol=%s err=%s",
                symbol,
                timeout_exc,
            )
            app.logger.warning(
                "stock-history timeout symbol=%s period=%s interval=%s timeout=%ss",
                symbol,
                period_value,
                interval_value,
                YFINANCE_TIMEOUT_SINGLE,
            )
            return pd.DataFrame()
        with app_state.history_circuit_lock:
            if symbol in app_state.history_circuit_state:
                app_state.history_circuit_state[symbol]["timeout_streak"] = 0
        return result

    def _fetch_history():
        try:
            t = safe_get_ticker(symbol)
            if not t:
                return {
                    "error": "銘柄情報が取得できませんでした。",
                    "symbol": symbol,
                }

            # 1d の場合は短いインターバルで取得を試みる
            interval = "5m" if period == "1d" else "1d"
            if period == "5d":
                interval = "15m"

            # MA25 計算のために日足では十分な期間を拡張して取得する
            extended_period_map = {
                "1mo": "6mo",
                "3mo": "6mo",
                "6mo": "1y",
                "1y": "2y",
                "2y": "5y",
                "5y": "10y",
            }
            extended_period = period
            if interval == "1d" and period in extended_period_map:
                extended_period = extended_period_map[period]

            hist = _history_with_timeout(t, extended_period, interval)
            hist = normalize_history_frame(hist)

            # フォールバック 1: 1d/5m が失敗 → 1d/1d を試す
            if hist.empty and period == "1d" and interval == "5m":
                app.logger.info("Fallback 1 for %s: 1d/5m failed, trying 1d/1d", symbol)
                hist = _history_with_timeout(t, "1d", "1d")
                hist = normalize_history_frame(hist)
                interval = "1d"

            # フォールバック 2: 空またはデータが少なすぎる場合 → 5d/1d を試す
            if (hist.empty or len(hist) < 1) and period in ["1d", "5d"]:
                app.logger.info("%s: trying 5d/1d", symbol)
                hist = _history_with_timeout(t, "5d", "1d")
                hist = normalize_history_frame(hist)
                interval = "1d"

            if hist.empty:
                return {
                    "error": "データが見つかりませんでした。銘柄が上場廃止されているか、選択した期間のデータが存在しない可能性があります。",
                    "symbol": symbol,
                    "interval_used": interval,
                    "period_requested": period,
                }

            # MA計算 (日足の場合のみ)
            # 拡張取得した全データで MA を計算するため NaN になる先頭行が減る
            if interval == "1d":
                if len(hist) >= 5:
                    hist["MA5"] = hist["Close"].rolling(window=5).mean()
                if len(hist) >= 25:
                    hist["MA25"] = hist["Close"].rolling(window=25).mean()

                # 元のピリオドに対応するカレンダー期間でデータをトリミング
                period_offset_map = {
                    "1mo": pd.DateOffset(months=1),
                    "3mo": pd.DateOffset(months=3),
                    "6mo": pd.DateOffset(months=6),
                    "1y": pd.DateOffset(years=1),
                    "2y": pd.DateOffset(years=2),
                    "5y": pd.DateOffset(years=5),
                }
                if extended_period != period and period in period_offset_map:
                    cutoff = hist.index[-1] - period_offset_map[period]
                    hist = hist[hist.index >= cutoff]

            data_list = []
            for dt, row in hist.iterrows():
                try:
                    vol = (
                        int(float(row["Volume"]))
                        if ("Volume" in row and pd.notna(row["Volume"]))
                        else 0
                    )
                except (TypeError, ValueError, KeyError):
                    vol = 0
                d = {
                    "x": dt.timestamp() * 1000,
                    "o": float(row["Open"]) if pd.notna(row["Open"]) else 0,
                    "h": float(row["High"]) if pd.notna(row["High"]) else 0,
                    "l": float(row["Low"]) if pd.notna(row["Low"]) else 0,
                    "c": float(row["Close"]) if pd.notna(row["Close"]) else 0,
                    "v": vol,
                }
                if "MA5" in row.index and pd.notna(row["MA5"]):
                    d["ma5"] = float(row["MA5"])
                if "MA25" in row.index and pd.notna(row["MA25"]):
                    d["ma25"] = float(row["MA25"])
                data_list.append(d)

            return {"symbol": symbol, "history": data_list, "interval_used": interval}
        except Exception as exc:  # pylint: disable=broad-exception-caught
            app.logger.error(
                "Stock history fetch failed (%s, %s): %s", symbol, period, exc
            )
            return {
                "error": get_error_message(ErrorCode.FETCH_FAILED, lang="ja"),
                "error_code": int(ErrorCode.FETCH_FAILED),
                "symbol": symbol,
            }

    # キャッシュキーには symbol と period を含める
    cache_key = f"hist_{symbol}_{period}"
    # 短期間はキャッシュを短く
    duration = 60 if period in ["1d", "5d"] else 3600
    return jsonify(get_cached(cache_key, _fetch_history, duration=duration))


# #endregion Market Data API Routes


# #region Search API
@app.route("/api/search")
def api_search():
    """銘柄検索APIエンドポイント"""
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return error_response(
            ErrorCode.INVALID_INPUT, details={"reason": "検索ワードは2文字以上"}
        )

    def _search():
        try:
            # yfinance 1.x系: Searchも同様にuser_agent不要
            s = yf.Search(q)
            quotes = getattr(s, "quotes", []) or []
            results = []
            for item in quotes[:10]:
                sym = item.get("symbol")
                if not sym:
                    continue
                results.append(
                    {
                        "symbol": sym,
                        "name": item.get("shortname")
                        or item.get("longname")
                        or "名称不明",
                        "exchange": item.get("exchange") or item.get("exchDisp") or "",
                    }
                )
            return {"results": results}
        except (requests.RequestException, ValueError, KeyError, AttributeError) as exc:
            app.logger.error("Search API failed (%s): %s", q, exc)
            return {
                "error": get_error_message(ErrorCode.API_SERVICE_ERROR, lang="ja"),
                "error_code": int(ErrorCode.API_SERVICE_ERROR),
            }

    return jsonify(get_cached(f"search_{q}", _search, duration=60))


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
    """キャッシュから銘柄を削除する"""
    with app_state.sse_data_lock:
        for cache in (app_state.current_stocks_cache, app_state.target_stocks_cache):
            if market not in cache:
                cache[market] = []
            cache[market] = [s for s in cache[market] if s.get("symbol") != symbol]


# ------------------------------
# /api/stocks/add
# ------------------------------
@app.route("/api/stocks/add", methods=["POST"])
def api_add_stock():
    """銘柄追加APIエンドポイント"""
    if not _is_local_request(request):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    data = _parse_json_request()
    if data is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )
    raw_symbol = data.get("symbol")
    name = normalize_text(data.get("name"))
    market = normalize_market(data.get("market"), default="")
    symbol = normalize_symbol_for_market(raw_symbol, market)

    if not symbol or not name or not market:
        return error_response(
            ErrorCode.MISSING_REQUIRED_FIELD,
            details={"fields": ["symbol", "name", "market"]},
        )
    if not is_valid_symbol(symbol):
        return error_response(ErrorCode.INVALID_SYMBOL)

    with app_state.user_stocks_lock:
        if (
            (market == "us" and (symbol in DEFAULT_US or symbol in app_state.user_us))
            or (
                market == "jp" and (symbol in DEFAULT_JP or symbol in app_state.user_jp)
            )
            or (
                market == "idx"
                and (symbol in DEFAULT_IDX or symbol in app_state.user_idx)
            )
        ):
            return error_response(
                ErrorCode.INVALID_INPUT, details={"reason": "既に追加済み"}
            )

        if market == "us":
            app_state.user_us[symbol] = name
        elif market == "jp":
            app_state.user_jp[symbol] = name
        else:
            app_state.user_idx[symbol] = name

    save_user_stocks()
    invalidate_stock_caches(symbol)
    ensure_stock_placeholder_in_caches(symbol, name, market)

    schedule_sync_all_stocks_now()
    return jsonify({"success": True})


# ------------------------------
# /api/stocks/delete
# ------------------------------
@app.route("/api/stocks/delete", methods=["POST"])
def api_delete_stock():
    """銘柄削除APIエンドポイント"""
    if not _is_local_request(request):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    data = _parse_json_request()
    if data is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )
    raw_symbol = data.get("symbol")
    market = normalize_market(data.get("market"), default="")
    symbol = normalize_symbol_for_market(raw_symbol, market)

    if not symbol or not market:
        return error_response(
            ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["symbol", "market"]}
        )

    with app_state.user_stocks_lock:
        if market == "us":
            app_state.user_us.pop(symbol, None)
        elif market == "jp":
            app_state.user_jp.pop(symbol, None)
        else:
            app_state.user_idx.pop(symbol, None)

    save_user_stocks()
    invalidate_stock_caches(symbol)
    remove_stock_from_caches(symbol, market)

    schedule_sync_all_stocks_now()
    return jsonify({"success": True})


# ------------------------------
# /api/stocks/portfolio
# ------------------------------
@app.route("/api/stocks/portfolio", methods=["POST"])
def api_update_portfolio():
    """ポートフォリオ更新APIエンドポイント"""
    if not _is_local_request(request):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    data = _parse_json_request()
    if data is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )
    raw_symbol = data.get("symbol")
    market = normalize_market(data.get("market"), default="")
    symbol = normalize_symbol_for_market(raw_symbol, market)

    if not symbol or not market:
        return error_response(
            ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["symbol", "market"]}
        )
    if not is_valid_symbol(symbol):
        return error_response(ErrorCode.INVALID_SYMBOL)

    try:
        shares_raw = data.get("shares")
        avg_price_raw = data.get("avg_price")
        avg_fx_rate_raw = data.get("avg_fx_rate")
        if shares_raw is None or str(shares_raw).strip() == "":
            return error_response(
                ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["shares"]}
            )
        if avg_price_raw is None or str(avg_price_raw).strip() == "":
            return error_response(
                ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["avg_price"]}
            )

        shares = parse_non_negative_float(
            shares_raw, "shares", max_value=PORTFOLIO_SHARES_MAX
        )
        avg_price = parse_non_negative_float(
            avg_price_raw, "avg_price", max_value=PORTFOLIO_AVG_PRICE_MAX
        )
        avg_fx_rate = None
        if avg_fx_rate_raw is not None and str(avg_fx_rate_raw).strip():
            fx_val = parse_non_negative_float(
                avg_fx_rate_raw, "avg_fx_rate", max_value=1_000_000.0
            )
            if fx_val <= 0:
                return error_response(
                    ErrorCode.INVALID_INPUT,
                    details={"reason": "avg_fx_rateは正の数値である必要があります"},
                )
            avg_fx_rate = fx_val
        if shares * avg_price > PORTFOLIO_TOTAL_VALUE_MAX:
            return error_response(
                ErrorCode.INVALID_INPUT,
                details={
                    "reason": f"portfolio total value must be <= {PORTFOLIO_TOTAL_VALUE_MAX}"
                },
            )
    except ValueError as exc:
        return error_response(ErrorCode.INVALID_INPUT, details={"reason": str(exc)})

    with app_state.user_stocks_lock:
        container = None
        if market == "us":
            container = app_state.user_us
        elif market == "jp":
            container = app_state.user_jp
        elif market == "idx":
            container = app_state.user_idx

        if container is None:
            return error_response(ErrorCode.INVALID_MARKET)

        # If stock does not exist in user list, or it's implicitly a default
        if symbol not in container:
            # We need to add it to user map, migrating from default if it was default
            # But we only store customized metadata if they edit a default stock
            # To avoid complex logic, we assume we can add it to _user_xxx

            # let's find the name first
            name = symbol
            if market == "us" and symbol in DEFAULT_US:
                name = DEFAULT_US[symbol]
            elif market == "jp" and symbol in DEFAULT_JP:
                name = DEFAULT_JP[symbol]
            elif market == "idx" and symbol in DEFAULT_IDX:
                name = DEFAULT_IDX[symbol]

            container[symbol] = {"name": name, "shares": shares, "avg_price": avg_price}
            if avg_fx_rate is not None:
                container[symbol]["avg_fx_rate"] = avg_fx_rate
        else:
            # Update existing
            val = container[symbol]
            if isinstance(val, str):
                val = {
                    "name": val,
                    "shares": shares,
                    "avg_price": avg_price,
                }
            else:
                val["shares"] = shares
                val["avg_price"] = avg_price

            if avg_fx_rate is not None:
                val["avg_fx_rate"] = avg_fx_rate
            else:
                val.pop("avg_fx_rate", None)

            container[symbol] = val

    save_user_stocks()
    invalidate_stock_caches(symbol)

    # フロントエンドの fetchInitialStocks や SSE に即座に反映させるため両方のキャッシュを更新する
    with app_state.sse_data_lock:
        for cache in (app_state.current_stocks_cache, app_state.target_stocks_cache):
            if market not in cache:
                cache[market] = []
            target_list = cache.get(market, [])
            found = False
            for s in target_list:
                if s.get("symbol") == symbol:
                    s["shares"] = shares
                    s["avg_price"] = avg_price
                    if avg_fx_rate is not None:
                        s["avg_fx_rate"] = avg_fx_rate
                    else:
                        s.pop("avg_fx_rate", None)
                    found = True
                    break
            if not found:
                name = symbol
                if market == "us":
                    name = DEFAULT_US.get(symbol, symbol)
                elif market == "jp":
                    name = DEFAULT_JP.get(symbol, symbol)
                elif market == "idx":
                    name = DEFAULT_IDX.get(symbol, symbol)
                target_list.append(
                    {
                        "symbol": symbol,
                        "name": name,
                        "market": market,
                        "price": "--",
                        "change": "--",
                        "change_percent": "--",
                        "chart_data": [],
                        "shares": shares,
                        "avg_price": avg_price,
                    }
                )
    schedule_sync_all_stocks_now()
    return jsonify({"success": True})


# ------------------------------
# /api/stocks/add_ext (拡張機能用)
# ------------------------------
@app.route("/api/stocks/add_ext", methods=["POST"])
def api_add_stock_ext():
    """拡張機能用銘柄追加APIエンドポイント"""
    if not _is_local_request(request):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    data = _parse_json_request()
    if data is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )
    raw_symbol = data.get("symbol")
    market = normalize_market(data.get("market"), default="us")
    symbol = normalize_symbol_for_market(raw_symbol, market)

    if not symbol:
        return error_response(
            ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["symbol"]}
        )
    if not market:
        return error_response(ErrorCode.INVALID_MARKET)
    if not is_valid_symbol(symbol):
        return error_response(ErrorCode.INVALID_SYMBOL)

    # Bug#3修正: デフォルト銘柄との重複チェックも含める
    added = False
    with app_state.user_stocks_lock:
        if market == "jp":
            if symbol not in app_state.user_jp and symbol not in DEFAULT_JP:
                app_state.user_jp[symbol] = (
                    symbol  # 名前は後でYahoo Financeから取得される
                )
                added = True
        elif market == "us":
            if symbol not in app_state.user_us and symbol not in DEFAULT_US:
                app_state.user_us[symbol] = symbol
                added = True
        else:
            if symbol not in app_state.user_idx and symbol not in DEFAULT_IDX:
                app_state.user_idx[symbol] = symbol
                added = True

    if added:
        save_user_stocks()
        invalidate_stock_caches(symbol)
        ensure_stock_placeholder_in_caches(symbol, symbol, market)

        # 同期をキック
        schedule_sync_all_stocks_now()
        return jsonify({"ok": True, "message": f"Added {symbol} to {market}"})
    return jsonify({"ok": True, "message": f"{symbol} already exists in {market}"})


# ------------------------------
# /api/stocks/reset


# ------------------------------
@app.route("/api/stocks/reset", methods=["POST"])
def api_reset_stocks():
    """銘柄リセットAPIエンドポイント"""
    if not _is_local_request(request):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    with app_state.user_stocks_lock:
        app_state.user_us, app_state.user_jp, app_state.user_idx = {}, {}, {}
    save_user_stocks()
    with app_state.sse_data_lock:
        app_state.current_stocks_cache = {"us": [], "jp": [], "idx": []}
        app_state.target_stocks_cache = {"us": [], "jp": [], "idx": []}
        app_state.current_indices_cache = {}
        app_state.target_indices_cache = {}
    clear_cache_prefix("stocks")
    schedule_sync_all_stocks_now()
    return jsonify({"success": True})


# ------------------------------
# /api/heatmap
# ------------------------------
@app.route("/api/heatmap")
def api_heatmap():
    """ヒートマップデータAPIエンドポイント"""
    market = normalize_market(request.args.get("market"), default="us")
    if not market:
        return error_response(ErrorCode.INVALID_MARKET)
    if market not in ("us", "jp"):
        return error_response(
            ErrorCode.INVALID_INPUT,
            details={"reason": "heatmap market は us/jp のみ対応です"},
        )
    symbols = POPULAR_US if market == "us" else POPULAR_JP

    def _fetch_heatmap():
        items = []
        for s in symbols:
            items.append((s, "", market))  # fallback name is empty

        fetched = fetch_stocks_batch(items)
        results = []
        for item in fetched:
            if not item:
                continue

            # P2修正: build_stock_payload が既に sector/industry を含むため get_stock_info_cached の再呼び出しを削除
            # market_cap のみ別途必要なため info から取得（ただし build_stock_payload 経由でキャッシュ済み）
            info = get_stock_info_cached(
                item["symbol"]
            )  # ここではキャッシュHITのみ（再フェッチなし）
            results.append(
                {
                    "symbol": item["symbol"],
                    "name": item["name"],
                    "price": item["price"],
                    "change_percent": item["change_percent"],
                    "market_cap": normalize_optional_number(info.get("marketCap")) or 0,
                    "sector": item.get("sector") or info.get("sector") or "Other",
                }
            )
        return {"stocks": results}

    cache_key = f"heatmap_{market}"
    return jsonify(get_cached(cache_key, _fetch_heatmap, duration=300))


# #region Chat API
@app.route("/api/chat", methods=["POST"])
def api_chat():
    """チャットAPIエンドポイント"""
    if not _is_local_request(request):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    api_key = extract_api_key(request)
    if not api_key:
        return error_response(ErrorCode.INVALID_API_KEY, status_code=401)

    data = _parse_json_request()
    if data is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )
    market = normalize_market(data.get("market"), default="us")
    symbol = normalize_symbol_for_market(data.get("symbol"), market)
    user_msg = (data.get("message") or "").strip()
    if not market:
        return error_response(ErrorCode.INVALID_MARKET)
    if not symbol or not user_msg:
        return error_response(
            ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["symbol", "message"]}
        )

    app.logger.info(
        "api_chat input id=%s market=%s symbol=%s msg_len=%d",
        getattr(g, "request_id", "-"),
        market,
        symbol,
        len(user_msg),
    )

    chat_key = f"{market}:{symbol}"

    # Bug#1修正: _chat_history への読み書きをロックで保護（複数スレッドからの同時アクセス対策）
    with app_state.chat_history_lock:
        if chat_key in app_state.chat_history:
            # LRU順を保つため、利用したキーを末尾へ移動
            app_state.chat_history[chat_key] = app_state.chat_history.pop(chat_key)
        else:
            app_state.chat_history[chat_key] = [
                {
                    "role": "system",
                    "content": f"あなたは{symbol}銘柄の専門家です。簡潔かつ投資家に有益な回答をしてください。",
                }
            ]

        # チャット履歴のキー数上限（最大50銘柄分）
        if len(app_state.chat_history) > 50:
            oldest_key = next(iter(app_state.chat_history))
            app_state.chat_history.pop(oldest_key, None)

        app_state.chat_history[chat_key].append({"role": "user", "content": user_msg})

        if len(app_state.chat_history[chat_key]) > 11:
            app_state.chat_history[chat_key] = [
                app_state.chat_history[chat_key][0]
            ] + app_state.chat_history[chat_key][-10:]

        # Mistral 呼び出し用にメッセージをコピーして取得（ロック内での長時間I/O回避）
        messages_snapshot = list(app_state.chat_history[chat_key])

    response = call_mistral_chat(api_key, messages_snapshot, max_tokens=420)
    ai_content = extract_chat_content(response)

    with app_state.chat_history_lock:
        if chat_key in app_state.chat_history:
            app_state.chat_history[chat_key].append(
                {"role": "assistant", "content": ai_content}
            )
    return jsonify({"reply": ai_content})


# #endregion Chat API

# #region News API


# ------------------------------
# /api/news
# trend_sources + LangSearch(失敗時DDGSフォールバック) で情報取得し、Mistralで要約
# ------------------------------
# #region AI Integration Routes & Logic
@app.route("/api/news", methods=["POST"])
def api_news():
    """ニュースAPIエンドポイント"""
    if not _is_local_request(request):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    api_key = extract_api_key(request)
    langsearch_api_key = extract_langsearch_api_key(request)
    if not api_key:
        return error_response(ErrorCode.INVALID_API_KEY, status_code=401)

    app.logger.info(
        "api_news start id=%s langsearch=%s",
        getattr(g, "request_id", "-"),
        bool(langsearch_api_key),
    )

    merged_trends = []
    trends_context = ""

    def _coerce_news_section_text(raw):
        if not raw:
            return ""
        return _flatten(raw)

    def _coerce_news_section_text_v2(raw):
        """Enhanced version of coerce for news section text with better truncation handling."""
        if not raw:
            return ""

        # If it's a list or dict, flatten it normally
        if isinstance(raw, (list, dict)):
            return _coerce_news_section_text(raw)

        # If it's a string, it might be a truncated JSON fragment or a raw list of lines
        text = str(raw).strip()

        # If it looks like a truncated sentence at the end (no punctuation), try to clean it
        if text and text[-1] not in '。！？!?."}]':
            # Search for the last complete sentence or line
            last_punc = max(
                text.rfind("。"), text.rfind("？"), text.rfind("！"), text.rfind("\n")
            )
            if last_punc != -1:
                text = text[: last_punc + 1].strip()

        return _coerce_news_section_text(text)

    def _flatten(item, current_depth=0, max_depth=5):
        if current_depth > max_depth:
            return str(item).strip()
        if item is None:
            return ""
        if isinstance(item, (int, float, bool)):
            return str(item)
        if isinstance(item, str):
            txt = item.strip()
            if not txt:
                return ""
            txt = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", txt)
            txt = re.sub(r"\s*```$", "", txt).strip()
            # JSON文字列なら再帰的にフラット化
            try:
                parsed_inner = json.loads(txt)
                return _flatten(parsed_inner, current_depth + 1, max_depth)
            except (json.JSONDecodeError, ValueError):
                # 末尾カンマ等を考慮した extract_json_payload のロジックの一部を適用
                if txt.startswith("{") or txt.startswith("["):
                    fixed_txt = re.sub(r",\s*([\]}])", r"\1", txt)
                    try:
                        return _flatten(json.loads(fixed_txt), current_depth + 1, max_depth)
                    except (json.JSONDecodeError, ValueError):
                        pass

            # JSON風の行から値だけを抽出
            values = []
            for line in txt.splitlines():
                s = line.strip().rstrip(",")
                if not s or s in {"{", "}", "[", "]"}:
                    continue
                m = re.match(
                    r'^"(?:topic|summary|details|market_impact|title|description|text)"\s*:\s*"?(.*?)"?$',
                    s,
                )
                if m:
                    val = m.group(1).strip().strip('"')
                    if val:
                        values.append(val)
                    continue
                # キー名がない場合でも引用符で囲まれていれば抽出
                if s.startswith('"') and s.endswith('"'):
                    values.append(s.strip('"'))
                else:
                    values.append(s)
            if values:
                # 重複を落として可読化
                uniq = []
                for v in values:
                    if v and v not in uniq and not _is_noise_news_line(v):
                        uniq.append(v)
                return "\n".join(uniq)
            return txt

        if isinstance(item, list):
            lines = []
            for x in item:
                t = _flatten(x, current_depth + 1, max_depth)
                if t:
                    lines.extend(
                        [seg.strip() for seg in str(t).splitlines() if seg.strip()]
                    )
            uniq = []
            for v in lines:
                if v not in uniq:
                    uniq.append(v)
            return "\n".join(uniq)

        if isinstance(item, dict):
            topic = str(item.get("topic") or item.get("title") or "").strip()
            summary = str(
                item.get("summary")
                or item.get("details")
                or item.get("description")
                or ""
            ).strip()
            impact = item.get("market_impact")
            parts = []
            if topic:
                parts.append(topic)
            if summary:
                parts.append(summary)
            if isinstance(impact, dict):
                impact_lines = []
                for k, v in impact.items():
                    kv = f"{str(k).strip()}: {str(v).strip()}".strip()
                    if kv and not kv.endswith(":"):
                        impact_lines.append(kv)
                if impact_lines:
                    parts.append(" | ".join(impact_lines))
            elif impact:
                parts.append(str(impact).strip())

            if parts:
                return " - ".join([p for p in parts if p])

            misc = []
            for k, v in item.items():
                kv = f"{str(k).strip()}: {str(v).strip()}".strip()
                if kv and not kv.endswith(":"):
                    misc.append(kv)
            return " | ".join(misc)

        return str(item).strip()

    def _is_noise_news_line(line):
        s = str(line or "").strip()
        if not s:
            return True
        lower = s.lower()
        if (
            lower.startswith("source:")
            or lower.startswith("date:")
            or lower.startswith("url:")
        ):
            return True
        if "<a " in lower or "<li" in lower or "<ol" in lower or "<ul" in lower:
            return True
        if re.search(r"<[^>]+>", s):
            return True
        if lower.startswith("http://") or lower.startswith("https://"):
            return True
        if "news.google.com/rss/articles" in lower:
            return True
        # 日本語テキストは文字数ベースで判定（スペース区切りが少ないため語数チェックは不正確）
        has_cjk = bool(re.search(r"[\u3040-\u9fff]", s))
        if has_cjk:
            # CJK文字を含む場合：文字数が10文字以下はノイズ扱い
            if len(s) <= 10:
                return True
        else:
            word_count = len(re.findall(r"\S+", s))
            if word_count <= 5 and not re.search(r"[。！？!?.]", s):
                return True
        return False

    def _parse_lines(text):
        lines = []
        for line in str(text or "").splitlines():
            s = re.sub(r"^\s*(?:[-*•▪]|\d+[.)])\s*", "", line).strip()
            s = s.strip("\"'")
            if s and not _is_noise_news_line(s):
                lines.append(s)
        return lines

    def _normalize_mistral_news_lines(section_text, max_lines=12):
        out = []
        seen = set()

        def push_unique(line):
            t = str(line or "").strip()
            if not t:
                return
            if _is_noise_news_line(t):
                return
            key = t.lower()
            if key in seen:
                return
            seen.add(key)
            out.append(t)

        for line in _parse_lines(section_text):
            push_unique(line)
        return "\n".join(out[:max_lines])

    try:
        search_source_hint = "ls" if langsearch_api_key else "ddgs"
        try:
            fut_us_ctx = app_state.news_executor.submit(
                get_cached_context_with_negative_cache,
                f"market_news_context_us_{search_source_hint}",
                lambda: collect_market_news_context(
                    "us", langsearch_api_key=langsearch_api_key
                ),
                300,
                90,
                False,
            )
            fut_jp_ctx = app_state.news_executor.submit(
                get_cached_context_with_negative_cache,
                f"market_news_context_jp_{search_source_hint}",
                lambda: collect_market_news_context(
                    "jp", langsearch_api_key=langsearch_api_key
                ),
                300,
                90,
                False,
            )
            fut_us_trends = app_state.news_executor.submit(
                collect_market_trending_titles,
                "us",
                8,
                langsearch_api_key,
            )
            fut_jp_trends = app_state.news_executor.submit(
                collect_market_trending_titles,
                "jp",
                8,
                langsearch_api_key,
            )

            done, not_done = wait(
                [fut_us_ctx, fut_jp_ctx, fut_us_trends, fut_jp_trends],
                timeout=NEWS_CONTEXT_WAIT_TIMEOUT,
            )
            for fut in not_done:
                fut.cancel()
            if not_done:
                pending_targets = []
                future_targets_for_log = {
                    fut_us_ctx: "us_context",
                    fut_jp_ctx: "jp_context",
                    fut_us_trends: "us_trends",
                    fut_jp_trends: "jp_trends",
                }
                for pending_future in not_done:
                    pending_targets.append(
                        future_targets_for_log.get(pending_future, "unknown")
                    )
                app.logger.warning(
                    "News context gather timeout: completed=%d pending=%d timeout=%ss pending_targets=%s",
                    len(done),
                    len(not_done),
                    NEWS_CONTEXT_WAIT_TIMEOUT,
                    pending_targets,
                )

            # 収集完了後に各結果を取得 & 取得状況を追跡
            us_context = ""
            jp_context = ""
            us_trends = []
            jp_trends = []
            retrieve_status = {
                "us": "pending",
                "jp": "pending",
                "trends": "pending",
            }

            future_targets = {
                fut_us_ctx: "us_context",
                fut_jp_ctx: "jp_context",
                fut_us_trends: "us_trends",
                fut_jp_trends: "jp_trends",
            }
            for fut in done:
                target = future_targets.get(fut)
                try:
                    result = fut.result()
                    if target == "us_context":
                        us_context = result or ""
                        retrieve_status["us"] = "success" if us_context else "empty"
                    elif target == "jp_context":
                        jp_context = result or ""
                        retrieve_status["jp"] = "success" if jp_context else "empty"
                    elif target == "us_trends":
                        us_trends = result if isinstance(result, list) else []
                        retrieve_status["trends"] = (
                            "success" if (us_trends or jp_trends) else "empty"
                        )
                    elif target == "jp_trends":
                        jp_trends = result if isinstance(result, list) else []
                        retrieve_status["trends"] = (
                            "success" if (us_trends or jp_trends) else "empty"
                        )
                except (RuntimeError, ValueError, KeyError, AttributeError) as fut_exc:
                    app.logger.warning(
                        "Future result retrieval error (%s): %s", target, fut_exc
                    )
                    if target == "us_context":
                        retrieve_status["us"] = "error"
                    elif target == "jp_context":
                        retrieve_status["jp"] = "error"
                    elif target in ("us_trends", "jp_trends"):
                        retrieve_status["trends"] = "error"
                    continue

            # 完了しなかったタスクはタイムアウト状態
            for fut in not_done:
                target = future_targets.get(fut)
                if target == "us_context":
                    retrieve_status["us"] = "timeout"
                elif target == "jp_context":
                    retrieve_status["jp"] = "timeout"
                elif target in ("us_trends", "jp_trends"):
                    retrieve_status["trends"] = "timeout"

            seen_trends = set()
            merged_trends = []
            for title in list(us_trends) + list(jp_trends):
                t = str(title or "").strip()
                key = t.lower()
                if not t or key in seen_trends:
                    continue
                seen_trends.add(key)
                merged_trends.append(t)
                if len(merged_trends) >= 12:
                    break
            trends_context = "\n".join(f"- {title}" for title in merged_trends)

        except (RuntimeError, ValueError, KeyError) as ctx_err:
            app.logger.warning("News context gather error: %s", ctx_err)
            us_context = ""
            jp_context = ""
            merged_trends = []
            trends_context = ""
            retrieve_status = {
                "us": "error",
                "jp": "error",
                "trends": "error",
            }

        instructions = (
            "あなたは金融市場の専門アナリストです。\n"
            "提供される情報を元に、簡潔だが具体性のある要約を提供してください。\n"
            "必ずJSONのみを返してください。\n"
            "各セクション（us, jp, trends）は完全に独立していて、他のセクションの情報を混ぜないでください。\n"
            "各行は1つの事実を述べ、出来事・背景・市場への影響の少なくとも2要素を含めてください。\n"
            "見出しの単語列挙ではなく、分析文として書いてください。"
        )

        combined_prompt = (
            "以下の3つの情報をそれぞれ独立して要約し、混ぜないでください。\n\n"
            "【US市場情報】\n"
            f"{us_context or 'データなし'}\n\n"
            "【日本市場情報】\n"
            f"{jp_context or 'データなし'}\n\n"
            "【トレンド情報】\n"
            f"{trends_context or 'データなし'}\n\n"
            "返すJSONは次の形式でなければなりません。原則はJSONオブジェクトのみを返してください。\n"
            "必要な場合のみ ```json ... ``` で囲っても構いません。前置き説明は禁止です。\n"
            "重要: us/jp/trends の値は必ず文字列にしてください。配列やオブジェクトは禁止です。\n"
            "文字列内で 6-8 行を改行区切りで書いてください（1行=1話題）。\n"
            "各行は30〜80文字程度で、何が起きたか / なぜ重要か / 市場への影響 をできるだけ含めてください。\n"
            "重要: 出力は『分析要約』のみ。ニュース見出しの生引用、source/date/url、HTMLタグ、URL文字列は絶対に含めないでください。\n"
            '{"us":"1行目\\n2行目","jp":"1行目\\n2行目","trends":"1行目\\n2行目"}'
        )

        # ニュース要約は毎回最新化する。コンテキスト側のみをキャッシュし、LLM出力は再利用しない。
        llm_hash_src = f"{us_context}|{jp_context}|{trends_context}".encode(
            "utf-8", errors="ignore"
        )
        llm_hash = hashlib.sha256(llm_hash_src).hexdigest()
        app.logger.info(
            "News bundle refresh id=%s context_hash=%s",
            getattr(g, "request_id", "-"),
            llm_hash[:12],
        )
        app.logger.info(
            "News prompt prepared id=%s us_chars=%s jp_chars=%s trends_titles=%s",
            getattr(g, "request_id", "-"),
            len(us_context or ""),
            len(jp_context or ""),
            len(merged_trends or []),
        )

        def _generate_news_bundle():
            def _best_effort_parse_news_sections(raw_text):
                text = str(raw_text or "").strip()
                if not text:
                    return None

                # Prefer fenced JSON body when present.
                fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
                if fence:
                    text = fence.group(1).strip()

                # Try strict JSON first.
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        return {
                            "us": _coerce_news_section_text(parsed.get("us") or ""),
                            "jp": _coerce_news_section_text(parsed.get("jp") or ""),
                            "trends": _coerce_news_section_text(
                                parsed.get("trends") or ""
                            ),
                        }
                except (json.JSONDecodeError, ValueError) as parse_exc:
                    app.logger.debug("News JSON strict parse failed: %s", parse_exc)

                # 末尾截断対応：閉じ括弧が不足している場合、追加して試す
                try:
                    depth = text.count("{") - text.count("}")
                    if 0 < depth <= 3:
                        repaired = text.rstrip() + "}" * depth
                        parsed = json.loads(repaired)
                        if isinstance(parsed, dict):
                            return {
                                "us": _coerce_news_section_text(parsed.get("us") or ""),
                                "jp": _coerce_news_section_text(parsed.get("jp") or ""),
                                "trends": _coerce_news_section_text(
                                    parsed.get("trends") or ""
                                ),
                            }
                except (json.JSONDecodeError, ValueError) as repair_exc:
                    app.logger.debug("News JSON brace-repair failed: %s", repair_exc)

                # If JSON is malformed/truncated, salvage per-section quoted strings.
                values = {}
                for key in ("us", "jp", "trends"):
                    marker = f'"{key}"'
                    marker_idx = text.find(marker)
                    if marker_idx == -1:
                        values[key] = ""
                        continue

                    colon_idx = text.find(":", marker_idx + len(marker))
                    if colon_idx == -1:
                        values[key] = ""
                        continue

                    i = colon_idx + 1
                    while i < len(text) and text[i].isspace():
                        i += 1

                    if i >= len(text) or text[i] != '"':
                        values[key] = ""
                        continue

                    i += 1
                    buf = []
                    escaped = False
                    while i < len(text):
                        ch = text[i]
                        if escaped:
                            if ch == "n":
                                buf.append("\n")
                            elif ch == "t":
                                buf.append("\t")
                            elif ch == "r":
                                buf.append("\r")
                            else:
                                buf.append(ch)
                            escaped = False
                            i += 1
                            continue

                        if ch == "\\":
                            escaped = True
                            i += 1
                            continue

                        if ch == '"':
                            break

                        buf.append(ch)
                        i += 1

                    values[key] = "".join(buf).strip()

                if not any(values.values()):
                    return None

                return {
                    "us": _coerce_news_section_text(values.get("us") or ""),
                    "jp": _coerce_news_section_text(values.get("jp") or ""),
                    "trends": _coerce_news_section_text(values.get("trends") or ""),
                }

            combined_res = call_mistral_chat(
                api_key,
                [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": combined_prompt},
                ],
                1500,
                use_cache=False,
                response_format={"type": "json_object"},
                cache_key_override="news_summary_system_v1",
            )

            combined_text = extract_chat_content(combined_res)
            try:
                payload = json.loads(extract_json_payload(combined_text))
                return {
                    "us": _coerce_news_section_text_v2(payload.get("us") or ""),
                    "jp": _coerce_news_section_text_v2(payload.get("jp") or ""),
                    "trends": _coerce_news_section_text_v2(payload.get("trends") or ""),
                }
            except (json.JSONDecodeError, TypeError, ValueError) as parse_err:
                raw_for_log = (combined_text or "")[:NEWS_PARSE_LOG_SNIPPET_CHARS]
                app.logger.warning(
                    "News bundle parse error: %s raw_len=%d raw_head=%s",
                    parse_err,
                    len(combined_text or ""),
                    raw_for_log,
                )
                salvaged = _best_effort_parse_news_sections(combined_text)
                if salvaged and any(
                    str(salvaged.get(k) or "").strip() for k in ("us", "jp", "trends")
                ):
                    app.logger.info(
                        "News bundle salvaged via local parser id=%s",
                        getattr(g, "request_id", "-"),
                    )
                    return salvaged
                try:
                    repaired_payload, _ = repair_news_json_with_llm(
                        api_key, combined_text
                    )
                    app.logger.info(
                        "News bundle repaired via llm formatter id=%s",
                        getattr(g, "request_id", "-"),
                    )
                    return {
                        "us": _coerce_news_section_text(
                            repaired_payload.get("us") or ""
                        ),
                        "jp": _coerce_news_section_text(
                            repaired_payload.get("jp") or ""
                        ),
                        "trends": _coerce_news_section_text(
                            repaired_payload.get("trends") or ""
                        ),
                    }
                except Exception as repair_err:  # pylint: disable=broad-exception-caught
                    app.logger.warning("News bundle repair failed: %s", repair_err)
                    return {
                        "us": "解析エラー",
                        "jp": "解析エラー",
                        "trends": "解析エラー",
                    }

        news_bundle = _generate_news_bundle()

        if not isinstance(news_bundle, dict):
            news_bundle = {"us": "", "jp": "", "trends": ""}

        us_text = _normalize_mistral_news_lines(news_bundle.get("us") or "")
        jp_text = _normalize_mistral_news_lines(news_bundle.get("jp") or "")
        trends_text = _normalize_mistral_news_lines(news_bundle.get("trends") or "")

        # トレンドバッジの同期用
        raw_trending = []
        if merged_trends:
            raw_trending = merged_trends[:]

        now_iso = datetime.now(timezone.utc).isoformat()

        return jsonify(
            {
                "us": {
                    "content": us_text,
                    "timestamp": now_iso,
                    "status": retrieve_status.get("us", "unknown"),
                },
                "jp": {
                    "content": jp_text,
                    "timestamp": now_iso,
                    "status": retrieve_status.get("jp", "unknown"),
                },
                "trends": {
                    "content": trends_text,
                    "timestamp": now_iso,
                    "status": retrieve_status.get("trends", "unknown"),
                },
                "trending_raw": raw_trending,
                "retrieve_status": retrieve_status,
            }
        )
    except (requests.RequestException, ValueError, KeyError, RuntimeError) as exc:
        app.logger.error("News API error: %s", exc)
        return error_response(ErrorCode.INTERNAL_SERVER_ERROR, status_code=500)


@app.route("/api/analyze-v2", methods=["POST"])
def api_analyze_v2():
    """
    Phase 1 Pilot: Mistral Function Calling variant

    This endpoint demonstrates Function Calling integration with Mistral API.
    Benefits over v1:
    - Tool definitions ensure structured output from Mistral
    - No dependency on JSON regex extraction
    - Parallel tool calling support (future)
    - More robust error handling
    """
    if not _is_local_request(request):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    api_key = extract_api_key(request)
    langsearch_api_key = extract_langsearch_api_key(request)
    if not api_key:
        return error_response(ErrorCode.INVALID_API_KEY, status_code=401)

    data = _parse_json_request()
    if data is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )
    raw_symbol = data.get("symbol")
    fallback_name = normalize_symbol(raw_symbol)
    market = normalize_market(data.get("market"), default="us")
    symbol = normalize_symbol_for_market(raw_symbol, market)
    name = normalize_text(data.get("name"), default=(symbol or fallback_name))
    price = data.get("price")
    chart_data = data.get("chart_data", []) or []

    if not market:
        return error_response(ErrorCode.INVALID_MARKET)
    if not symbol:
        return error_response(
            ErrorCode.MISSING_REQUIRED_FIELD, details={"fields": ["symbol"]}
        )

    app.logger.info(
        "api_analyze_v2 input id=%s market=%s symbol=%s has_price=%s chart_points=%d",
        getattr(g, "request_id", "-"),
        market,
        symbol,
        price is not None,
        len(chart_data or []),
    )

    try:
        # Fetch missing data
        if not chart_data or price is None:
            fetched = fetch_stock(symbol, name, market)
            if fetched:
                chart_data = chart_data or fetched.get("chart_data", [])
                if price is None:
                    price = fetched.get("price")

        # Gather research context
        research_context = get_cached_context_with_negative_cache(
            f"research_context_{symbol}_{market}_fc",
            lambda: collect_symbol_research_context(
                symbol, name, market, langsearch_api_key=langsearch_api_key
            ),
            600,
            120,
            True,
        )
        if len(research_context) > ANALYZE_RESEARCH_CONTEXT_MAX_CHARS:
            research_context = research_context[:ANALYZE_RESEARCH_CONTEXT_MAX_CHARS]

        info = get_stock_info_cached(symbol)
        sector = info.get("sector") or data.get("sector") or ""
        industry = info.get("industry") or data.get("industry") or ""
        market_cap = (
            info.get("marketCap")
            if info.get("marketCap") is not None
            else data.get("market_cap")
        )
        pe_ratio = (
            info.get("trailingPE")
            if info.get("trailingPE") is not None
            else data.get("pe_ratio")
        )
        price_trend = " → ".join([str(d.get("price")) for d in chart_data[-6:]])

        # Define analysis tools for Function Calling
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "generate_analysis_json",
                    "description": "Generate structured stock analysis in JSON format with recommendation and metrics",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "recommendation": {
                                "type": "string",
                                "enum": [
                                    "強い買い",
                                    "買い",
                                    "中立",
                                    "売り",
                                    "強い売り",
                                ],
                                "description": "Investment recommendation",
                            },
                            "sentiment": {
                                "type": "string",
                                "enum": ["強気", "中立", "弱気"],
                                "description": "Market sentiment",
                            },
                            "target_price_3m": {
                                "type": "number",
                                "description": "3-month target price",
                            },
                            "upside_3m": {
                                "type": "string",
                                "description": "3-month upside percentage, e.g. '+10%'",
                            },
                            "confidence": {
                                "type": "string",
                                "enum": ["高", "中", "低"],
                                "description": "Analysis confidence level",
                            },
                            "analysis_summary": {
                                "type": "string",
                                "description": "100-character summary of analysis",
                            },
                            "key_catalysts": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Key catalysts (up to 3 items)",
                            },
                            "risk_factors": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Risk factors (up to 2 items)",
                            },
                            "technical_analysis": {
                                "type": "string",
                                "description": "Technical analysis summary (50 chars max)",
                            },
                            "fundamental_analysis": {
                                "type": "string",
                                "description": "Fundamental analysis summary (50 chars max)",
                            },
                            "latest_news_impact": {
                                "type": "string",
                                "description": "Impact of latest news (90 chars max)",
                            },
                        },
                        "required": [
                            "recommendation",
                            "sentiment",
                            "target_price_3m",
                            "upside_3m",
                            "confidence",
                            "analysis_summary",
                            "key_catalysts",
                            "risk_factors",
                            "technical_analysis",
                            "fundamental_analysis",
                            "latest_news_impact",
                        ],
                    },
                },
            }
        ]

        # System and user prompts
        system_prompt = (
            "あなたは株式分析の専門家です。提供されたツールを使用して、"
            "厳密な分析結果をJSON形式で返してください。"
            "数値データは入力された通貨単位を維持し、断定できない情報は保守的に扱ってください。"
        )

        user_prompt = (
            f"以下の銘柄を分析してください。\n"
            f"【銘柄情報】\n"
            f"- シンボル: {symbol}\n"
            f"- 企業名: {name}\n"
            f"- 現在価格: {price}\n"
            f"- 業種: {industry or 'N/A'}\n"
            f"- セクター: {sector or 'N/A'}\n"
            f"- 時価総額: {market_cap or 'N/A'}\n"
            f"- PER: {pe_ratio or 'N/A'}\n"
            f"- 直近価格推移: {price_trend}\n"
            f"【外部調査コンテキスト】\n{research_context}\n"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # Call Mistral with Function Calling and Retry Logic
        # Retry up to 2 times if function calling doesn't yield a valid tool_call
        response = None
        for fc_attempt in range(2):
            response = call_mistral_chat(
                api_key,
                messages=messages,
                max_tokens=600,  # Increased slightly for safety
                use_cache=False,
                response_format=None,
                tools=tools,
                tool_choice=(
                    {"type": "function", "function": {"name": "generate_analysis_json"}}
                    if fc_attempt == 0
                    else "auto"
                ),  # Try auto on second attempt
                cache_key_override=f"analyze_system_v1_{symbol}",
            )
            if isinstance(response, dict) and response.get("choices"):
                msg = response["choices"][0].get("message", {})
                if msg.get("tool_calls"):
                    break  # Success
            app.logger.info(
                "Analyze-v2 function call missing tool_calls (attempt %d)",
                fc_attempt + 1,
            )

        # Extract tool call result
        result = None
        if isinstance(response, dict) and response.get("choices"):
            msg = response["choices"][0].get("message", {})
            if msg.get("tool_calls"):
                for tool_call in msg["tool_calls"]:
                    if (
                        tool_call.get("function", {}).get("name")
                        == "generate_analysis_json"
                    ):
                        try:
                            # Parse tool arguments
                            args_json = tool_call["function"]["arguments"]
                            if isinstance(args_json, str):
                                result = json.loads(args_json)
                            else:
                                result = args_json
                            app.logger.info(
                                "Analyze-v2 tool call succeeded for %s", symbol
                            )
                        except (json.JSONDecodeError, TypeError, ValueError) as e:
                            app.logger.warning(
                                "Analyze-v2 tool argument parsing failed: %s", e
                            )

        # Fallback: If no tool call or parsing fails, fall back to v1 logic
        if not result:
            app.logger.info("Analyze-v2 no tool call, falling back to v1 logic")

            # Try extracting direct JSON from response content
            content = extract_chat_content(response)
            try:
                json_str = extract_json_payload(content)
                result = json.loads(json_str)
            except (json.JSONDecodeError, ValueError, TypeError) as parse_exc:
                app.logger.debug("Analyze-v2 JSON extraction failed: %s", parse_exc)
                # Further fallback to repair
                try:
                    repaired_result, _ = repair_analysis_json_with_llm(api_key, content)
                    result = repaired_result
                except Exception as e:  # pylint: disable=broad-exception-caught
                    app.logger.warning("Analyze-v2 complete fallback failed: %s", e)
                    result = None

        if result:
            # Validate the result against a minimal schema. If validation fails, try LLM-based repair.
            valid, reason = validate_analysis_result(result)
            if not valid:
                app.logger.info(
                    "Analyze-v2 result validation failed (%s); attempting LLM repair",
                    reason,
                )
                try:
                    repaired_result, repaired_content = repair_analysis_json_with_llm(
                        api_key, json.dumps(result)
                    )
                    result = repaired_result
                except Exception as e:  # pylint: disable=broad-exception-caught
                    app.logger.warning("Analyze-v2 repair attempt failed: %s", e)
                    result = None

        if result:
            result = normalize_analysis_result(result)
            result["search_used"] = bool(research_context.strip())
            result["analyzed_at"] = datetime.now(timezone.utc).isoformat()
            result["version"] = "v2-function-calling"
            result["tool_used"] = True

            # Store in chat history (LRU/limitロジックをv1と統一)
            chat_key = f"{market}:{symbol}"
            with app_state.chat_history_lock:
                if chat_key in app_state.chat_history:
                    app_state.chat_history[chat_key] = app_state.chat_history.pop(
                        chat_key
                    )
                else:
                    app_state.chat_history[chat_key] = [
                        {
                            "role": "system",
                            "content": f"あなたは{symbol}銘柄の専門家です。簡潔かつ投資家に有益な回答をしてください。",
                        }
                    ]

                if len(app_state.chat_history) > 50:
                    oldest_key = next(iter(app_state.chat_history))
                    app_state.chat_history.pop(oldest_key, None)

                app_state.chat_history[chat_key].append(
                    {
                        "role": "assistant",
                        "content": f"分析サマリー（v2）: {result.get('analysis_summary')}",
                    }
                )

                if len(app_state.chat_history[chat_key]) > 11:
                    app_state.chat_history[chat_key] = [
                        app_state.chat_history[chat_key][0]
                    ] + app_state.chat_history[chat_key][-10:]
            return jsonify(result)
        # Complete failure
        fallback_result = build_fallback_analysis_result("Function calling failed")
        fallback_result["version"] = "v2-fallback"
        return jsonify(fallback_result), 200
    except Exception as e:  # pylint: disable=broad-exception-caught
        app.logger.error("Analyze-v2 unexpected error: %s", e)
        return error_response(ErrorCode.INTERNAL_SERVER_ERROR, status_code=500)


# #region Health & System Utility
@app.route("/api/health", methods=["GET", "OPTIONS"])
def api_health():
    """ヘルスチェックエンドポイント"""
    with app_state.yfinance_lock:
        yf_limited = app_state.is_yfinance_rate_limited and (
            time.time() < app_state.yfinance_rate_limit_until
        )
        yf_until = (
            datetime.fromtimestamp(app_state.yfinance_rate_limit_until).isoformat()
            if app_state.is_yfinance_rate_limited
            else None
        )

    return jsonify(
        {
            "ok": True,
            "app": "Mistral NeX Stocks",
            "model": get_model_name(),
            "badge": get_model_badge(),
            "is_yfinance_rate_limited": yf_limited,
            "yfinance_rate_limit_until": yf_until,
            "extension_manifest_ok": app_state._extension_manifest_status.get("ok", True),
            "extension_manifest_error": app_state._extension_manifest_status.get("error", ""),
            **get_api_credential_state(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


@app.route("/api/csp-report", methods=["POST"])
def api_csp_report():
    """CSP report receiver for Report-Only mode (accepts JSON POST)."""
    try:
        payload = request.get_json(force=True, silent=True) or {}
        # Log up to 2KB of the report to avoid leaking large payloads
        app.logger.warning("CSP report received: %s", json.dumps(payload, ensure_ascii=False)[:2000])
    except Exception as exc:
        app.logger.debug("Failed to parse CSP report: %s", exc)
    # Return 204 No Content as recommended for CSP reports
    return ('', 204)


def _is_loopback_host(host: str) -> bool:
    """ホストがループバックアドレスか判定"""
    if not host:
        return False
    host = host.strip()
    if host.lower() == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_loopback
    except ValueError:
        return False


def _is_local_request(req):
    """Check if the request originates from localhost."""
    remote = req.remote_addr or ""
    # Trust loopback only - strict validation
    if remote not in ("127.0.0.1", "localhost", "::1"):
        return False
    # Do NOT trust X-Forwarded-For from untrusted proxies
    # In production behind a trusted reverse proxy, this check should be modified
    forwarded = req.headers.get("X-Forwarded-For", "")
    if forwarded:
        # If X-Forwarded-For exists but remote is localhost, it may indicate spoofing
        # Only allow if all forwarded IPs are also localhost
        forwarded_ips = [x.strip() for x in forwarded.split(",")]
        for ip in forwarded_ips:
            if ip and ip not in ("127.0.0.1", "localhost", "::1"):
                app.logger.debug(
                    "X-Forwarded-For non-local header detected from %s: %s",
                    remote,
                    forwarded,
                )
                return False
    return True


def _is_allowed_shutdown_origin(req):
    """シャットダウン要求の送信元オリジンが許可されているか判定"""
    allowed_origins = get_allowed_cors_origins()
    # 末尾のスラッシュを削除して正規化
    normalized_origins = {o.rstrip("/") for o in allowed_origins}

    origin = (req.headers.get("Origin") or "").strip().rstrip("/")
    if origin:
        return origin in normalized_origins

    referer = (req.headers.get("Referer") or "").strip()
    if referer:
        parsed = urlparse(referer)
        ref_origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        return ref_origin in normalized_origins

    # Origin/Referer がないリクエストは許可しない
    return False


@app.route("/api/shutdown", methods=["POST", "OPTIONS"])
def api_shutdown():
    """シャットダウンエンドポイント"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    if not _is_local_request(request):
        app.logger.warning(
            "Shutdown request rejected from non-local address: %s", request.remote_addr
        )
        return jsonify({"ok": False, "error": "forbidden"}), 403

    if not _is_allowed_shutdown_origin(request):
        app.logger.warning("Shutdown request rejected from untrusted origin")
        return jsonify({"ok": False, "error": "untrusted origin"}), 403

    # CSRF トークン検証
    data = _parse_json_request()
    if data is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )

    token_header = request.headers.get("X-MNS-Shutdown-Token")
    token_json = data.get("shutdown_token")
    provided_token = token_header or token_json
    expected_token = app.config.get("SHUTDOWN_TOKEN")

    if expected_token is not None:
        if not provided_token or not secrets.compare_digest(expected_token, provided_token):
            app.logger.warning("Shutdown request rejected: invalid or missing shutdown token")
            return jsonify({"ok": False, "error": "invalid or missing shutdown token"}), 403

    if data.get("confirm") is not True:
        return jsonify({"ok": False, "error": "confirm flag required"}), 400

    def shutdown_server():
        app.logger.info("Shutdown thread started")
        time.sleep(1.0)

        try:
            app_state.shutdown_executors()
        except (RuntimeError, AttributeError, ValueError) as exc:
            app.logger.warning("Executor shutdown before process exit failed: %s", exc)

        # 終了前にトークンファイルを削除
        try:
            token_file = Path(__file__).resolve().parent / ".mns_shutdown_token"
            token_file.unlink(missing_ok=True)
            app.logger.info("Shutdown token file removed successfully")
        except (IOError, OSError) as exc:
            app.logger.warning("Failed to remove shutdown token file during shutdown: %s", exc)

        # 終了前にPIDファイルを削除
        try:
            app.logger.info("Removing PID file")
            base_dir = Path(__file__).resolve().parent
            pid_file = base_dir / ".backend.pid"
            if pid_file.exists():
                removed = False
                for _ in range(2):
                    try:
                        pid_file.unlink()
                    except (IOError, OSError):
                        time.sleep(0.1)
                    if not pid_file.exists():
                        removed = True
                        break
                if not removed:
                    app.logger.warning(
                        "PID file still exists after retry attempts: %s", pid_file
                    )
                else:
                    app.logger.info("PID file removed successfully")
        except (IOError, OSError) as exc:
            app.logger.warning("Failed to remove pid file during shutdown: %s", exc)

        try:
            app.logger.info("Shutting down logging")
            logging.shutdown()
        except Exception:  # pylint: disable=broad-exception-caught
            pass

        # PIDファイルを使用してプロセスを終了
        try:
            import psutil

            current_pid = os.getpid()
            app.logger.info("Current PID: %s", current_pid)

            # 自分自身のプロセスを終了
            parent = psutil.Process(current_pid)
            parent.terminate()

            # タイムアウト後に強制終了
            def force_kill():
                try:
                    time.sleep(2.0)
                    if parent.is_running():
                        app.logger.warning("Process still running, forcing kill")
                        parent.kill()
                except psutil.NoSuchProcess:
                    pass

            threading.Thread(target=force_kill, daemon=True).start()
        except ImportError:
            app.logger.warning("psutil not available, using os._exit")
            os._exit(0)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            app.logger.error("Failed to terminate process: %s", exc)
            os._exit(0)

    # デーモンスレッドとして設定
    shutdown_thread = threading.Thread(target=shutdown_server)
    shutdown_thread.daemon = True
    shutdown_thread.start()
    return jsonify({"ok": True, "message": "Shutting down..."})


# #endregion Health & System Utility

# #region Market Hours Logic


# #region Real-Time SSE Engine
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


def is_market_open(market_type):
    """市場が現在開いているかを判定。Yahoo Financeのステータスを優先し、フォールバックとして時間ベースの判定を行う。"""
    with app_state.market_status_lock:
        status = app_state.market_status_cache.get(market_type)
    if status == "REGULAR":
        return True
    if status and status != "REGULAR":
        return False

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
        except Exception as exc:  # pylint: disable=broad-exception-caught
            app.logger.warning(
                "ZoneInfo resolution for Ny-Time failed: %s. Using manual fallback.",
                exc,
            )
            year = now_utc.year
            # Fallback DST logic
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


class MessageAnnouncer:
    """SSE配信用のリスナー管理クラス"""

    def __init__(self):
        """リスナーリストとロックを初期化"""
        self.listeners = []
        self.lock = threading.Lock()

    def listen(self):
        """SSEリスナー用キューを登録して返す"""
        q = queue.Queue(maxsize=5)
        with self.lock:
            self.listeners.append(q)
        return q

    def unlisten(self, q):
        """SSEリスナーのキューを登録解除"""
        with self.lock:
            try:
                self.listeners.remove(q)
            except ValueError:
                pass

    def announce(self, msg):
        """全リスナーにメッセージを配信"""
        with self.lock:
            for i in reversed(range(len(self.listeners))):
                try:
                    self.listeners[i].put_nowait(msg)
                except queue.Full:
                    # Keep listener alive by dropping one stale message and enqueueing latest.
                    # This avoids silent client desynchronization under temporary UI lag.
                    try:
                        self.listeners[i].get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self.listeners[i].put_nowait(msg)
                    except queue.Full:
                        app.logger.warning(
                            "SSE queue overflow persists: dropping latest message for one listener"
                        )

    def listener_count(self):
        """現在のリスナー数を返す"""
        with self.lock:
            return len(self.listeners)


app_state.sse_announcer = MessageAnnouncer()


@app.route("/api/stocks/stream")
def api_stocks_stream():
    """SSEストリームエンドポイント"""
    request_id = getattr(g, "request_id", "-")

    @stream_with_context
    def stream():
        q = app_state.sse_announcer.listen()
        try:
            # 初回接続時に即座に現在のキャッシュ状態を送信する
            with app_state.sse_data_lock:
                initial_payload = json.dumps(
                    {
                        "stream_event": "initial_snapshot",
                        "stocks": app_state.current_stocks_cache,
                        "indices": app_state.current_indices_cache,
                    }
                )
            yield f"data: {initial_payload}\n\n"

            # 30秒ハートビート（クライアント側でタイムアウト検出用）
            heartbeat_interval = 30

            while True:
                try:
                    # タイムアウトを30秒に設定し、その間隔でハートビート送信
                    msg = q.get(timeout=heartbeat_interval)
                    yield msg
                except queue.Empty:
                    # 30秒間何もデータが来なかった場合、ハートビート送信
                    heartbeat_data = json.dumps(
                        {"type": "heartbeat", "timestamp": time.time()}
                    )
                    yield f"event: heartbeat\ndata: {heartbeat_data}\n\n"
        except GeneratorExit:
            # クライアントが接続を切った
            app.logger.info("SSE client disconnected id=%s", request_id)
        finally:
            app_state.sse_announcer.unlisten(q)

    response = Response(stream(), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response


def interpolate_value(
    current,
    target,
    is_price=True,
    is_open=True,
    stock_market_state=None,  # pylint: disable=unused-argument
):
    """現在値と目標値の間を補間"""
    if current is None and target is None:
        return None

    # 文字列（"--"など）からの遷移をハンドリング
    try:
        curr_float = float(current) if current is not None else None
    except (ValueError, TypeError):
        curr_float = None

    try:
        target_float = float(target) if target is not None else None
    except (ValueError, TypeError):
        target_float = None

    if curr_float is None:
        return target_float if target_float is not None else target
    if target is None:
        return curr_float
    if target_float is None:
        return curr_float

    if stock_market_state and stock_market_state != "REGULAR":
        return target_float

    diff = target_float - curr_float
    if abs(diff) < 1e-6:
        return target_float

    # パターンB: 滑らかな Ease-out 補間
    step = diff * 0.25  # 追従速度を少し上げる
    min_step = 0.02 if is_price else 0.01

    if abs(step) < min_step:
        step = diff if abs(diff) < min_step else (min_step if diff > 0 else -min_step)

    return curr_float + step


def _round_if_numeric(value, digits=2):
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return value


def clone_structure_for_current(target_list, current_list, market="us", is_open=None):
    """
    ターゲット（目標値）から現在値を補間して新しいリストを作成
    chart_data/ohlc_data は参照共有を避けるため明示的にコピー
    """
    if is_open is None:
        is_open = is_market_open(market)
    current_map = {
        item.get("symbol"): item
        for item in current_list
        if isinstance(item, dict) and "symbol" in item
    }
    new_current = []

    for t_item in target_list:
        if not t_item or not isinstance(t_item, dict):
            continue

        sym = t_item.get("symbol")
        if sym in current_map:
            # 基本的に shallow copy だが、リストは明示的にコピー
            c_item = current_map[sym].copy()

            # スカラー値を更新
            c_item["name"] = t_item.get("name", c_item.get("name"))
            c_item["market"] = t_item.get("market", c_item.get("market"))
            c_item["currency"] = t_item.get("currency", c_item.get("currency"))
            c_item["market_state"] = t_item.get(
                "market_state", c_item.get("market_state")
            )
            c_item["shares"] = t_item.get("shares", c_item.get("shares"))
            c_item["avg_price"] = t_item.get("avg_price", c_item.get("avg_price"))
            c_item["portfolio_value"] = t_item.get(
                "portfolio_value", c_item.get("portfolio_value")
            )
            c_item["portfolio_pl"] = t_item.get(
                "portfolio_pl", c_item.get("portfolio_pl")
            )
            c_item["sector"] = t_item.get("sector", c_item.get("sector"))
            c_item["industry"] = t_item.get("industry", c_item.get("industry"))
            c_item["high"] = t_item.get("high", c_item.get("high"))
            c_item["low"] = t_item.get("low", c_item.get("low"))
            c_item["volume"] = t_item.get("volume", c_item.get("volume"))
            c_item["snapshot_ts_ms"] = t_item.get(
                "snapshot_ts_ms", c_item.get("snapshot_ts_ms")
            )

            # リスト（chart_data/ohlc_data）は参照共有を避けるためコピー
            if "chart_data" in t_item and isinstance(t_item["chart_data"], list):
                c_item["chart_data"] = [
                    d.copy() if isinstance(d, dict) else d for d in t_item["chart_data"]
                ]

            if "ohlc_data" in t_item and isinstance(t_item["ohlc_data"], list):
                c_item["ohlc_data"] = [
                    d.copy() if isinstance(d, dict) else d for d in t_item["ohlc_data"]
                ]
        else:
            # 新規銘柄の場合は shallow copy
            # リスト（chart_data/ohlc_data）も複製
            c_item = t_item.copy()
            if "chart_data" in t_item and isinstance(t_item["chart_data"], list):
                c_item["chart_data"] = [
                    d.copy() if isinstance(d, dict) else d for d in t_item["chart_data"]
                ]
            if "ohlc_data" in t_item and isinstance(t_item["ohlc_data"], list):
                c_item["ohlc_data"] = [
                    d.copy() if isinstance(d, dict) else d for d in t_item["ohlc_data"]
                ]

        if c_item.get("price") is not None and t_item.get("price") is not None:
            c_item["price"] = _round_if_numeric(
                interpolate_value(
                    c_item["price"],
                    t_item["price"],
                    is_open=is_open,
                    stock_market_state=t_item.get("market_state"),
                )
            )
        if c_item.get("change") is not None and t_item.get("change") is not None:
            c_item["change"] = _round_if_numeric(
                interpolate_value(
                    c_item["change"],
                    t_item["change"],
                    is_open=is_open,
                    stock_market_state=t_item.get("market_state"),
                )
            )
        if (
            c_item.get("change_percent") is not None
            and t_item.get("change_percent") is not None
        ):
            c_item["change_percent"] = _round_if_numeric(
                interpolate_value(
                    c_item["change_percent"],
                    t_item["change_percent"],
                    is_price=False,
                    is_open=is_open,
                    stock_market_state=t_item.get("market_state"),
                )
            )

        new_current.append(c_item)
    return new_current


def _build_sse_light_stocks_payload(stocks_by_market):
    """SSE配信用の軽量株価ペイロードを構築"""
    fields = (
        "symbol",
        "name",
        "market",
        "price",
        "change",
        "change_percent",
        "high",
        "low",
        "volume",
        "currency",
        "market_state",
        "shares",
        "avg_price",
        "avg_fx_rate",
        "portfolio_value",
        "portfolio_pl",
        "sector",
        "industry",
    )
    payload = {"us": [], "jp": [], "idx": []}
    for market in ("us", "jp", "idx"):
        rows = (
            stocks_by_market.get(market, [])
            if isinstance(stocks_by_market, dict)
            else []
        )
        out = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            row = {k: item.get(k) for k in fields if k in item}

            row["snapshot_ts_ms"] = item.get("snapshot_ts_ms")

            chart_rows = (
                item.get("chart_data")
                if isinstance(item.get("chart_data"), list)
                else []
            )
            if chart_rows:
                compact_chart = []
                for p in chart_rows[-24:]:
                    if not isinstance(p, dict):
                        continue
                    price = p.get("price")
                    if price is None:
                        continue
                    compact_chart.append(
                        {
                            "x": p.get("x"),
                            "price": price,
                        }
                    )
                if compact_chart:
                    row["chart_data"] = compact_chart

            out.append(row)
        payload[market] = out
    return payload


def bg_interpolate_loop():
    """継続的に全銘柄の現在値を補間してSSE配信（市場キャッシュ付き）"""
    last_market_check = time.time()
    market_check_interval = 60  # 1分ごとに市場状態を更新
    # 起動時に市場状態を即時取得（最初の60秒間が常にFalseになるのを防ぐ）
    us_market_open = is_market_open("us")
    jp_market_open = is_market_open("jp")
    idx_market_open = is_market_open("idx")

    while True:
        try:
            current_time = time.time()

            # リスナーがいない場合は計算そのものをスキップして待機
            # 誰も見ていない時のサーバー負荷（クローンと計算）を最小化する
            listener_count = app_state.sse_announcer.listener_count()
            if listener_count == 0:
                time.sleep(10.0)
                continue

            # 市場状態キャッシュを1分ごとに更新
            if current_time - last_market_check > market_check_interval:
                try:
                    us_market_open = is_market_open("us")
                    jp_market_open = is_market_open("jp")
                    idx_market_open = is_market_open("idx")
                    last_market_check = current_time
                except Exception as market_check_exc:  # pylint: disable=broad-exception-caught
                    app.logger.debug("Market status check error: %s", market_check_exc)
                    # 前回の値を保持

            with app_state.sse_data_lock:
                target_us = list(app_state.target_stocks_cache.get("us", []))
                target_jp = list(app_state.target_stocks_cache.get("jp", []))
                target_idx = list(app_state.target_stocks_cache.get("idx", []))
                current_us = list(app_state.current_stocks_cache.get("us", []))
                current_jp = list(app_state.current_stocks_cache.get("jp", []))
                current_idx = list(app_state.current_stocks_cache.get("idx", []))

            new_current_stocks = {
                "us": clone_structure_for_current(
                    target_us, current_us, market="us", is_open=us_market_open
                ),
                "jp": clone_structure_for_current(
                    target_jp, current_jp, market="jp", is_open=jp_market_open
                ),
                "idx": clone_structure_for_current(
                    target_idx, current_idx, market="idx", is_open=idx_market_open
                ),
            }

            with app_state.sse_data_lock:
                app_state.current_stocks_cache = new_current_stocks
                app_state.current_indices_cache = app_state.target_indices_cache
                indices_copy = copy.copy(app_state.current_indices_cache)

            # ロックの外側で重い処理を行う
            light_stocks = _build_sse_light_stocks_payload(new_current_stocks)
            payload = json.dumps(
                {"stocks": light_stocks, "indices": indices_copy}
            )
            app_state.sse_announcer.announce(f"data: {payload}\n\n")

            # 市場が閉場時は補間間隔を長くする（10秒）
            if not us_market_open and not jp_market_open:
                time.sleep(10.0)  # 閉場時: 10秒間隔
            else:
                time.sleep(1.0)  # 開場時: 1秒間隔
        except (RuntimeError, ValueError, KeyError) as e:
            app.logger.error("bg_interpolate_loop: %s", e)
            # Prevent a hot error loop from consuming CPU when repeated exceptions occur.
            time.sleep(1.0)


def _run_scheduled_sync_job():
    """スケジュールされた同期ジョブを実行"""
    try:
        sync_all_stocks_now()
    finally:
        with app_state.sync_schedule_lock:
            app_state.sync_scheduled = False


def schedule_sync_all_stocks_now():
    """同期ジョブをスケジュール"""
    with app_state.is_syncing_lock:
        if app_state.is_syncing:
            return False

    with app_state.sync_schedule_lock:
        if app_state.sync_scheduled:
            return False
        app_state.sync_scheduled = True

    try:
        app_state.sync_refresh_executor.submit(_run_scheduled_sync_job)
        return True
    except (RuntimeError, AttributeError, ValueError) as exc:
        with app_state.sync_schedule_lock:
            app_state.sync_scheduled = False
        app.logger.warning("Failed to schedule stock sync: %s", exc)
        return False


def sync_all_stocks_now():
    """Yahoo Financeから全銘柄を一括同期し、ターゲットキャッシュを更新する"""
    with app_state.is_syncing_lock:
        if app_state.is_syncing:
            app.logger.info("Sync already in progress, skipping.")
            return
        app_state.is_syncing = True

    try:
        # 取得不能時に画面から値が完全に消えるのを防ぐため、キャッシュをクリアせず、マージ更新する
        # with app_state.market_status_lock:
        #     app_state.market_status_cache = {"us": None, "jp": None, "idx": None}

        # メインの株価一括取得（yf.downloadベース）へ統合するため、
        # ヘッダー用の個別の取得処理は抑制し、後続の fetched_items から抽出する
        app_state.current_indices_cache = app_state.current_indices_cache or {}

        # 市場ステータスの更新（代表指数から）
        # 市場ステータスの更新は後続の new_header_data 構築後に行います

        # Fetch Stocks
        load_user_stocks(force=True)
        items = []
        with app_state.user_stocks_lock:
            user_us_snapshot = dict(app_state.user_us)
            user_jp_snapshot = dict(app_state.user_jp)
            user_idx_snapshot = dict(app_state.user_idx)

        # User stocks take priority
        user_us_set = set(user_us_snapshot.keys())
        user_jp_set = set(user_jp_snapshot.keys())
        user_idx_set = set(user_idx_snapshot.keys())

        # Add User Stocks
        for s, n in user_us_snapshot.items():
            items.append((s, n, "us"))
        for s, n in user_jp_snapshot.items():
            items.append((s, n, "jp"))
        for s, n in user_idx_snapshot.items():
            items.append((s, n, "idx"))

        # Add Default Stocks only if not already in user list
        for s, n in DEFAULT_US.items():
            if s not in user_us_set:
                items.append((s, n, "us"))
        for s, n in DEFAULT_JP.items():
            if s not in user_jp_set:
                items.append((s, n, "jp"))
        for s, n in DEFAULT_IDX.items():
            if s not in user_idx_set:
                items.append((s, n, "idx"))

        snapshot_ts_ms = int(time.time() * 1000)
        fetched_items = fetch_stocks_batch(items, snapshot_ts_ms=snapshot_ts_ms)
        us_res, jp_res, idx_res = [], [], []
        for item in fetched_items:
            if not item:
                continue
            m = item.get("market")
            if m == "us":
                us_res.append(item)
            elif m == "jp":
                jp_res.append(item)
            else:
                idx_res.append(item)

        if items and not (us_res or jp_res or idx_res):
            app.logger.warning(
                "Stock sync produced no valid items; preserving previous target cache."
            )
            return

        with app_state.sse_data_lock:
            app_state.target_stocks_cache = {"us": us_res, "jp": jp_res, "idx": idx_res}
            # 初回表示の遅延を避けるため、current が空なら target を即時反映する
            current_empty = not any(
                app_state.current_stocks_cache.get(m) for m in ("us", "jp", "idx")
            )
            if current_empty:
                app_state.current_stocks_cache = copy.deepcopy(
                    app_state.target_stocks_cache
                )
            # メインの取得結果からヘッダー用インデックスを更新
            header_mapping = {
                "^N225": "N225",
                "^DJI": "DJI",
                "USDJPY=X": "USDJPY",
                "JPY=X": "USDJPY",
                "EURJPY=X": "EURJPY",
                "^IXIC": "NASDAQ",
                "^GSPC": "SP500",
                "^VIX": "VIX",
            }
            new_header_data = {}
            for item in idx_res + us_res + jp_res:
                if not item:
                    continue
                sym = item.get("symbol")
                if sym in header_mapping:
                    h_key = header_mapping[sym]
                    new_header_data[h_key] = {
                        "price": item.get("price"),
                        "change": item.get("change"),
                        "percent": item.get("change_percent") or item.get("percent"),
                        "open": item.get("open"),
                        "high": item.get("high"),
                        "low": item.get("low"),
                        "volume": item.get("volume"),
                        "market_state": item.get("market_state", "UNKNOWN"),
                        "market": item.get("market"),
                    }

            # --- 指標セーフティネット ---
            # 主要指標がバッチ結果に含まれなかった場合のみ、個別にリトライして確実に更新する
            critical_indices = {
                "N225": "^N225",
                "DJI": "^DJI",
                "USDJPY": "USDJPY=X",
                "EURJPY": "EURJPY=X",
                "VIX": "^VIX",
                "NASDAQ": "^IXIC",
                "SP500": "^GSPC",
            }
            for key, sym in critical_indices.items():
                if (
                    key not in new_header_data
                    or new_header_data[key].get("price") == "--"
                ):
                    try:
                        app.logger.debug(
                            "Safety net trigger: fetching %s (%s) individually",
                            key,
                            sym,
                        )
                        res = fetch_index_data(
                            key, sym
                        )  # ロバストな個別取得関数を再利用
                        if res and res[1]:
                            new_header_data[key] = res[1]
                    except Exception as safety_exc:  # pylint: disable=broad-exception-caught
                        app.logger.warning(
                            "Safety net failed for %s: %s", key, safety_exc
                        )

            if new_header_data:
                # 取得できた指標だけを上書き（失敗しても以前のキャッシュを消さない）
                app_state.current_indices_cache.update(new_header_data)
                # 市場ステータスの同期
                with app_state.market_status_lock:
                    if "N225" in new_header_data:
                        app_state.market_status_cache["jp"] = new_header_data[
                            "N225"
                        ].get("market_state")
                    if "SP500" in new_header_data:
                        st = new_header_data["SP500"].get("market_state")
                        app_state.market_status_cache["us"] = st
                        app_state.market_status_cache["idx"] = st
        app.logger.info("Sync completed.")
    except (requests.RequestException, ValueError, KeyError, RuntimeError) as e:
        app.logger.error("sync_all_stocks_now: %s", e)
    finally:
        with app_state.is_syncing_lock:
            app_state.is_syncing = False


def bg_yahoo_fetch_loop():
    """Yahoo Financeデータの定期取得ループ"""
    time.sleep(0.5)
    consecutive_errors = 0
    max_consecutive_errors = 10

    while True:
        try:
            sync_all_stocks_now()
            consecutive_errors = 0
        except Exception as e:  # pylint: disable=broad-exception-caught
            consecutive_errors += 1
            app.logger.error(
                "sync_all_stocks_now failed (%d/%d): %s",
                consecutive_errors,
                max_consecutive_errors,
                e,
            )
            if consecutive_errors >= max_consecutive_errors:
                app.logger.critical("Too many consecutive errors, stopping fetch loop")
                break

        try:
            # 市場閉場時は更新間隔を長くする（5分）
            if not is_market_open("us") and not is_market_open("jp"):
                time.sleep(300.0)  # 閉場時: 5分間隔
            else:
                time.sleep(45.0)  # 開場時: 45秒間隔
        except Exception as e:  # pylint: disable=broad-exception-caught
            app.logger.error("Error in market check: %s", e)
            time.sleep(60.0)  # エラー時は安全側に倒して待機


# ------------------------------
# 銘柄追加・削除時に同期をキックするように既存ルートを修正
# ------------------------------
# (メモ: 既存の api_add_stock, api_delete_stock 等の関数内で sync_all_stocks_now() を呼ぶように修正)


# ------------------------------
# Run
# ------------------------------
def _start_background_threads():
    """バックグラウンドスレッドを安全に開始（クラッシュ時に再起動）"""

    def wrapped_loop(func, name):
        consecutive_errors = 0
        max_consecutive_errors = 10
        while True:
            try:
                func()
                consecutive_errors = 0
            except Exception as e:  # pylint: disable=broad-exception-caught
                consecutive_errors += 1
                app.logger.error(
                    "%s thread crashed (%d/%d): %s",
                    name,
                    consecutive_errors,
                    max_consecutive_errors,
                    e,
                )
                if consecutive_errors >= max_consecutive_errors:
                    app.logger.critical("%s thread stopped after too many errors", name)
                    break
                time.sleep(min(2**consecutive_errors, 60))  # 指数バックオフ

    threading.Thread(
        target=wrapped_loop, args=(bg_yahoo_fetch_loop, "Yahoo"), daemon=True
    ).start()
    threading.Thread(
        target=wrapped_loop, args=(bg_interpolate_loop, "Interpolate"), daemon=True
    ).start()


if __name__ == "__main__":
    # シャットダウントークンの生成
    import secrets
    shutdown_token = secrets.token_hex(32)
    app.config["SHUTDOWN_TOKEN"] = shutdown_token
    token_file = Path(__file__).resolve().parent / ".mns_shutdown_token"
    try:
        token_file.write_text(shutdown_token, encoding="utf-8")
    except Exception as e:
        app.logger.error("Failed to write shutdown token file: %s", e)

    # スクリプト直接実行時のみ常駐スレッドを開始
    _start_background_threads()
    schedule_sync_all_stocks_now()
    schedule_news_warmup()
    app.run(debug=False, threaded=True, host="127.0.0.1", port=BACKEND_PORT)
