"""
ai_state.py - AI service state management (Mistral, LangSearch, chat history).

Extracted from app_state.py to reduce module complexity.
"""

import logging
import threading
import time
from typing import Any

from cachetools import LRUCache, TTLCache
from mistral_compat import Mistral
from constants import MISTRAL_API_TIMEOUT_SEC

logger = logging.getLogger("backend")


class AIState:
    """Manages Mistral, LangSearch, and chat history state."""

    def __init__(self):
        self.mistral_call_semaphore = threading.Semaphore(3)
        self.mistral_cooldown_lock = threading.Lock()
        self.mistral_next_allowed_ts = 0.0
        self.mistral_429_streak = 0
        self.mistral_last_call_ts = 0.0
        self.mistral_response_cache: TTLCache[Any, Any] = TTLCache(maxsize=128, ttl=240)
        self.mistral_response_lock = threading.Lock()
        self.mistral_clients: LRUCache[Any, Any] = LRUCache(maxsize=128)
        self.mistral_clients_lock = threading.Lock()

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

    def add_chat_history(self, key: str, message: Any):
        with self.chat_history_lock:
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
            self.mistral_next_allowed_ts = 0.0

    def get_or_create_mistral_client(self, api_key: str):
        # M-2: Use api_key only (not thread_id) as the cache key.
        # Mistral SDK client is thread-safe, so sharing a single client
        # across threads avoids unnecessary client creation and memory
        # accumulation from short-lived threads.
        cache_key = api_key
        with self.mistral_clients_lock:
            if cache_key in self.mistral_clients:
                return self.mistral_clients[cache_key]

            client = Mistral(api_key=api_key, timeout_ms=int(MISTRAL_API_TIMEOUT_SEC * 1000))
            self.mistral_clients[cache_key] = client
            return client
