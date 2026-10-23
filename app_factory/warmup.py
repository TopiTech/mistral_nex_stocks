"""ニュースウォームアップ。`app` からの純粋な移動。

APIキー取得はテストで `patch(\"app.get_langsearch_api_key\")` 等されるため、
呼び出し時に `app` 属性として解決する。
"""

from __future__ import annotations

import json
import logging
import queue
from typing import Any

from requests.exceptions import RequestException

import app
from app_state import KeyringError, app_state
from constants import CACHE_DURATION_NEWS, NEGATIVE_CACHE_TTL
from services.search_service import (
    _determine_search_strategy,
    collect_market_news_context,
    collect_market_trending_titles,
)
from utils.caching import get_cached_context_with_negative_cache

logger = logging.getLogger("app")


def schedule_news_warmup():
    """Warm up news/trends caches in background."""
    try:
        langsearch_api_key = app.get_langsearch_api_key() or ""
        tavily_api_key = app.get_tavily_api_key() or ""
    except (KeyringError, RuntimeError, ValueError):
        langsearch_api_key = ""
        tavily_api_key = ""

    strategy = _determine_search_strategy(tavily_api_key, langsearch_api_key)

    def _job():
        results: dict[str, Any] = {}
        # 各ウォームアップステップを個別にtry/exceptし、1つの失敗が他に影響しないようにする
        try:
            results["us_context"] = get_cached_context_with_negative_cache(
                f"market_news_context_us_{strategy}",
                lambda: collect_market_news_context(
                    "us",
                    langsearch_api_key=langsearch_api_key,
                    tavily_api_key=tavily_api_key,
                ),
                CACHE_DURATION_NEWS,
                NEGATIVE_CACHE_TTL,
                True,
            )
        except (OSError, RuntimeError, RequestException, ValueError, json.JSONDecodeError) as exc:
            logger.warning("News warmup (us context) failed: %s", exc)
            results["us_context"] = None

        try:
            results["jp_context"] = get_cached_context_with_negative_cache(
                f"market_news_context_jp_{strategy}",
                lambda: collect_market_news_context(
                    "jp",
                    langsearch_api_key=langsearch_api_key,
                    tavily_api_key=tavily_api_key,
                ),
                CACHE_DURATION_NEWS,
                NEGATIVE_CACHE_TTL,
                True,
            )
        except (OSError, RuntimeError, RequestException, ValueError, json.JSONDecodeError) as exc:
            logger.warning("News warmup (jp context) failed: %s", exc)
            results["jp_context"] = None

        try:
            results["us_trends"] = collect_market_trending_titles(
                "us", 8, langsearch_api_key, tavily_api_key
            )
        except (OSError, RuntimeError, RequestException, ValueError) as exc:
            logger.warning("News warmup (us trends) failed: %s", exc)
            results["us_trends"] = None

        try:
            results["jp_trends"] = collect_market_trending_titles(
                "jp", 8, langsearch_api_key, tavily_api_key
            )
        except (OSError, RuntimeError, RequestException, ValueError) as exc:
            logger.warning("News warmup (jp trends) failed: %s", exc)
            results["jp_trends"] = None

        success_count = sum(1 for v in results.values() if v is not None)
        total_count = len(results)
        if success_count < total_count:
            logger.info(
                "News warmup completed: %d/%d steps successful (partial)",
                success_count,
                total_count,
            )

    try:
        app_state.execution.news_executor.submit(_job)
    except (RuntimeError, AttributeError, ValueError, queue.Full) as exc:
        logger.warning("Failed to schedule news warmup: %s", exc)
