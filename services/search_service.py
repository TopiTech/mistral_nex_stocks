# services/search_service.py
"""Centralized search engine coordinator and provider wrapper."""

import logging
import time
from typing import Any, Optional, List, Tuple

import requests
from requests.exceptions import RequestException

import trend_sources as ts
from app_helpers import _get_cached_value, _set_cached_value
from app_state import app_state
from config_utils import _env_int
from constants import LANGSEARCH_TIMEOUT

# Sub-provider split exports (explicitly import private helpers to maintain test backward compatibility)
from services.search.ddgs import (
    ddgs_news_search,
    ddgs_text_search,
    _format_ddgs_news_items,
    _format_ddgs_text_items,
    _collect_ddgs_items,
    _market_ddgs_queries,
    _symbol_ddgs_queries,
    _get_ddgs_timeout,
    MAX_DDGS_QUERY_LEN,
)
from services.search.tavily import (
    tavily_search,
    _format_tavily_items,
    _collect_tavily_items,
    _get_tavily_client,
)
from services.search.langsearch import (
    langsearch_search,
    langsearch_rerank,
    _collect_langsearch_items,
    _request_json_post,
    _langsearch_request_retryable,
    _langsearch_acquire_slot,
    _langsearch_mark_retry_after_429,
    _langsearch_post_json,
    _summarize_http_error,
    _extract_langsearch_entries,
    _format_langsearch_items,
    _map_langsearch_freshness,
)

logger = logging.getLogger(__name__)


def _dedupe_items(items):
    return ts.dedupe_items(items)


def _determine_search_strategy(tavily_api_key="", langsearch_api_key=""):
    """Determine which search strategy to use based on available API keys."""
    if langsearch_api_key:
        return "langsearch"
    if tavily_api_key:
        return "ddgs_tavily"
    return "ddgs_only"


def _collect_hybrid_items(
    queries, region, timelimit, news_n, text_n, tavily_api_key, limit=10, query_limit=3, tavily_topic="news"
):
    """Hybrid search: DDGS primary, supplement with Tavily when DDGS results are sparse."""
    ddgs_items = _collect_ddgs_items(
        queries, region, timelimit, news_n, text_n, limit=limit, query_limit=query_limit
    )

    if len(ddgs_items) >= limit:
        logger.info(
            "DDGS provided sufficient results (%d/%d), skipping Tavily",
            len(ddgs_items),
            limit,
        )
        return ddgs_items[:limit]

    if tavily_api_key and len(ddgs_items) < limit:
        tavily_needed = limit - len(ddgs_items)
        logger.info(
            "DDGS results sparse (%d/%d), supplementing with Tavily (need %d more)",
            len(ddgs_items),
            limit,
            tavily_needed,
        )
        try:
            tavily_items = _collect_tavily_items(
                queries,
                api_key=tavily_api_key,
                timelimit=timelimit,
                max_results=max(tavily_needed, 3),
                limit=limit,
                query_limit=query_limit,
                topic=tavily_topic,
            )
            merged = _dedupe_items(list(ddgs_items) + list(tavily_items))
            logger.info(
                "Hybrid search: DDGS=%d Tavily=%d merged=%d",
                len(ddgs_items),
                len(tavily_items),
                len(merged),
            )
            return merged[:limit]
        except Exception as exc:
            logger.warning("Tavily supplement failed, using DDGS only: %s", exc)
            return ddgs_items[:limit]

    return ddgs_items[:limit]


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


