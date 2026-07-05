# app_state.py
"""Application state management, logging filters, and Pydantic schemas."""

import json
import logging
import os
import platform
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Set

from cachetools import LRUCache, TTLCache

from constants import MAX_SSE_LISTENERS
from mistral_compat import Mistral
from utils.threading import DaemonThreadPoolExecutor

logger = logging.getLogger("backend")


# #region yfinance Session Management

try:
    import keyring.errors as _keyring_errors
    KeyringError: type[Exception] = _keyring_errors.KeyringError
except ImportError:
    class _KeyringErrorFallback(Exception):
        """Fallback if keyring is not installed or keyring is unavailable."""

    KeyringError = _KeyringErrorFallback



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
    """yfinance用のセッションを管理し、ユーザーエージェントとブラウザフィンガープリントをローテーション"""

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
                    self._excluded_until = {}
                    self._all_sessions = []
                    self._local = threading.local()
                    self._ua_index = 0
                    self._session_epoch = 0
                    self._request_lock = threading.Lock()
                    self._last_request_ts = 0.0
                    self._initialized = True

    def get_user_agent(self):
        with self._lock:
            return YFINANCE_USER_AGENTS[self._ua_index]

    def _create_session(self, ua):
        """curl_cffiを使用してブラウザ（Chrome）の挙動を模倣するセッションを作成"""
        if CURL_CFFI_AVAILABLE:
            # impersonate='chrome' によりTLSフィンガープリントを偽装
            session: Any = curl_requests.Session(impersonate="chrome")
        else:
            import requests

            session = requests.Session()

        session.headers.update(
            {
                "User-Agent": ua,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Origin": "https://finance.yahoo.com",
                "Referer": "https://finance.yahoo.com",
            }
        )

        # Intercept requests to detect 401 (Invalid Crumb) and 429 (Rate Limit) responses
        original_request = session.request

        def custom_request(*args, **kwargs):
            # Enforce global spacing and serialization across all threads and sessions
            with self._request_lock:
                now = time.time()
                elapsed = now - self._last_request_ts
                min_interval = 0.25
                wait_time = 0.0
                if elapsed < min_interval:
                    wait_time = min_interval - elapsed
                    self._last_request_ts = now + wait_time
                else:
                    self._last_request_ts = now

            if wait_time > 0.0:
                time.sleep(wait_time)

            # Execute original request outside the lock
            resp = original_request(*args, **kwargs)

            try:
                status_code = getattr(resp, "status_code", None)
                if status_code == 429:
                    url = kwargs.get("url") or (args[1] if len(args) > 1 else "")
                    logger.warning("yfinance session received 429 for url: %s", url)
                    self.mark_rate_limited("yfinance", duration=300)
                elif status_code == 401:
                    url = kwargs.get("url") or (args[1] if len(args) > 1 else "")
                    logger.warning("yfinance session received 401 (Invalid Crumb) for url: %s", url)
                    self.mark_rate_limited("yfinance", duration=120)
            except Exception as e:
                logger.debug("Error in session wrapper: %s", e)
            return resp

        session.request = custom_request


        with self._lock:
            self._all_sessions.append(session)
        return session

    def get_session(self):
        with self._lock:
            idx = self._ua_index
            current_epoch = self._session_epoch
            if not hasattr(self._local, "sessions"):
                self._local.sessions = {}
            
            if idx in self._local.sessions:
                sess, epoch = self._local.sessions[idx]
                if epoch == current_epoch:
                    return sess
                else:
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
        with self._lock:
            self._excluded_until[key] = time.time() + duration
            self._ua_index = (self._ua_index + 1) % len(YFINANCE_USER_AGENTS)
            self._session_epoch += 1
            logger.warning(
                "YFinanceSessionManager rotated due to 429/limit. UA index: %d, epoch: %d",
                self._ua_index,
                self._session_epoch,
            )

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
        """全セッションをクリーンアップ"""
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
            self._session_epoch += 1


yf_session_manager = YFinanceSessionManager()

# #endregion yfinance Session Management


