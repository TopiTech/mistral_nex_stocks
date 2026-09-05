import logging
from typing import Any

from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

import trend_sources as ts

logger = logging.getLogger(__name__)


def _external_error_metadata(exc: BaseException | None) -> tuple[str, str]:
    """Return safe diagnostics for an external-provider exception.

    Provider exception messages can reflect request data or credentials.  Keep
    logs useful for operations without serializing their untrusted text.
    """
    if exc is None:
        return "UnknownError", "unknown"
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    return type(exc).__name__, str(status) if status is not None else "unknown"


def _log_tavily_retry(retry_state: Any) -> None:
    """Log retry metadata without Tenacity's raw exception representation."""
    try:
        outcome = getattr(retry_state, "outcome", None)
        exc = outcome.exception() if outcome is not None else None
    except Exception:  # pragma: no cover - defensive logging path
        exc = None
    error_type, status = _external_error_metadata(exc)
    logger.warning(
        "Tavily request retrying attempt=%s error_type=%s status=%s",
        getattr(retry_state, "attempt_number", "?"),
        error_type,
        status,
    )


def _tavily_request_retryable(exc: BaseException) -> bool:
    """Predicate to determine if a Tavily error should be retried.

    Retries only transient failures (network timeouts, connection errors, and
    HTTP 429/5xx); application-level errors (invalid key, quota exhaustion) are
    not retried.
    """
    msg = str(exc).lower()
    if "timeout" in msg or "timed out" in msg or "connection" in msg:
        return True
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is None:
        # tavily-python exposes the HTTP status directly on TavilyError.
        status = getattr(exc, "status_code", None)
    if status is not None:
        return status in (429, 500, 502, 503, 504)
    return "429" in msg or "too many requests" in msg or "rate limit" in msg


@retry(
    retry=retry_if_exception(_tavily_request_retryable),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
    before_sleep=_log_tavily_retry,
)
def _tavily_client_search(client: Any, kwargs: dict[str, Any]) -> Any:
    """Tavily client.search with bounded retry for transient failures."""
    return client.search(**kwargs)


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

        response = _tavily_client_search(client, kwargs)
        results = response.get("results", []) if isinstance(response, dict) else []
        return results if isinstance(results, list) else []
    except ImportError as exc:
        logger.error("Tavily package not installed error_type=%s", type(exc).__name__)
        if isinstance(errors_out, list):
            errors_out.append(exc)
        return []
    except Exception as exc:
        error_type, status = _external_error_metadata(exc)
        logger.warning(
            "Tavily search failed query_length=%s error_type=%s status=%s",
            len(normalized_query),
            error_type,
            status,
        )
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
    queries,
    api_key,
    timelimit,
    max_results=6,
    limit=10,
    query_limit=3,
    topic="news",
    errors_out=None,
):
    """Collects search items from Tavily API across multiple queries."""
    if not api_key:
        return []

    items: list[dict[str, Any]] = []
    query_list = list(queries)
    for q in query_list[: max(1, int(query_limit))]:
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
            error_type, status = _external_error_metadata(exc)
            logger.warning(
                "Tavily search failed query_length=%s error_type=%s status=%s",
                len(str(q)),
                error_type,
                status,
            )
            continue

    return ts.dedupe_items(items)[:limit]