def collect_market_news_context(market="us", langsearch_api_key="", tavily_api_key=""):
    """Fetches and merges market-wide context from multiple sources."""
    region, queries = _market_ddgs_queries(market)
    ts_items = ts.collect_market_news_items_fast(market)

    strategy = _determine_search_strategy(tavily_api_key, langsearch_api_key)

    if strategy == "langsearch":
        search_items = _collect_langsearch_items(
            queries,
            api_key=langsearch_api_key,
            timelimit="d",
            max_results=2,
            limit=6,
            query_limit=2,
        )
        if search_items:
            logger.info(
                "LangSearch used: context=market_news market=%s items=%s",
                market,
                len(search_items),
            )
        else:
            reason = "missing_api_key" if not langsearch_api_key else "empty_or_error"
            logger.info(
                "DDGS fallback used: context=market_news market=%s reason=%s",
                market,
                reason,
            )
            search_items = _collect_ddgs_items(
                queries, region, "d", news_n=1, text_n=1, limit=6, query_limit=2
            )
            logger.info(
                "DDGS results: context=market_news market=%s items=%s",
                market,
                len(search_items),
            )
    elif strategy == "ddgs_tavily":
        logger.info(
            "Hybrid DDGS+Tavily used: context=market_news market=%s",
            market,
        )
        search_items = _collect_hybrid_items(
            queries, region, "d",
            news_n=2, text_n=1,
            tavily_api_key=tavily_api_key,
            limit=6, query_limit=2,
            tavily_topic="news",
        )
        logger.info(
            "Hybrid results: context=market_news market=%s items=%s",
            market,
            len(search_items),
        )
    else:
        logger.info(
            "DDGS only: context=market_news market=%s",
            market,
        )
        search_items = _collect_ddgs_items(
            queries, region, "d", news_n=1, text_n=1, limit=6, query_limit=2
        )
        logger.info(
            "DDGS results: context=market_news market=%s items=%s",
            market,
            len(search_items),
        )

    merged = ts.dedupe_items(list(ts_items) + list(search_items))
    return _compact_small_model_context(merged, limit=6, max_chars=1400)


def collect_symbol_research_context(symbol, name, market="us", langsearch_api_key="", tavily_api_key=""):
    """Collects deep research context for a specific stock ticker."""
    region, queries = _symbol_ddgs_queries(symbol, name, market)
    ts_items = ts.collect_symbol_research_items(symbol, name, market)

    strategy = _determine_search_strategy(tavily_api_key, langsearch_api_key)

    if strategy == "langsearch":
        search_items = _collect_langsearch_items(
            queries,
            api_key=langsearch_api_key,
            timelimit="m",
            max_results=3,
            limit=8,
            query_limit=3,
        )
        if search_items:
            logger.info(
                "LangSearch used: context=symbol_research market=%s symbol=%s items=%s",
                market,
                symbol,
                len(search_items),
            )
        else:
            reason = "missing_api_key" if not langsearch_api_key else "empty_or_error"
            logger.info(
                "DDGS fallback used: context=symbol_research market=%s symbol=%s reason=%s",
                market,
                symbol,
                reason,
            )
            search_items = _collect_ddgs_items(
                queries, region, "m", news_n=2, text_n=1, limit=8
            )
            logger.info(
                "DDGS results: context=symbol_research market=%s symbol=%s items=%s",
                market,
                symbol,
                len(search_items),
            )
    elif strategy == "ddgs_tavily":
        logger.info(
            "Hybrid DDGS+Tavily used: context=symbol_research market=%s symbol=%s",
            market,
            symbol,
        )
        search_items = _collect_hybrid_items(
            queries, region, "m",
            news_n=3, text_n=1,
            tavily_api_key=tavily_api_key,
            limit=8, query_limit=3,
            tavily_topic="general",
        )
        logger.info(
            "Hybrid results: context=symbol_research market=%s symbol=%s items=%s",
            market,
            symbol,
            len(search_items),
        )
    else:
        logger.info(
            "DDGS only: context=symbol_research market=%s symbol=%s",
            market,
            symbol,
        )
        search_items = _collect_ddgs_items(
            queries, region, "m", news_n=2, text_n=1, limit=8
        )
        logger.info(
            "DDGS results: context=symbol_research market=%s symbol=%s items=%s",
            market,
            symbol,
            len(search_items),
        )

    merged = ts.dedupe_items(list(ts_items) + list(search_items))
    return _compact_small_model_context(merged, limit=8, max_chars=2200)


def collect_market_trending_titles(market="us", count=10, langsearch_api_key="", tavily_api_key=""):
    """Retrieve trending market titles for UI display."""
    capped = min(count, 15)
    strategy = _determine_search_strategy(tavily_api_key, langsearch_api_key)
    return _get_market_trending_titles(market, strategy, langsearch_api_key, tavily_api_key)[
        :capped
    ]


def _market_trends_cache_key(market: str, strategy: str) -> str:
    return f"market_trends_{market}_{strategy}"