# #region Logging Filter & Formatter Classes

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
    def __init__(self, log_level=logging.INFO):
        super().__init__()
        self.log_level = log_level

    def filter(self, record):
        if record.levelno >= logging.WARNING:
            return True
        if record.levelno < logging.INFO:
            return self.log_level <= record.levelno
        msg = record.getMessage()
        return any(pattern in msg for pattern in IMPORTANT_INFO_PATTERNS)


class PollingFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        if " 200 -" in msg and any(
            x in msg for x in ["GET /api/indices", "GET /api/health", "GET /api/stocks"]
        ):
            return False
        return True


# #endregion Logging Filter & Formatter Classes


# #region Application State Groups


class ExecutionState:
    """スレッドプールとバックグラウンドタスクの実行を管理するクラス。"""

    def __init__(self):
        self.executor = DaemonThreadPoolExecutor(max_workers=5)
        self.news_executor = DaemonThreadPoolExecutor(max_workers=4)
        self.sync_refresh_executor = DaemonThreadPoolExecutor(max_workers=1)
        self.shutdown_event = threading.Event()
        self.background_threads: list[threading.Thread] = []

    def shutdown(self):
        """Shut down all executors safely."""
        self.shutdown_event.set()
        for ex in [
            self.executor,
            self.news_executor,
            self.sync_refresh_executor,
        ]:
            try:
                ex.shutdown(wait=True, cancel_futures=True)
            except TypeError:
                ex.shutdown(wait=True)

        for t in self.background_threads:
            try:
                if t.is_alive():
                    t.join(timeout=2.0)
            except Exception:
                pass


