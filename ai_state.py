"""
ai_state.py - AI service state management (Mistral, LangSearch, chat history).

Extracted from app_state.py to reduce module complexity.
"""

import logging
import threading
import time
from typing import Any, ClassVar

from cachetools import LRUCache, TTLCache

from constants import (
    MISTRAL_API_TIMEOUT_SEC,
    MISTRAL_BASE_URL,
    STREAM_CHAT_MAX_CONCURRENT,
)

logger = logging.getLogger("backend")


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

        # Cumulative token usage counters (C-4): updated on every successful
        # Mistral call so operators can track cost without scraping logs.
        self.mistral_usage_lock = threading.Lock()
        self.mistral_call_count = 0
        self.mistral_total_prompt_tokens = 0
        self.mistral_total_completion_tokens = 0
        self.mistral_model_usage: dict[str, dict[str, int]] = {}

        self.langsearch_rate_lock = threading.Lock()
        self.langsearch_next_allowed_ts = 0.0
        self.langsearch_min_interval_sec = 2.0
        self.langsearch_429_cooldown_sec = 90.0

        self.trends_refresh_inflight: set[str] = set()
        self.trends_refresh_lock = threading.Lock()

        from utils.chat_history import SQLiteChatHistoryStore

        self.chat_history: Any = SQLiteChatHistoryStore(max_sessions=50)
        self.chat_history_lock = threading.Lock()
        self.max_history = 50

    def record_mistral_usage(self, usage: Any, model: str = "") -> None:
        """Accumulate token usage from a successful Mistral response."""
        if not isinstance(usage, dict):
            return
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        if prompt_tokens < 0 or completion_tokens < 0:
            return
        clean_model = (model or "unknown").strip().lower()
        with self.mistral_usage_lock:
            self.mistral_call_count += 1
            self.mistral_total_prompt_tokens += prompt_tokens
            self.mistral_total_completion_tokens += completion_tokens

            if clean_model:
                if clean_model not in self.mistral_model_usage:
                    self.mistral_model_usage[clean_model] = {
                        "calls": 0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                    }
                self.mistral_model_usage[clean_model]["calls"] += 1
                self.mistral_model_usage[clean_model]["prompt_tokens"] += prompt_tokens
                self.mistral_model_usage[clean_model]["completion_tokens"] += completion_tokens

    def mistral_usage_stats(self) -> dict[str, Any]:
        """Return a thread-safe snapshot of the cumulative usage counters and estimated costs."""
        with self.mistral_usage_lock:
            total_cost_usd = 0.0
            by_model_out: dict[str, dict[str, Any]] = {}

            for m_name, m_data in self.mistral_model_usage.items():
                p_toks = m_data["prompt_tokens"]
                c_toks = m_data["completion_tokens"]
                pricing = self._MISTRAL_PRICING.get(m_name, {"prompt": 0.10, "completion": 0.30})
                cost = (p_toks / 1_000_000.0 * pricing["prompt"]) + (
                    c_toks / 1_000_000.0 * pricing["completion"]
                )
                total_cost_usd += cost
                by_model_out[m_name] = {
                    "calls": m_data["calls"],
                    "prompt_tokens": p_toks,
                    "completion_tokens": c_toks,
                    "total_tokens": p_toks + c_toks,
                    "estimated_cost_usd": round(cost, 6),
                }

            # Fallback cost estimate for untracked legacy tokens if any
            if not self.mistral_model_usage and (
                self.mistral_total_prompt_tokens or self.mistral_total_completion_tokens
            ):
                total_cost_usd = (self.mistral_total_prompt_tokens / 1_000_000.0 * 0.10) + (
                    self.mistral_total_completion_tokens / 1_000_000.0 * 0.30
                )

            # JPY conversion rate reference (~155 JPY/USD)
            cost_jpy = round(total_cost_usd * 155.0, 2)

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

    def add_chat_history(self, key: str, message: Any):
        with self.chat_history_lock:
            if isinstance(message, list):
                self.chat_history[key] = message
            elif isinstance(message, dict) and hasattr(self.chat_history, "add_message"):
                self.chat_history.add_message(key, message)
            else:
                self.chat_history[key] = message

    def mark_mistral_429(self, retry_after_sec=None) -> float:
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
        with self.mistral_cooldown_lock:
            self.mistral_429_streak = 0
            # mistral_next_allowed_ts は意図的にリセットしない。並行する別スレッドが
            # 設定した 429 バックオフを、他スレッドの成功1回で解除すると
            # 429 連打・追加制限を招く（R3）。クールダウンは自然に期限切れさせる。

    def get_or_create_mistral_client(self, api_key: str):
        # M-2: Use api_key only (not thread_id) as the cache key.
        # Mistral SDK client is thread-safe, so sharing a single client
        # across threads avoids unnecessary client creation and memory
        # accumulation from short-lived threads.
        cache_key = api_key
        with self.mistral_clients_lock:
            if cache_key in self.mistral_clients:
                return self.mistral_clients[cache_key]

            # If cache is full, pop the LRU item and close its client session.
            # ``popitem()`` pops the least-recently-used entry by default.
            if len(self.mistral_clients) >= getattr(self.mistral_clients, "maxsize", 128):
                try:
                    _, old_client = self.mistral_clients.popitem()
                    if hasattr(old_client, "close"):
                        old_client.close()
                except (KeyError, Exception) as exc:
                    logger.debug(
                        "Error closing evicted Mistral client error_type=%s", type(exc).__name__
                    )

            import mistral_compat

            client = mistral_compat.Mistral(
                api_key=api_key,
                timeout_ms=int(MISTRAL_API_TIMEOUT_SEC * 1000),
                server_url=MISTRAL_BASE_URL,
            )
            self.mistral_clients[cache_key] = client
            return client

    def response_cache_size(self) -> int:
        """Return the current number of cached Mistral responses under lock."""
        with self.mistral_response_lock:
            return len(self.mistral_response_cache)

    def clients_cached_count(self) -> int:
        """Return the current number of cached Mistral clients under lock."""
        with self.mistral_clients_lock:
            return len(self.mistral_clients)
