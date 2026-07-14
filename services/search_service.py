# services/search_service.py
"""Centralized search engine coordinator and provider wrapper."""

import logging
from typing import Any

from requests.exceptions import RequestException

import trend_sources as ts
from app_helpers import _get_cached_value, _set_cached_value
from app_state import app_state

# Sub-provider split exports (explicitly import private helpers to maintain test backward compatibility)
from services.search.ddgs import (
    _collect_ddgs_items,
    _market_ddgs_queries,
    _symbol_ddgs_queries,
    _get_ddgs_timeout,  # noqa: F401
    MAX_DDGS_QUERY_LEN,  # noqa: F401
    ddgs_news_search,  # noqa: F401
    ddgs_text_search,  # noqa: F401
    _format_ddgs_news_items,  # noqa: F401
    _format_ddgs_text_items,  # noqa: F401
)
from services.search.tavily import (
    _collect_tavily_items,
)
from services.search.langsearch import (
    _collect_langsearch_items,
    _langsearch_post_json,  # noqa: F401
    langsearch_rerank,  # noqa: F401
    langsearch_search,  # noqa: F401
    _format_langsearch_items,  # noqa: F401
    _extract_langsearch_entries,  # noqa: F401
    _map_langsearch_freshness,  # noqa: F401
    _langsearch_request_retryable,  # noqa: F401
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


def _execute_search_strategy(
    strategy: str,
    queries: list,
    region: str,
    timelimit: str,
    news_n: int,
    text_n: int,
    langsearch_api_key: str = "",
    tavily_api_key: str = "",
    limit: int = 6,
    query_limit: int = 2,
    tavily_topic: str = "news",
    context_label: str = "",
    errors_out: list[Any] | None = None,
) -> list[Any]:
    """Unified search strategy execution.

    Eliminates the 3x copy-paste of langsearch/ddgs_tavily/ddgs_only branching
    across collect_market_news_context, collect_symbol_research_context,
    and _build_market_trending_titles.

    Returns a deduplicated list of search result items.
    """
    if strategy == "langsearch":
        ls_items: list[Any] = _collect_langsearch_items(
            queries,
            api_key=langsearch_api_key,
            timelimit=timelimit,
            max_results=max(news_n + text_n, 3),
            limit=limit,
            query_limit=query_limit,
            errors_out=errors_out,
        )
        if ls_items:
            logger.info(
                "LangSearch used: context=%s items=%s",
                context_label,
                len(ls_items),
            )
        else:
            reason = "missing_api_key" if not langsearch_api_key else "empty_or_error"
            logger.info(
                "DDGS fallback used: context=%s reason=%s",
                context_label,
                reason,
            )
            ls_items = _collect_ddgs_items(
                queries, region, timelimit, news_n=news_n, text_n=text_n,
                limit=limit, query_limit=query_limit
            )
            logger.info(
                "DDGS results: context=%s items=%s",
                context_label,
                len(ls_items),
            )
        return ls_items

    if strategy == "ddgs_tavily":
        logger.info(
            "Hybrid DDGS+Tavily used: context=%s",
            context_label,
        )
        hybrid_items: list[Any] = _collect_hybrid_items(
            queries, region, timelimit,
            news_n=news_n, text_n=text_n,
            tavily_api_key=tavily_api_key,
            limit=limit, query_limit=query_limit,
            tavily_topic=tavily_topic,
            errors_out=errors_out,
        )
        logger.info(
            "Hybrid results: context=%s items=%s",
            context_label,
            len(hybrid_items),
        )
        return hybrid_items

    # ddgs_only
    logger.info(
        "DDGS only: context=%s",
        context_label,
    )
    ddgs_items: list[Any] = _collect_ddgs_items(
        queries, region, timelimit, news_n=news_n, text_n=text_n,
        limit=limit, query_limit=query_limit
    )
    logger.info(
        "DDGS results: context=%s items=%s",
        context_label,
        len(ddgs_items),
    )
    return ddgs_items


def _collect_hybrid_items(
    queries, region, timelimit, news_n, text_n, tavily_api_key, limit=10, query_limit=3, tavily_topic="news", errors_out: list[Any] | None = None
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
                errors_out=errors_out,
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
    search_items = _execute_search_strategy(
        strategy, queries, region, timelimit="d",
        news_n=2, text_n=1,
        langsearch_api_key=langsearch_api_key,
        tavily_api_key=tavily_api_key,
        limit=6, query_limit=2,
        tavily_topic="news",
        context_label=f"market_news market={market}",
    )

    # search_items is already deduplicated by _execute_search_strategy;
    # ts_items is already deduplicated by ts.collect_market_news_items_fast.
    merged = ts.dedupe_items(list(ts_items) + list(search_items))
    return _compact_small_model_context(merged, limit=6, max_chars=1400)


def collect_symbol_research_context(symbol, name, market="us", langsearch_api_key="", tavily_api_key="", errors_out: list[Any] | None = None):
    """Collects deep research context for a specific stock ticker."""
    region, queries = _symbol_ddgs_queries(symbol, name, market)
    ts_items = ts.collect_symbol_research_items(symbol, name, market)

    strategy = _determine_search_strategy(tavily_api_key, langsearch_api_key)
    search_items = _execute_search_strategy(
        strategy, queries, region, timelimit="m",
        news_n=3, text_n=1,
        langsearch_api_key=langsearch_api_key,
        tavily_api_key=tavily_api_key,
        limit=8, query_limit=3,
        tavily_topic="general",
        context_label=f"symbol_research market={market} symbol={symbol}",
        errors_out=errors_out,
    )

    # search_items is already deduplicated by _execute_search_strategy;
    # ts_items is already deduplicated by ts.collect_symbol_research_items.
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
        search_items = _execute_search_strategy(
            strategy, queries, region, timelimit="d",
            news_n=3, text_n=2,
            langsearch_api_key=langsearch_api_key,
            tavily_api_key=tavily_api_key,
            limit=12, query_limit=4,
            tavily_topic="news",
            context_label=f"market_trending market={market}",
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

    with app_state.ai.trends_refresh_lock:
        if cache_key in app_state.ai.trends_refresh_inflight:
            return False
        app_state.ai.trends_refresh_inflight.add(cache_key)

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
            with app_state.ai.trends_refresh_lock:
                app_state.ai.trends_refresh_inflight.discard(cache_key)

    app_state.execution.executor.submit(_job)
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
