"""
ai_state.py - AI service state management (Mistral, LangSearch, chat history).

Extracted from app_state.py to reduce module complexity.
"""

import logging
import math
import ssl
import threading
import time
from collections import deque
from typing import Any, ClassVar

import httpx
from cachetools import LRUCache, TTLCache

from constants import (
    DEFAULT_USDJPY_RATE,
    MISTRAL_API_TIMEOUT_SEC,
    MISTRAL_BASE_URL,
    STREAM_CHAT_MAX_CONCURRENT,
)

logger = logging.getLogger("backend")


def _tracked_usdjpy_rate() -> float:
    """Best-effort read of the tracked USD/JPY rate (0.0 when unavailable).

    The import is local: app_state aggregates this module, so a module-level
    back-import would be circular.
    """
    try:
        from app_state import app_state

        market = app_state.market
        snapshot_fn = getattr(market, "get_usdjpy_rate_snapshot", None)
        if callable(snapshot_fn):
            rate, _ = snapshot_fn()
        else:
            raw_rate = getattr(market, "last_usdjpy_rate", 0.0)
            rate = float(raw_rate or 0.0)
        return rate if math.isfinite(rate) else 0.0
    except Exception:
        return 0.0


class AIState:
    """Manages Mistral, LangSearch, and chat history state."""

    # Pricing per 1M tokens in USD
    _MISTRAL_PRICING: ClassVar[dict[str, dict[str, float]]] = {
        "mistral-small-2603": {"prompt": 0.10, "completion": 0.30},
        "mistral-small-latest": {"prompt": 0.10, "completion": 0.30},
        "mistral-medium-2604": {"prompt": 0.40, "completion": 1.20},
        "mistral-medium-latest": {"prompt": 0.40, "completion": 1.20},
        "mistral-large-2512": {"prompt": 2.00, "completion": 6.00},
        "mistral-large-latest": {"prompt": 2.00, "completion": 6.00},
        "ministral-8b-latest": {"prompt": 0.10, "completion": 0.10},
        "ministral-3b-latest": {"prompt": 0.04, "completion": 0.04},
        "codestral-latest": {"prompt": 0.30, "completion": 0.90},
        "pixtral-large-latest": {"prompt": 2.00, "completion": 6.00},
        "pixtral-12b-2409": {"prompt": 0.15, "completion": 0.15},
        "mistral-embed": {"prompt": 0.10, "completion": 0.00},
    }

    def __init__(self):
        self.mistral_call_semaphore = threading.Semaphore(3)
        self.mistral_stream_semaphore = threading.Semaphore(STREAM_CHAT_MAX_CONCURRENT)
        self.mistral_cooldown_lock = threading.Lock()
        self.mistral_next_allowed_ts = 0.0
        self.mistral_429_streak = 0
        self.mistral_last_call_ts = 0.0
        self.mistral_response_cache: TTLCache[Any, Any] = TTLCache(maxsize=128, ttl=240)
        self.mistral_response_lock = threading.Lock()
        self.mistral_clients: LRUCache[Any, Any] = LRUCache(maxsize=128)
        self.mistral_clients_lock = threading.Lock()
        self._retired_mistral_clients: deque[tuple[float, Any]] = deque()

        # OpenAI-compatible HTTP client connection pool and concurrency limits
        self.openai_clients: LRUCache[tuple[str, str], Any] = LRUCache(maxsize=32)
        self.openai_clients_lock = threading.Lock()
        self._retired_openai_clients: deque[tuple[float, Any]] = deque()
        self.openai_call_semaphore = threading.Semaphore(3)
        self.openai_stream_semaphore = threading.Semaphore(STREAM_CHAT_MAX_CONCURRENT)

        # Cumulative token usage counters (Mistral)
        self.mistral_usage_lock = threading.Lock()
        self.mistral_call_count = 0
        self.mistral_total_prompt_tokens = 0
        self.mistral_total_completion_tokens = 0
        self.mistral_model_usage: dict[str, dict[str, int]] = {}

        # Cumulative token usage counters (OpenAI-compatible)
        self.openai_usage_lock = threading.Lock()
        self.openai_call_count = 0
        self.openai_total_prompt_tokens = 0
        self.openai_total_completion_tokens = 0
        self.openai_model_usage: dict[str, dict[str, int]] = {}

        self.langsearch_rate_lock = threading.Lock()
        self.langsearch_next_allowed_ts = 0.0
        self.langsearch_min_interval_sec = 2.0
        self.langsearch_429_cooldown_sec = 90.0

        self.trends_refresh_inflight: set[str] = set()
        self.trends_refresh_lock = threading.Lock()

        from utils.chat_history import SQLiteChatHistoryStore

        self.chat_history: SQLiteChatHistoryStore = SQLiteChatHistoryStore(max_sessions=50)
        self.chat_history_lock = threading.Lock()
        self.max_history = 50

    def record_ai_usage(self, usage: Any, model: str = "", provider: str = "mistral") -> None:
        """Accumulate token usage for the specified AI provider ('mistral' or 'openai_compatible')."""
        if not isinstance(usage, dict):
            return

        def _coerce_usage_int(value: Any) -> int:
            # M10 (2026-11-07 review): arbitrary OpenAI-compatible gateways
            # emit token counts as strings ("1,234") or floats ("12.5"); a
            # bare int() raised and turned an otherwise successful LLM
            # response into the error envelope. Coerce best-effort and
            # degrade defunct values to 0 instead of losing the answer.
            try:
                return int(float(str(value).replace(",", "")))
            except (TypeError, ValueError, OverflowError):
                return 0

        prompt_tokens = _coerce_usage_int(usage.get("prompt_tokens") or 0)
        completion_tokens = _coerce_usage_int(usage.get("completion_tokens") or 0)
        if prompt_tokens < 0 or completion_tokens < 0:
            return
        clean_model = (model or "unknown").strip().lower()

        if provider in ("openai_compatible", "openai"):
            with self.openai_usage_lock:
                self.openai_call_count += 1
                self.openai_total_prompt_tokens += prompt_tokens
                self.openai_total_completion_tokens += completion_tokens
                if clean_model and clean_model != "unknown":
                    if clean_model not in self.openai_model_usage:
                        self.openai_model_usage[clean_model] = {
                            "calls": 0,
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                        }
                    self.openai_model_usage[clean_model]["calls"] += 1
                    self.openai_model_usage[clean_model]["prompt_tokens"] += prompt_tokens
                    self.openai_model_usage[clean_model]["completion_tokens"] += completion_tokens
        else:
            with self.mistral_usage_lock:
                self.mistral_call_count += 1
                self.mistral_total_prompt_tokens += prompt_tokens
                self.mistral_total_completion_tokens += completion_tokens
                if clean_model and clean_model != "unknown":
                    if clean_model not in self.mistral_model_usage:
                        self.mistral_model_usage[clean_model] = {
                            "calls": 0,
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                        }
                    self.mistral_model_usage[clean_model]["calls"] += 1
                    self.mistral_model_usage[clean_model]["prompt_tokens"] += prompt_tokens
                    self.mistral_model_usage[clean_model]["completion_tokens"] += completion_tokens

    def record_mistral_usage(self, usage: Any, model: str = "") -> None:
        """Accumulate token usage from a successful Mistral response (backwards-compatible)."""
        self.record_ai_usage(usage, model=model, provider="mistral")

    def record_openai_usage(self, usage: Any, model: str = "") -> None:
        """Accumulate token usage from a successful OpenAI-compatible response."""
        self.record_ai_usage(usage, model=model, provider="openai_compatible")

    def mistral_usage_stats(self) -> dict[str, Any]:
        """Return a thread-safe snapshot of the cumulative usage counters and estimated costs for Mistral."""
        with self.mistral_usage_lock:
            total_cost_usd = 0.0
            by_model_out: dict[str, dict[str, Any]] = {}
            by_model_prompt = 0
            by_model_completion = 0
            by_model_calls = 0

            for m_name, m_data in self.mistral_model_usage.items():
                p_toks = m_data["prompt_tokens"]
                c_toks = m_data["completion_tokens"]
                pricing = self._MISTRAL_PRICING.get(m_name, {"prompt": 0.10, "completion": 0.30})
                cost = (p_toks / 1_000_000.0 * pricing["prompt"]) + (
                    c_toks / 1_000_000.0 * pricing["completion"]
                )
                total_cost_usd += cost
                by_model_prompt += p_toks
                by_model_completion += c_toks
                by_model_calls += m_data["calls"]
                by_model_out[m_name] = {
                    "calls": m_data["calls"],
                    "prompt_tokens": p_toks,
                    "completion_tokens": c_toks,
                    "total_tokens": p_toks + c_toks,
                    "estimated_cost_usd": round(cost, 6),
                }

            # M11 (2026-11-07 review): the totals accrue every call while
            # the by-model map only tracks calls with a usable model name, so
            # model-less entries (e.g. agents runs with an unnamed model)
            # previously vanished from the cost when ANY other model existed.
            # Price the residual tokens at the default rates so the reported
            # cost always matches the reported token totals.
            residual_prompt = self.mistral_total_prompt_tokens - by_model_prompt
            residual_completion = self.mistral_total_completion_tokens - by_model_completion
            residual_calls = self.mistral_call_count - by_model_calls
            if residual_prompt > 0 or residual_completion > 0 or residual_calls > 0:
                residual_cost = (max(0, residual_prompt) / 1_000_000.0 * 0.10) + (
                    max(0, residual_completion) / 1_000_000.0 * 0.30
                )
                total_cost_usd += residual_cost
                by_model_out["unknown"] = {
                    "calls": max(0, residual_calls),
                    "prompt_tokens": max(0, residual_prompt),
                    "completion_tokens": max(0, residual_completion),
                    "total_tokens": max(0, residual_prompt) + max(0, residual_completion),
                    "estimated_cost_usd": round(residual_cost, 6),
                }

            # M11: convert using the tracked USD/JPY rate instead of a hard
            # coded 155.0, so the yen figure matches the dashboard's rate.
            fx_rate = _tracked_usdjpy_rate() or DEFAULT_USDJPY_RATE
            cost_jpy = round(total_cost_usd * fx_rate, 2)

            return {
                "call_count": self.mistral_call_count,
                "prompt_tokens": self.mistral_total_prompt_tokens,
                "completion_tokens": self.mistral_total_completion_tokens,
                "total_tokens": (
                    self.mistral_total_prompt_tokens + self.mistral_total_completion_tokens
                ),
                "estimated_cost_usd": round(total_cost_usd, 6),
                "estimated_cost_jpy": cost_jpy,
                "by_model": by_model_out,
            }

    def openai_usage_stats(self) -> dict[str, Any]:
        """Return a thread-safe snapshot of the cumulative usage counters for OpenAI-compatible models."""
        with self.openai_usage_lock:
            by_model_out: dict[str, dict[str, Any]] = {}
            by_model_prompt = 0
            by_model_completion = 0
            by_model_calls = 0

            for m_name, m_data in self.openai_model_usage.items():
                p_toks = m_data["prompt_tokens"]
                c_toks = m_data["completion_tokens"]
                by_model_prompt += p_toks
                by_model_completion += c_toks
                by_model_calls += m_data["calls"]
                by_model_out[m_name] = {
                    "calls": m_data["calls"],
                    "prompt_tokens": p_toks,
                    "completion_tokens": c_toks,
                    "total_tokens": p_toks + c_toks,
                }

            residual_prompt = self.openai_total_prompt_tokens - by_model_prompt
            residual_completion = self.openai_total_completion_tokens - by_model_completion
            residual_calls = self.openai_call_count - by_model_calls
            if residual_prompt > 0 or residual_completion > 0 or residual_calls > 0:
                by_model_out["unknown"] = {
                    "calls": max(0, residual_calls),
                    "prompt_tokens": max(0, residual_prompt),
                    "completion_tokens": max(0, residual_completion),
                    "total_tokens": max(0, residual_prompt) + max(0, residual_completion),
                }

            return {
                "call_count": self.openai_call_count,
                "prompt_tokens": self.openai_total_prompt_tokens,
                "completion_tokens": self.openai_total_completion_tokens,
                "total_tokens": (
                    self.openai_total_prompt_tokens + self.openai_total_completion_tokens
                ),
                "by_model": by_model_out,
            }

    def ai_usage_stats(self) -> dict[str, Any]:
        """Return combined usage stats with provider separation."""
        return {
            "mistral": self.mistral_usage_stats(),
            "openai_compatible": self.openai_usage_stats(),
        }

    def add_chat_history(
        self, key: str, message: list[dict[str, Any]] | dict[str, Any]
    ) -> None:
        """Persist a chat turn under an explicit storage contract (H-2).

        A ``list`` replaces the session transcript; a single ``dict`` appends
        one message. Any other shape is a programming error and raises
        ``TypeError`` instead of falling through to an implicit write.
        """
        with self.chat_history_lock:
            if isinstance(message, list):
                self.chat_history[key] = message
            elif isinstance(message, dict):
                self.chat_history.add_message(key, message)
            else:
                raise TypeError(
                    "chat history message must be a list[dict] (replace) or "
                    f"dict (append), got {type(message).__name__}"
                )

    def mark_mistral_429(self, retry_after_sec=None) -> float:
        with self.mistral_cooldown_lock:
            self.mistral_429_streak = min(self.mistral_429_streak + 1, 6)
            exponential_backoff = min(2.0**self.mistral_429_streak, 120.0)
            try:
                retry_after = max(0.0, float(retry_after_sec or 0.0))
            except (TypeError, ValueError):
                retry_after = 0.0
            backoff = min(max(exponential_backoff, retry_after), 300.0)
            # Monotonic window (M1, 2026-11-07 review): a concurrent 429
            # reported with a smaller backoff must never SHRINK an active
            # server-requested cooldown. Every sibling cooldown in
            # market_state.py uses the same window-max idiom, and the rollback
            # in ai_service._wait_for_rate_limit_slot deliberately does too —
            # otherwise the calls resume at full rate well before the
            # Retry-After the server demanded.
            self.mistral_next_allowed_ts = max(self.mistral_next_allowed_ts, time.time() + backoff)
            return backoff

    def reset_mistral_streak(self):
        with self.mistral_cooldown_lock:
            self.mistral_429_streak = 0

    @staticmethod
    def _close_client_safely(client: Any, label: str) -> None:
        try:
            if hasattr(client, "close") and callable(client.close):
                client.close()
        except Exception as exc:
            logger.debug("Error closing %s client error_type=%s", label, type(exc).__name__)

    @classmethod
    def _prune_retired_clients_locked(
        cls,
        retired_queue: deque[tuple[float, Any]],
        max_retired: int,
        label: str,
    ) -> None:
        grace_sec = max(float(MISTRAL_API_TIMEOUT_SEC) * 2.0, 120.0)
        now = time.monotonic()
        while retired_queue:
            retired_ts, old_client = retired_queue[0]
            if getattr(old_client, "is_closed", False):
                retired_queue.popleft()
                continue
            if (now - retired_ts) >= grace_sec or len(retired_queue) > max_retired:
                retired_queue.popleft()
                cls._close_client_safely(old_client, label)
            else:
                break

    @classmethod
    def _lookup_or_evict_locked(
        cls,
        pool: LRUCache[Any, Any],
        retired_queue: deque[tuple[float, Any]],
        cache_key: Any,
        default_maxsize: int,
        label: str,
    ) -> Any | None:
        maxsize = max(int(getattr(pool, "maxsize", default_maxsize)), 1)
        cls._prune_retired_clients_locked(retired_queue, max_retired=max(maxsize * 2, 64), label=label)

        if cache_key in pool:
            client = pool[cache_key]
            if not getattr(client, "is_closed", False):
                return client
            # Evict the closed client for this key before checking pool capacity
            # so an unrelated live client is never evicted needlessly.
            pool.pop(cache_key, None)

        while len(pool) >= maxsize:
            try:
                _, old_client = pool.popitem()
                if not getattr(old_client, "is_closed", False):
                    # Defer closing an evicted live client so any concurrent
                    # caller that already borrowed it can finish cleanly.
                    retired_queue.append((time.monotonic(), old_client))
            except (KeyError, Exception) as exc:
                logger.debug(
                    "Error retiring evicted %s client error_type=%s",
                    label,
                    type(exc).__name__,
                )
        return None

    def get_or_create_mistral_client(self, api_key: str):
        cache_key = api_key
        with self.mistral_clients_lock:
            existing = self._lookup_or_evict_locked(
                self.mistral_clients,
                self._retired_mistral_clients,
                cache_key,
                default_maxsize=128,
                label="Mistral",
            )
            if existing is not None:
                return existing

            import mistral_compat

            client = mistral_compat.Mistral(
                api_key=api_key,
                timeout_ms=int(MISTRAL_API_TIMEOUT_SEC * 1000),
                server_url=MISTRAL_BASE_URL,
            )
            self.mistral_clients[cache_key] = client
            return client

    def get_or_create_openai_client(self, base_url: str, api_key: str | None = None) -> Any:
        """Retrieve or create an httpx.Client configured with connection pooling for OpenAI endpoints."""
        clean_url = (base_url or "").strip().rstrip("/")
        from utils.networking import validate_outbound_base_url

        url_ok, url_reason = validate_outbound_base_url(clean_url)
        if not url_ok:
            raise ValueError(url_reason)

        clean_key = (api_key or "").strip()
        cache_key = (clean_url, clean_key)

        with self.openai_clients_lock:
            existing = self._lookup_or_evict_locked(
                self.openai_clients,
                self._retired_openai_clients,
                cache_key,
                default_maxsize=32,
                label="OpenAI",
            )
            if existing is not None:
                return existing

            verify_ctx: Any = True
            try:
                ctx = ssl.create_default_context()
                ctx.load_default_certs()
                verify_ctx = ctx
            except Exception as exc:
                logger.debug("Failed configuring OS trust store certs for OpenAI: %s", exc)

            from utils.networking import create_pinned_outbound_httpx_transport

            limits = httpx.Limits(
                max_keepalive_connections=20,
                max_connections=50,
                keepalive_expiry=60.0,
            )
            transport = create_pinned_outbound_httpx_transport(
                verify=verify_ctx,
                limits=limits,
            )
            try:
                client = httpx.Client(
                    verify=verify_ctx,
                    limits=limits,
                    timeout=MISTRAL_API_TIMEOUT_SEC,
                    follow_redirects=False,
                    trust_env=False,
                    transport=transport,
                )
            except Exception:
                transport.close()
                raise
            self.openai_clients[cache_key] = client
            return client

    def close_all_clients(self) -> None:
        """Close all persistent client sessions across all providers (called during shutdown)."""
        with self.mistral_clients_lock:
            while self.mistral_clients:
                _, client = self.mistral_clients.popitem()
                self._close_client_safely(client, "Mistral shutdown")
            while self._retired_mistral_clients:
                _, client = self._retired_mistral_clients.popleft()
                self._close_client_safely(client, "Mistral retired shutdown")

        with self.openai_clients_lock:
            while self.openai_clients:
                _, client = self.openai_clients.popitem()
                self._close_client_safely(client, "OpenAI shutdown")
            while self._retired_openai_clients:
                _, client = self._retired_openai_clients.popleft()
                self._close_client_safely(client, "OpenAI retired shutdown")

    def response_cache_size(self) -> int:
        """Return the current number of cached Mistral responses under lock."""
        with self.mistral_response_lock:
            return len(self.mistral_response_cache)

    def clients_cached_count(self) -> int:
        """Return the current number of cached Mistral clients under lock."""
        with self.mistral_clients_lock:
            return len(self.mistral_clients)
