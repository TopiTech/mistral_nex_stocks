import logging
from typing import Any
import trend_sources as ts

logger = logging.getLogger(__name__)


def _get_tavily_client(api_key: str):
    """Lazy-create a TavilyClient. Raises ImportError if tavily is not installed."""
    from tavily import TavilyClient
    return TavilyClient(api_key=api_key)


def tavily_search(
    query,
    api_key,
    max_results=8,
    timelimit="d",
    topic="news",
    errors_out=None,
):
    """Performs a web search via Tavily API."""
    normalized_query = " ".join(str(query or "").split())
    if not normalized_query:
        return []
    if not api_key:
        raise ValueError("Tavily API key is required")

    time_range_map = {
        "d": "day",
        "w": "week",
        "m": "month",
        "y": "year",
    }
    time_range = time_range_map.get(str(timelimit).lower())

    try:
        client = _get_tavily_client(api_key)
        kwargs = {
            "query": normalized_query,
            "search_depth": "advanced" if max_results > 5 else "basic",
            "topic": topic,
            "max_results": min(max(1, int(max_results or 8)), 20),
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }
        if time_range:
            kwargs["time_range"] = time_range

        response = client.search(**kwargs)
        results = response.get("results", []) if isinstance(response, dict) else []
        return results if isinstance(results, list) else []
    except ImportError as exc:
        logger.error("Tavily package not installed: %s", exc)
        if isinstance(errors_out, list):
            errors_out.append(exc)
        return []
    except Exception as exc:
        logger.warning("Tavily search failed (%s): %s", normalized_query, exc)
        if isinstance(errors_out, list):
            errors_out.append(exc)
        return []


def _format_tavily_items(items):
    """Normalizes Tavily search result items into a common internal format."""
    rows: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return rows
    for x in items:
        if not isinstance(x, dict):
            continue
        rows.append(
            {
                "title": x.get("title", ""),
                "summary": x.get("content") or x.get("body") or "",
                "url": x.get("url", ""),
                "source": x.get("source", "tavily"),
                "date": x.get("published_date") or x.get("date") or "",
            }
        )
    return rows


def _collect_tavily_items(
    queries, api_key, timelimit, max_results=6, limit=10, query_limit=3, topic="news", errors_out=None
):
    """Collects search items from Tavily API across multiple queries."""
    if not api_key:
        return []

    items: list[dict[str, Any]] = []
    for q in queries[: max(1, int(query_limit))]:
        if len(items) >= limit * 2:
            break
        try:
            results = tavily_search(
                q,
                api_key=api_key,
                max_results=max_results,
                timelimit=timelimit,
                topic=topic,
                errors_out=errors_out,
            )
            items.extend(_format_tavily_items(results))
        except (ValueError, RuntimeError) as exc:
            # We don't have _summarize_http_error here, we can just do str(exc)
            logger.warning(
                "Tavily search failed (%s): %s", q, exc
            )
            continue

    return ts.dedupe_items(items)[:limit]