class MarketDataState:
    """銘柄データ、市場状況、およびyfinanceのレート制限を管理するクラス。"""
    is_yfinance_rate_limited: bool
    yfinance_rate_limit_until: float
    yfinance_lock: threading.RLock
    last_usdjpy_rate: float

    def __init__(self):
        self.user_us = {}
        self.user_jp = {}
        self.user_idx = {}
        self.user_stocks_lock = threading.RLock()
        default_usdjpy = 150.00
        try:
            default_usdjpy = float(os.environ.get("MNS_DEFAULT_USDJPY", "150.00"))
        except (ValueError, TypeError):
            pass
        self.last_usdjpy_rate = default_usdjpy
        self.last_modified_ns = 0
        self.current_stocks_cache: Dict[str, List[Any]] = {"us": [], "jp": [], "idx": []}
        self.target_stocks_cache: Dict[str, List[Any]] = {"us": [], "jp": [], "idx": []}
        self.current_indices_cache = {}
        self.target_indices_cache = {}
        self.is_syncing = False
        self.is_syncing_lock = threading.RLock()
        self.sync_scheduled = False
        self.sync_schedule_lock = threading.RLock()
        self.sync_pending = False
        self.market_status_cache: Dict[str, Optional[str]] = {"us": None, "jp": None, "idx": None}
        self.market_status_lock = threading.RLock()

        # yfinance rate limiting
        self.yfinance_lock = threading.RLock()
        self.is_yfinance_rate_limited = False
        self.yfinance_rate_limit_until = 0.0
        self.yfinance_last_request_ts = 0.0
        self.yfinance_min_interval_sec = 0.8
        self.yfinance_429_streak = 0
        self.yfinance_429_backoff_multiplier = 2.0
        self.yfinance_max_backoff_sec = 60.0
        self.yfinance_history_semaphore = threading.Semaphore(2)

        # Circuit breakers
        self.circuit_lock = threading.RLock()
        # For backward compatibility with existing tests and code
        self.history_circuit_lock = self.circuit_lock
        self.history_circuit_state: Dict[str, Any] = {}  # Alias/Backing for tests

        # {service_key: {"status": "CLOSED"|"OPEN"|"HALF_OPEN", "timeout_streak": int, "open_until": float}}
        self.circuit_states = {
            "mistral": {"status": "CLOSED", "timeout_streak": 0, "open_until": 0.0},
            "langsearch": {"status": "CLOSED", "timeout_streak": 0, "open_until": 0.0},
        }
        self.history_circuit_states = self.history_circuit_state

    def get_circuit_state(self, service: str, symbol: Optional[str] = None):
        """サーキットブレーカーの状態を取得。symbol指定時は個別の状態を返す。"""
        with self.circuit_lock:
            if symbol:
                if symbol not in self.history_circuit_states:
                    self.history_circuit_states[symbol] = {
                        "status": "CLOSED",
                        "timeout_streak": 0,
                        "open_until": 0.0,
                    }
                return self.history_circuit_states[symbol]
            return self.circuit_states.get(
                service, {"status": "CLOSED", "timeout_streak": 0, "open_until": 0.0}
            )

    def report_circuit_result(
        self,
        service: str,
        success: bool,
        symbol: Optional[str] = None,
        threshold=3,
        open_sec=30,
    ):
        """API呼び出しの結果を報告し、サーキットの状態を更新する。"""
        now = time.time()
        with self.circuit_lock:
            if symbol and symbol not in self.history_circuit_states:
                self.history_circuit_states[symbol] = {
                    "status": "CLOSED",
                    "timeout_streak": 0,
                    "open_until": 0.0,
                }

            target: Optional[Dict[str, Any]] = (
                self.history_circuit_states[symbol]
                if symbol
                else self.circuit_states.get(service)
            )
            if not target:
                return

            if success:
                target["status"] = "CLOSED"
                target["timeout_streak"] = 0
                target["open_until"] = 0.0
            else:
                if target.get("status") == "HALF_OPEN":
                    target["status"] = "OPEN"
                    target["open_until"] = now + open_sec
                    target["timeout_streak"] = 0
                else:
                    target["timeout_streak"] = int(target.get("timeout_streak") or 0) + 1
                    if target["timeout_streak"] >= threshold:
                        target["status"] = "OPEN"
                        target["open_until"] = now + open_sec
                        target["timeout_streak"] = 0

    def is_circuit_open(self, service: str, symbol: Optional[str] = None) -> bool:
        """サーキットが遮断中（OPEN）か判定する。"""
        now = time.time()
        with self.circuit_lock:
            target: Optional[Dict[str, Any]] = (
                self.history_circuit_states.get(symbol)
                if symbol
                else self.circuit_states.get(service)
            )
            if not target:
                return False

            if target.get("status") == "OPEN":
                if now >= float(target.get("open_until") or 0.0):
                    target["status"] = "HALF_OPEN"
                    return False
                return True
            return False

    def set_syncing(self, value: bool):
        """同期中フラグを設定"""
        with self.is_syncing_lock:
            self.is_syncing = value

    def update_market_status(self, market: str, status: Optional[str]):
        """市場ステータスを更新"""
        with self.market_status_lock:
            self.market_status_cache[market] = status

    def get_market_status(self, market: str) -> Optional[str]:
        """市場ステータスを取得"""
        with self.market_status_lock:
            value = self.market_status_cache.get(market)
            return None if value is None else value

    def is_yf_rate_limited(self) -> bool:
        """yfinanceが現在レート制限中か判定"""
        with self.yfinance_lock:
            return self.is_yfinance_rate_limited and (time.time() < self.yfinance_rate_limit_until)

    def mark_yf_429(self) -> float:
        """yfinance of 429 error logs and sets backoff"""
        with self.yfinance_lock:
            self.yfinance_429_streak = min(self.yfinance_429_streak + 1, 5)
            self.is_yfinance_rate_limited = True
            backoff = min(
                self.yfinance_429_backoff_multiplier**self.yfinance_429_streak,
                self.yfinance_max_backoff_sec,
            )
            self.yfinance_rate_limit_until = time.time() + backoff
            try:
                yf_session_manager.mark_rate_limited("yfinance", int(backoff))
            except Exception as e:
                logger.debug(
                    "Failed to call yf_session_manager.mark_rate_limited: %s", e
                )
            return backoff


