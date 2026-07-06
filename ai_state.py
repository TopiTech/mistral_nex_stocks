"""
ai_state.py - AI service state management (Mistral, LangSearch, chat history).

Extracted from app_state.py to reduce module complexity.
"""

import logging
import threading
import time
from collections import OrderedDict
from typing import Any

from cachetools import LRUCache, TTLCache
from mistral_compat import Mistral
from config_utils import _env_float

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

        self.chat_history: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        self.chat_history_lock = threading.Lock()
        self.max_history = 50

    def add_chat_history(self, key: str, message: Any):
        with self.chat_history_lock:
            if key not in self.chat_history:
                if len(self.chat_history) >= self.max_history:
                    self.chat_history.popitem(last=False)
            self.chat_history[key] = message
            self.chat_history.move_to_end(key)

    def mark_mistral_429(self, retry_after_sec=None) -> float:
        with self.mistral_cooldown_lock:
            self.mistral_429_streak = min(self.mistral_429_streak + 1, 6)
            exponential_backoff = min(2.0 ** self.mistral_429_streak, 120.0)
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
        thread_id = threading.get_ident()
        cache_key = (api_key, thread_id)
        with self.mistral_clients_lock:
            if cache_key in self.mistral_clients:
                return self.mistral_clients[cache_key]

            timeout_sec = _env_float("MNS_MISTRAL_API_TIMEOUT", 45.0, 5.0, 180.0)
            client = Mistral(api_key=api_key, timeout_ms=int(timeout_sec * 1000))
            self.mistral_clients[cache_key] = client
            return client