def _build_market_trending_titles(market: str, langsearch_api_key: str, tavily_api_key: str = "") -> list[str]:
    try:
        trend_target = 12
        region, queries = _market_ddgs_queries(market)
        ts_titles = ts.collect_market_trending_titles(market, count=trend_target)

        strategy = _determine_search_strategy(tavily_api_key, langsearch_api_key)

        if strategy == "langsearch":
            search_items = _collect_langsearch_items(
                queries,
                api_key=langsearch_api_key,
                timelimit="d",
                max_results=4,
                limit=12,
                query_limit=4,
            )
            if search_items:
                logger.info(
                    "LangSearch used: context=market_trending market=%s items=%s",
                    market,
                    len(search_items),
                )
            else:
                reason = "missing_api_key" if not langsearch_api_key else "empty_or_error"
                logger.info(
                    "DDGS fallback used: context=market_trending market=%s reason=%s",
                    market,
                    reason,
                )
                search_items = _collect_ddgs_items(
                    queries, region, "d", news_n=3, text_n=2, limit=12, query_limit=4
                )
                logger.info(
                    "DDGS results: context=market_trending market=%s items=%s",
                    market,
                    len(search_items),
                )
        elif strategy == "ddgs_tavily":
            logger.info(
                "Hybrid DDGS+Tavily used: context=market_trending market=%s",
                market,
            )
            search_items = _collect_hybrid_items(
                queries, region, "d",
                news_n=3, text_n=2,
                tavily_api_key=tavily_api_key,
                limit=12, query_limit=4,
                tavily_topic="news",
            )
            logger.info(
                "Hybrid results: context=market_trending market=%s items=%s",
                market,
                len(search_items),
            )
        else:
            logger.info(
                "DDGS only: context=market_trending market=%s",
                market,
            )
            search_items = _collect_ddgs_items(
                queries, region, "d", news_n=3, text_n=2, limit=12, query_limit=4
            )
            logger.info(
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
            t = (title or "").strip()
            key = t.lower()
            if not t or key in seen:
                continue
            seen.add(key)
            merged_titles.append(t)
            if len(merged_titles) >= trend_target:
                break
        return merged_titles
    except Exception as exc:
        logger.error("Trend building error: %s", exc)
        return []


def _schedule_market_trends_refresh_async(
    market: str, strategy: str, langsearch_api_key: str, tavily_api_key: str = ""
) -> bool:
    cache_key = _market_trends_cache_key(market, strategy)

    with app_state.trends_refresh_lock:
        if cache_key in app_state.trends_refresh_inflight:
            return False
        app_state.trends_refresh_inflight.add(cache_key)

    def _job():
        try:
            trend_titles = _build_market_trending_titles(market, langsearch_api_key, tavily_api_key)
            _set_cached_value(cache_key, trend_titles, duration=300)
            logger.info(
                "News trends async refresh completed: market=%s source=%s cache_key=%s items=%s",
                market,
                strategy,
                cache_key,
                len(trend_titles),
            )
        except (RuntimeError, RequestException, ValueError) as exc:
            logger.warning(
                "News trends async refresh failed: market=%s source=%s error=%s",
                market,
                strategy,
                exc,
            )
        finally:
            with app_state.trends_refresh_lock:
                app_state.trends_refresh_inflight.discard(cache_key)

    app_state.executor.submit(_job)
    return True


def _get_market_trending_titles(
    market: str, strategy: str, langsearch_api_key: str, tavily_api_key: str = ""
) -> list[str]:
    cache_key = _market_trends_cache_key(market, strategy)
    cached = _get_cached_value(cache_key, duration=300, default=None)

    if isinstance(cached, list) and cached:
        return cached
    if isinstance(cached, str) and cached.strip():
        return [t.strip() for t in cached.split("、") if t.strip()]

    logger.info(
        "Market trending cache miss, building synchronously: market=%s strategy=%s",
        market,
        strategy,
    )
    trend_titles = _build_market_trending_titles(market, langsearch_api_key, tavily_api_key)
    if trend_titles:
        _set_cached_value(cache_key, trend_titles, duration=300)
        return trend_titles

    started = _schedule_market_trends_refresh_async(
        market, strategy, langsearch_api_key, tavily_api_key
    )
    logger.info(
        "Market trending refresh %s after cache miss: market=%s strategy=%s",
        "started" if started else "already-running",
        market,
        strategy,
    )
    return []