class AIState:
    """Mistral, LangSearch, およびチャット履歴の状態を管理するクラス。"""

    def __init__(self):
        self.mistral_call_semaphore = threading.Semaphore(3)
        self.mistral_cooldown_lock = threading.Lock()
        self.mistral_next_allowed_ts = 0.0
        self.mistral_429_streak = 0
        self.mistral_last_call_ts = 0.0
        self.mistral_response_cache: TTLCache[Any, Any] = TTLCache(maxsize=128, ttl=240)
        self.mistral_response_lock = threading.Lock()
        self.mistral_clients: LRUCache[Any, Any] = LRUCache(maxsize=128)  # {(api_key, thread_id): Mistral}
        self.mistral_clients_lock = threading.Lock()

        self.langsearch_rate_lock = threading.Lock()
        self.langsearch_next_allowed_ts = 0.0
        self.langsearch_min_interval_sec = 2.0
        self.langsearch_429_cooldown_sec = 90.0

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

    def mark_mistral_429(self, retry_after_sec=None) -> float:
        """Mistralの429エラーを記録し Retry-After 優先でバックオフを適用"""
        with self.mistral_cooldown_lock:
            self.mistral_429_streak = min(self.mistral_429_streak + 1, 6)
            exponential_backoff = min(2.0**self.mistral_429_streak, 120.0)
            try:
                retry_after = max(0.0, float(retry_after_sec or 0.0))
            except (TypeError, ValueError):
                retry_after = 0.0
            backoff = min(max(exponential_backoff, retry_after), 300.0)
            self.mistral_next_allowed_ts = time.time() + backoff
            return backoff

    def reset_mistral_streak(self):
        """Mistralのエラーストリークをリセット"""
        with self.mistral_cooldown_lock:
            self.mistral_429_streak = 0
            self.mistral_next_allowed_ts = 0.0

    def get_or_create_mistral_client(self, api_key: str):
        """APIキーと現在のスレッドに対応するMistralクライアントを取得または作成"""
        thread_id = threading.get_ident()
        cache_key = (api_key, thread_id)
        with self.mistral_clients_lock:
            if cache_key in self.mistral_clients:
                return self.mistral_clients[cache_key]

            from config_utils import _env_float

            timeout_sec = _env_float("MNS_MISTRAL_API_TIMEOUT", 45.0, 5.0, 180.0)
            client = Mistral(api_key=api_key, timeout_ms=int(timeout_sec * 1000))
            self.mistral_clients[cache_key] = client
            return client


class CacheState:
    """グローバルなTTLCacheとフェッチイベントを管理するクラス。"""

    def __init__(self):
        self.caches = {}  # Map of duration -> TTLCache
        self.cache_lock = threading.Lock()
        self.file_lock = threading.Lock()
        self.fetch_events = {}
        self.fetch_events_lock = threading.Lock()
        self.sse_data_lock = threading.RLock()
        self.stats_lock = threading.Lock()
        self.cache_hits = 0
        self.cache_misses = 0

    def record_hit(self):
        with self.stats_lock:
            self.cache_hits += 1

    def record_miss(self):
        with self.stats_lock:
            self.cache_misses += 1

    def get_stats(self):
        with self.stats_lock:
            total = self.cache_hits + self.cache_misses
            hit_rate = (self.cache_hits / total * 100) if total > 0 else 0.0
            return {
                "hits": self.cache_hits,
                "misses": self.cache_misses,
                "total": total,
                "hit_rate_pct": round(hit_rate, 2),
            }

    def reset_stats(self):
        with self.stats_lock:
            self.cache_hits = 0
            self.cache_misses = 0


class MessageAnnouncer:
    """SSE配信用のリスナー管理クラス"""

    def __init__(self):
        self.listeners = []
        self.lock = threading.Lock()

    def listen(self):
        """SSEリスナー用キューを登録して返す"""
        import queue

        q: queue.Queue[Any] = queue.Queue(maxsize=5)
        with self.lock:
            if len(self.listeners) >= MAX_SSE_LISTENERS:
                raise RuntimeError("too many SSE listeners")
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
        import queue

        with self.lock:
            targets = list(self.listeners)

        for q in targets:
            try:
                q.put_nowait(msg)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    logger.warning(
                        "SSE queue overflow persists: dropping latest message for one listener"
                    )

    def listener_count(self):
        """現在のリスナー数を返す"""
        with self.lock:
            return len(self.listeners)


