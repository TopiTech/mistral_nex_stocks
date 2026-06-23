# app_state.py
"""Application state management, logging filters, and Pydantic schemas."""

import json
import logging
import platform
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Set

from cachetools import LRUCache, TTLCache
from pydantic import BaseModel, Field

from constants import MAX_SSE_LISTENERS
from mistral_compat import Mistral  # type: ignore[attr-defined,no-redef]

logger = logging.getLogger("backend")


# #region Pydantic Models for Structured Outputs


class NewsSummaryModel(BaseModel):
    """ニュース要約用の3セクション構造化モデル"""

    us: str = Field(description="US市場の要約文 (複数行)")
    jp: str = Field(description="日本市場の要約文 (複数行)")
    trends: str = Field(description="トレンド情報の要約文 (複数行)")


class StockAnalysis(BaseModel):
    """個別銘柄のAI分析結果用の構造化モデル (2026仕様)"""

    recommendation: str = Field(
        description="Investment recommendation",
        pattern="^(強い買い|買い|中立|売り|強い売り)$",
    )
    sentiment: str = Field(description="Market sentiment", pattern="^(強気|中立|弱気)$")
    target_price_3m: float = Field(description="3-month target price")
    upside_3m: str = Field(description="3-month upside percentage, e.g. '+10%'")
    confidence: str = Field(
        description="Analysis confidence level", pattern="^(高|中|低)$"
    )
    analysis_summary: str = Field(description="100-character summary of analysis")
    key_catalysts: List[str] = Field(
        description="Key catalysts (up to 3 items)", max_length=3
    )
    risk_factors: List[str] = Field(
        description="Risk factors (up to 2 items)", max_length=2
    )
    technical_analysis: str = Field(
        description="Technical analysis summary (50 chars max)"
    )
    fundamental_analysis: str = Field(
        description="Fundamental analysis summary (50 chars max)"
    )
    latest_news_impact: str = Field(description="Impact of latest news (90 chars max)")


# Re-export NewsFormatter from its dedicated module for backward compatibility
from services.news_formatter import NewsFormatter  # noqa: E402,F401

# #endregion Pydantic Models for Structured Outputs


# #region yfinance Session Management

try:
    from keyring.errors import KeyringError
except ImportError:

    class KeyringError(Exception):  # type: ignore[no-redef]
        """Fallback if keyring is not installed."""


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
            session = curl_requests.Session(impersonate="chrome")
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
                if elapsed < min_interval:
                    time.sleep(min_interval - elapsed)
                self._last_request_ts = time.time()

                # Execute original request inside the lock to serialize network call
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
                        pass
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
                    pass
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


class DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor subclass that spawns daemon threads and prevents blocking shutdown."""

    def _get_executor_threads(self):
        """Get worker threads belonging to this executor across Python versions."""
        try:
            # Python 3.9+: _threads is a set of Thread objects
            return list(self._threads)
        except AttributeError:
            pass
        # Fallback: enumerate all threads and match by prefix
        prefix = getattr(self, "_thread_name_prefix", "") or ""
        if prefix:
            return [
                t for t in threading.enumerate()
                if t.name and t.name.startswith(prefix)
            ]
        return []

    def submit(self, fn, /, *args, **kwargs):
        future = super().submit(fn, *args, **kwargs)
        try:
            for t in self._get_executor_threads():
                if not t.daemon:
                    t.daemon = True
        except Exception:
            pass

        def _done_callback(fut):
            try:
                exc = fut.exception()
                if exc:
                    logger.error(
                        "Background task %s failed with exception: %s",
                        fn.__name__ if hasattr(fn, "__name__") else str(fn),
                        exc,
                        exc_info=exc
                    )
                else:
                    logger.debug(
                        "Background task %s completed successfully",
                        fn.__name__ if hasattr(fn, "__name__") else str(fn)
                    )
            except Exception as cb_exc:
                logger.error("Error in background task done callback: %s", cb_exc)

        future.add_done_callback(_done_callback)
        return future


class ExecutionState:
    """スレッドプールとバックグラウンドタスクの実行を管理するクラス。"""

    def __init__(self):
        self.executor = DaemonThreadPoolExecutor(max_workers=5)
        self.news_executor = DaemonThreadPoolExecutor(max_workers=4)
        self.sync_refresh_executor = DaemonThreadPoolExecutor(max_workers=1)
        self.shutdown_event = threading.Event()

    def shutdown(self):
        """Shut down all executors safely."""
        self.shutdown_event.set()
        for ex in [
            self.executor,
            self.news_executor,
            self.sync_refresh_executor,
        ]:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                ex.shutdown(wait=False)


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
        self.sync_pending = False
        self.market_status_cache = {"us": None, "jp": None, "idx": None}
        self.market_status_lock = threading.Lock()

        # yfinance rate limiting
        self.yfinance_lock = threading.RLock()
        self.is_yfinance_rate_limited = False
        self.yfinance_rate_limit_until = 0.0
        self.yfinance_last_request_ts = 0.0
        self.yfinance_min_interval_sec = 0.8
        self.yfinance_429_streak = 0
        self.yfinance_429_backoff_multiplier = 2.0
        self.yfinance_max_backoff_sec = 60.0

        # Circuit breakers
        self.circuit_lock = threading.Lock()
        # For backward compatibility with existing tests and code
        self.history_circuit_lock = self.circuit_lock
        self.history_circuit_state = {}  # Alias/Backing for tests

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

            target = (
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
            target = (
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
            return self.market_status_cache.get(market)

    def is_yf_rate_limited(self) -> bool:
        """yfinanceが現在レート制限中か判定"""
        with self.yfinance_lock:
            return self.is_yfinance_rate_limited and (
                time.time() < self.yfinance_rate_limit_until
            )

    def mark_yf_429(self):
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
        self.mistral_call_semaphore = threading.Semaphore(1)
        self.mistral_cooldown_lock = threading.Lock()
        self.mistral_next_allowed_ts = 0.0
        self.mistral_429_streak = 0
        self.mistral_last_call_ts = 0.0
        self.mistral_response_cache = TTLCache(maxsize=128, ttl=240)
        self.mistral_response_lock = threading.Lock()
        self.mistral_clients = LRUCache(maxsize=128)  # {(api_key, thread_id): Mistral}
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

    def mark_mistral_429(self, retry_after_sec=None):
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
        self.sse_data_lock = threading.Lock()
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

        q = queue.Queue(maxsize=5)
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
            for i in reversed(range(len(self.listeners))):
                try:
                    self.listeners[i].put_nowait(msg)
                except queue.Full:
                    try:
                        self.listeners[i].get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self.listeners[i].put_nowait(msg)
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
                        return token
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
        return token

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
    """論理的にグループ化されたレガシープロキシをサポートする分散型アプリケーション状態管理クラス。"""

    execution: ExecutionState
    market: MarketDataState
    ai: AIState
    cache: CacheState
    shutdown_manager: ShutdownTokenManager
    stock_provider: Any

    # --- Type annotations for properties proxied via __getattr__ ---
    # MarketDataState
    user_us: Dict[str, Any]
    user_jp: Dict[str, Any]
    user_idx: Dict[str, Any]
    user_stocks_lock: threading.RLock
    last_modified_ns: int
    current_stocks_cache: Dict[str, List[Dict[str, Any]]]
    target_stocks_cache: Dict[str, List[Dict[str, Any]]]
    current_indices_cache: Dict[str, Any]
    target_indices_cache: Dict[str, Any]
    is_syncing: bool
    is_syncing_lock: threading.Lock
    sync_scheduled: bool
    sync_schedule_lock: threading.Lock
    sync_pending: bool
    market_status_cache: Dict[str, Optional[str]]
    market_status_lock: threading.Lock
    yfinance_lock: threading.RLock
    is_yfinance_rate_limited: bool
    yfinance_rate_limit_until: float
    yfinance_last_request_ts: float
    yfinance_min_interval_sec: float
    yfinance_429_streak: int
    yfinance_429_backoff_multiplier: float
    yfinance_max_backoff_sec: float
    circuit_lock: threading.Lock
    history_circuit_lock: threading.Lock
    history_circuit_state: Dict[str, Any]
    circuit_states: Dict[str, Any]
    history_circuit_states: Dict[str, Any]

    # AIState
    mistral_call_semaphore: threading.Semaphore
    mistral_cooldown_lock: threading.Lock
    mistral_next_allowed_ts: float
    mistral_429_streak: int
    mistral_last_call_ts: float
    mistral_response_cache: Any
    mistral_response_lock: threading.Lock
    mistral_clients: Any
    mistral_clients_lock: threading.Lock
    langsearch_rate_lock: threading.Lock
    langsearch_next_allowed_ts: float
    langsearch_min_interval_sec: float
    langsearch_429_cooldown_sec: float
    trends_refresh_inflight: Set[str]
    trends_refresh_lock: threading.Lock
    chat_history: Any
    chat_history_lock: threading.Lock
    max_history: int

    # CacheState
    caches: Dict[int, Any]
    cache_lock: threading.Lock
    file_lock: threading.Lock
    fetch_events: Dict[str, threading.Event]
    fetch_events_lock: threading.Lock
    sse_data_lock: threading.Lock
    stats_lock: threading.Lock
    cache_hits: int
    cache_misses: int

    def __init__(self):
        self.execution = ExecutionState()
        self.market = MarketDataState()
        self.ai = AIState()
        self.cache = CacheState()
        self.shutdown_manager = ShutdownTokenManager()

        from services.stock_provider import YFinanceProvider
        self.stock_provider = YFinanceProvider()

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

    def __getattr__(self, name):
        """Dynamically resolve attributes from logical state groups."""
        # Performance optimization: check common attributes directly first
        # These are the most frequently accessed hot-path attributes
        if name == "current_stocks_cache":
            return self.market.current_stocks_cache
        if name == "target_stocks_cache":
            return self.market.target_stocks_cache
        if name == "current_indices_cache":
            return self.market.current_indices_cache
        if name == "target_indices_cache":
            return self.market.target_indices_cache
        if name == "execution_executor":
            return self.execution.executor
        if name == "user_us":
            return self.market.user_us
        if name == "user_jp":
            return self.market.user_jp
        if name == "user_idx":
            return self.market.user_idx
        if name == "user_stocks_lock":
            return self.market.user_stocks_lock
        if name == "is_syncing":
            return self.market.is_syncing
        if name == "is_syncing_lock":
            return self.market.is_syncing_lock
        if name == "sync_scheduled":
            return self.market.sync_scheduled
        if name == "sync_schedule_lock":
            return self.market.sync_schedule_lock
        if name == "sync_pending":
            return self.market.sync_pending
        if name == "market_status_cache":
            return self.market.market_status_cache
        if name == "market_status_lock":
            return self.market.market_status_lock
        if name == "yfinance_lock":
            return self.market.yfinance_lock
        if name == "is_yfinance_rate_limited":
            return self.market.is_yfinance_rate_limited
        if name == "yfinance_rate_limit_until":
            return self.market.yfinance_rate_limit_until
        if name == "yfinance_last_request_ts":
            return self.market.yfinance_last_request_ts
        if name == "yfinance_min_interval_sec":
            return self.market.yfinance_min_interval_sec
        if name == "yfinance_429_streak":
            return self.market.yfinance_429_streak
        if name == "yfinance_429_backoff_multiplier":
            return self.market.yfinance_429_backoff_multiplier
        if name == "yfinance_max_backoff_sec":
            return self.market.yfinance_max_backoff_sec
        if name == "circuit_lock":
            return self.market.circuit_lock
        if name == "history_circuit_lock":
            return self.market.history_circuit_lock
        if name == "history_circuit_state":
            return self.market.history_circuit_state
        if name == "circuit_states":
            return self.market.circuit_states
        if name == "history_circuit_states":
            return self.market.history_circuit_states
        if name == "mistral_call_semaphore":
            return self.ai.mistral_call_semaphore
        if name == "mistral_cooldown_lock":
            return self.ai.mistral_cooldown_lock
        if name == "mistral_next_allowed_ts":
            return self.ai.mistral_next_allowed_ts
        if name == "mistral_429_streak":
            return self.ai.mistral_429_streak
        if name == "mistral_last_call_ts":
            return self.ai.mistral_last_call_ts
        if name == "mistral_response_cache":
            return self.ai.mistral_response_cache
        if name == "mistral_response_lock":
            return self.ai.mistral_response_lock
        if name == "mistral_clients":
            return self.ai.mistral_clients
        if name == "mistral_clients_lock":
            return self.ai.mistral_clients_lock
        if name == "langsearch_rate_lock":
            return self.ai.langsearch_rate_lock
        if name == "langsearch_next_allowed_ts":
            return self.ai.langsearch_next_allowed_ts
        if name == "langsearch_min_interval_sec":
            return self.ai.langsearch_min_interval_sec
        if name == "langsearch_429_cooldown_sec":
            return self.ai.langsearch_429_cooldown_sec
        if name == "trends_refresh_inflight":
            return self.ai.trends_refresh_inflight
        if name == "trends_refresh_lock":
            return self.ai.trends_refresh_lock
        if name == "chat_history":
            return self.ai.chat_history
        if name == "chat_history_lock":
            return self.ai.chat_history_lock
        if name == "max_history":
            return self.ai.max_history
        if name == "caches":
            return self.cache.caches
        if name == "cache_lock":
            return self.cache.cache_lock
        if name == "file_lock":
            return self.cache.file_lock
        if name == "fetch_events":
            return self.cache.fetch_events
        if name == "fetch_events_lock":
            return self.cache.fetch_events_lock
        if name == "sse_data_lock":
            return self.cache.sse_data_lock
        if name == "stats_lock":
            return self.cache.stats_lock
        if name == "cache_hits":
            return self.cache.cache_hits
        if name == "cache_misses":
            return self.cache.cache_misses

        for group_name in ("execution", "market", "ai", "cache"):
            group = getattr(self, group_name, None)
            if group is not None and hasattr(group, name):
                return getattr(group, name)
        raise AttributeError(
            f"'{self.__class__.__name__}' object has no attribute '{name}'"
        )

    def __setattr__(self, name, value):
        """Dynamically set attributes in logical state groups if they exist there."""
        if name in (
            "execution",
            "market",
            "ai",
            "cache",
            "shutdown_manager",
            "sse_announcer",
            "stock_provider",
            "_extension_origins_cache",
            "_extension_origins_cache_ts",
            "_extension_origins_cache_lock",
            "_extension_manifest_status",
            "EXTENSION_MANIFEST_ERROR_LOGGED",
            "_EXTENSION_ORIGINS_CACHE_TTL_SEC",
        ):
            super().__setattr__(name, value)
            return

        # Hot-path optimized attributes
        if name == "current_stocks_cache":
            self.market.current_stocks_cache = value
            return
        if name == "target_stocks_cache":
            self.market.target_stocks_cache = value
            return
        if name == "current_indices_cache":
            self.market.current_indices_cache = value
            return
        if name == "target_indices_cache":
            self.market.target_indices_cache = value
            return
        if name == "user_us":
            self.market.user_us = value
            return
        if name == "user_jp":
            self.market.user_jp = value
            return
        if name == "user_idx":
            self.market.user_idx = value
            return
        if name == "is_syncing":
            self.market.is_syncing = value
            return
        if name == "is_yfinance_rate_limited":
            self.market.is_yfinance_rate_limited = value
            return
        if name == "yfinance_rate_limit_until":
            self.market.yfinance_rate_limit_until = value
            return
        if name == "yfinance_last_request_ts":
            self.market.yfinance_last_request_ts = value
            return
        if name == "sync_scheduled":
            self.market.sync_scheduled = value
            return
        if name == "sync_pending":
            self.market.sync_pending = value
            return
        if name == "chat_history":
            self.ai.chat_history = value
            return
        if name == "caches":
            self.cache.caches = value
            return
        if name == "fetch_events":
            self.cache.fetch_events = value
            return

        for group_name in ("execution", "market", "ai", "cache"):
            group = getattr(self, group_name, None)
            if group is not None and hasattr(group, name):
                setattr(group, name, value)
                return

        super().__setattr__(name, value)

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
app_state.sse_announcer = MessageAnnouncer()