class ShutdownTokenManager:
    """シャットダウントークンの生成・検証・ローテーションを管理するクラス"""
    shutdown_token: Optional[str]
    shutdown_token_used: bool

    def __init__(self, logger=None):
        from pathlib import Path
        self.logger = logger or logging.getLogger("backend")
        self.token_file = Path(__file__).resolve().parent / ".mns_shutdown_token"
        self.used_marker = Path(__file__).resolve().parent / ".mns_shutdown_token.used"
        self.shutdown_token = None
        self.shutdown_token_used = False

    def get_or_create_shutdown_token(self) -> str:
        if self.shutdown_token and not self.used_marker.exists():
            return self.shutdown_token

        was_used = self.used_marker.exists()
        if was_used:
            self.used_marker.unlink(missing_ok=True)

        try:
            if not self.used_marker.exists() and self.token_file.exists():
                from config_utils import enforce_secure_permissions, unprotect_data
                enforce_secure_permissions(self.token_file)
                raw = self.token_file.read_text(encoding="utf-8").strip()
                if raw:
                    try:
                        entry = json.loads(raw)
                        token = unprotect_data(entry, "shutdown_token")
                    except (json.JSONDecodeError, TypeError, ValueError):
                        self.logger.warning(
                            "Ignoring legacy plaintext shutdown token file; regenerating secure token."
                        )
                        token = ""
                    if token:
                        self.shutdown_token = token
                        self.shutdown_token_used = False
                        return self.shutdown_token
        except (OSError, UnicodeDecodeError):
            pass

        import secrets
        from config_utils import protect_data, enforce_secure_permissions
        token = secrets.token_urlsafe(32)
        self.shutdown_token = token
        self.shutdown_token_used = False
        try:
            protected = protect_data(token, "shutdown_token")
            self.token_file.write_text(json.dumps(protected), encoding="utf-8")
            enforce_secure_permissions(self.token_file)
            self.logger.info("Session shutdown token generated and secured.")
        except Exception as exc:
            self.logger.error("Failed to write shutdown token file: %s", exc)
        return self.shutdown_token

    def consume_shutdown_token(self, token: str) -> bool:
        if not self.shutdown_token:
            self.logger.warning("No shutdown token configured")
            return False

        if self.shutdown_token_used:
            self.logger.warning("Shutdown token already used")
            return False

        if token is None or not isinstance(token, str):
            return False

        import secrets
        if not secrets.compare_digest(self.shutdown_token, token):
            return False

        self.shutdown_token_used = True
        return True

    def rotate_shutdown_token(self):
        import secrets
        from config_utils import protect_data
        new_token = secrets.token_urlsafe(32)
        self.shutdown_token = new_token
        self.shutdown_token_used = False

        try:
            protected = protect_data(new_token, "shutdown_token")
            self.token_file.write_text(json.dumps(protected), encoding="utf-8")
            if platform.system().lower() != "windows":
                self.token_file.chmod(0o600)
            self.used_marker.write_text(str(time.time()), encoding="utf-8")
            self.logger.info("New shutdown token generated after consumption.")
        except Exception as exc:
            self.logger.error("Failed to write new shutdown token: %s", exc)





class AppState:
    """分散型アプリケーション状態管理クラス。

    ExecutionState, MarketDataState, AIState, CacheState に責務を分割し、
    AppState は統一インターフェースとして @property と明示的メソッド委譲を提供する。
    """

    execution: ExecutionState
    market: MarketDataState
    ai: AIState
    cache: CacheState
    shutdown_manager: ShutdownTokenManager
    stock_provider: Any

    # Attributes set directly in __init__ (not delegated via @property)
    history_fetch_inflight: Set[str]
    history_fetch_lock: threading.Lock
    sse_announcer: 'MessageAnnouncer'

    def __init__(self):
        self.execution = ExecutionState()
        self.market = MarketDataState()
        self.ai = AIState()
        self.cache = CacheState()
        self.shutdown_manager = ShutdownTokenManager()
        self.history_fetch_inflight = set()
        self.history_fetch_lock = threading.Lock()

        from services.stock_provider import YFinanceProvider
        self.stock_provider = YFinanceProvider()

        # Persistent disk cache — survives server restarts so that cold-start
        # can serve recent stock data immediately without waiting for yfinance.
        from constants import BASE_DIR, STOCK_HISTORY_CACHE_MAXSIZE, STOCK_HISTORY_DISK_CACHE_TTL
        from utils.disk_cache import StockDiskCache
        self.stock_disk_cache = StockDiskCache(
            cache_dir=BASE_DIR / ".cache" / "stock_history",
            max_entries=STOCK_HISTORY_CACHE_MAXSIZE,
            default_ttl=STOCK_HISTORY_DISK_CACHE_TTL,
        )
        # Separate disk cache for full stock payloads (used for cold-start warm-up)
        self.payload_disk_cache = StockDiskCache(
            cache_dir=BASE_DIR / ".cache" / "stock_payloads",
            max_entries=256,
            default_ttl=3600,
        )

        self.sse_announcer = MessageAnnouncer()
        self._extension_origins_cache = set()
        self._extension_origins_cache_ts = 0.0
        self._extension_origins_cache_lock = threading.Lock()
        self._extension_manifest_status = {"ok": True, "error": ""}
        self.EXTENSION_MANIFEST_ERROR_LOGGED = False
        self._EXTENSION_ORIGINS_CACHE_TTL_SEC = 30.0

    def update_market_status(self, market: str, status: Optional[str]):
        return self.market.update_market_status(market, status)

    def get_market_status(self, market: str) -> Optional[str]:
        return self.market.get_market_status(market)


    # Method Delegations
    def is_circuit_open(self, service: str, symbol: Optional[str] = None) -> bool:
        return self.market.is_circuit_open(service, symbol)

    def report_circuit_result(
        self,
        service: str,
        success: bool,
        symbol: Optional[str] = None,
        threshold=3,
        open_sec=30,
    ):
        return self.market.report_circuit_result(service, success, symbol, threshold, open_sec)

    def get_circuit_state(self, service: str, symbol: Optional[str] = None):
        return self.market.get_circuit_state(service, symbol)

    def set_syncing(self, value: bool):
        return self.market.set_syncing(value)

    def is_yf_rate_limited(self) -> bool:
        return self.market.is_yf_rate_limited()

    def mark_yf_429(self) -> float:
        return self.market.mark_yf_429()

    def add_chat_history(self, key, message):
        return self.ai.add_chat_history(key, message)

    def mark_mistral_429(self, retry_after_sec=None) -> float:
        return self.ai.mark_mistral_429(retry_after_sec)

    def reset_mistral_streak(self):
        return self.ai.reset_mistral_streak()

    def get_or_create_mistral_client(self, api_key: str):
        return self.ai.get_or_create_mistral_client(api_key)

    def shutdown_executors(self):
        """Clean up background resources with deadlock prevention."""
        self.execution.shutdown()

        # Clean up YFinance sessions safely
        try:
            yf_session_manager.close_all()
        except Exception as e:
            logger.debug("Error closing YFinance sessions: %s", e)

        # Close Mistral clients to avoid unclosed socket warnings
        try:
            if hasattr(
                self.ai, "mistral_clients_lock"
            ) and self.ai.mistral_clients_lock.acquire(timeout=2.0):
                try:
                    for client in self.ai.mistral_clients.values():
                        if hasattr(client, "close"):
                            try:
                                client.close()
                            except Exception:
                                pass
                    self.ai.mistral_clients.clear()
                finally:
                    self.ai.mistral_clients_lock.release()
            else:
                logger.warning("Timeout acquiring mistral_clients_lock during shutdown")
        except Exception as e:
            logger.debug("Error closing Mistral clients: %s", e)

    def get_or_create_shutdown_token(self) -> str:
        return self.shutdown_manager.get_or_create_shutdown_token()

    def consume_shutdown_token(self, token: str) -> bool:
        return self.shutdown_manager.consume_shutdown_token(token)

    def rotate_shutdown_token(self):
        self.shutdown_manager.rotate_shutdown_token()

    def record_hit(self):
        self.cache.record_hit()

    def record_miss(self):
        self.cache.record_miss()

    def get_stats(self):
        return self.cache.get_stats()

    def reset_stats(self):
        self.cache.reset_stats()


# Instantiation
app_state = AppState()
