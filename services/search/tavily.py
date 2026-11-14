import logging
import time
from typing import Any

from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

import trend_sources as ts

logger = logging.getLogger(__name__)

_TAVILY_REQUEST_TIMEOUT_SEC = 10.0
_TAVILY_TOTAL_DEADLINE_SEC = 18.0
_TAVILY_CIRCUIT_NAME = "tavily"


def _is_tavily_circuit_open() -> bool:
    try:
        from app_state import app_state

        market = getattr(app_state, "market", None)
        if market is not None and hasattr(market, "is_circuit_open"):
            return bool(market.is_circuit_open(_TAVILY_CIRCUIT_NAME))
    except Exception:
        pass
    return False


def _report_tavily_circuit(success: bool) -> None:
    try:
        from app_state import app_state

        market = getattr(app_state, "market", None)
        if market is not None and hasattr(market, "report_circuit_result"):
            market.report_circuit_result(_TAVILY_CIRCUIT_NAME, success)
    except Exception:
        pass


def _extract_tavily_status_code(exc: BaseException | None) -> int | None:
    if exc is None:
        return None
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _is_tavily_circuit_failure(exc: BaseException) -> bool:
    """True for transient/server availability failures that should advance circuit breaker."""
    status = _extract_tavily_status_code(exc)
    if status is not None:
        return status >= 500
    msg = str(exc).lower()
    return any(
        kw in msg
        for kw in ("timeout", "timed out", "connectionerror", "connection reset", "connection aborted")
    ) or isinstance(exc, (TimeoutError, ConnectionError))


def _is_tavily_fatal_batch_error(exc: BaseException) -> bool:
    """True when an auth/rate-limit/quota error makes remaining queries futile."""
    status = _extract_tavily_status_code(exc)
    if status in (401, 403, 429, 432, 433):
        return True
    msg = str(exc).lower()
    return any(
        kw in msg
        for kw in ("unauthorized", "invalid api key", "too many requests", "rate limit", "usage limit")
    )


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
    timeout: float | None = None,
):
    """Performs a web search via Tavily API."""
    normalized_query = " ".join(str(query or "").split())
    if not normalized_query:
        return []
    if not api_key:
        raise ValueError("Tavily API key is required")
    if _is_tavily_circuit_open():
        logger.info("Tavily circuit is open; skipping search request")
        return []

    time_range_map = {
        "d": "day",
        "w": "week",
        "m": "month",
        "y": "year",
    }
    time_range = time_range_map.get(str(timelimit).lower())

    try:
        client = _get_tavily_client(api_key)
        req_timeout = _TAVILY_REQUEST_TIMEOUT_SEC if timeout is None else max(1.0, float(timeout))
        kwargs: dict[str, Any] = {
            "query": normalized_query,
            "search_depth": "advanced" if max_results > 5 else "basic",
            "topic": topic,
            "max_results": min(max(1, int(max_results or 8)), 20),
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "timeout": req_timeout,
        }
        if time_range:
            kwargs["time_range"] = time_range

        response = _tavily_client_search(client, kwargs)
        _report_tavily_circuit(True)
        results = response.get("results", []) if isinstance(response, dict) else []
        return results if isinstance(results, list) else []
    except ImportError as exc:
        logger.error("Tavily package not installed error_type=%s", type(exc).__name__)
        if isinstance(errors_out, list):
            errors_out.append(exc)
        return []
    except Exception as exc:
        error_type, status = _external_error_metadata(exc)
        if _is_tavily_circuit_failure(exc):
            _report_tavily_circuit(False)
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
    try:
        eff_limit = int(limit)
        eff_query_limit = int(query_limit)
    except (TypeError, ValueError):
        return []
    if eff_limit <= 0 or eff_query_limit <= 0:
        return []

    items: list[dict[str, Any]] = []
    query_list = list(queries)
    deadline = time.monotonic() + _TAVILY_TOTAL_DEADLINE_SEC
    local_errors = errors_out if isinstance(errors_out, list) else []

    for q in query_list[: max(1, eff_query_limit)]:
        if len(items) >= eff_limit * 2:
            break
        if time.monotonic() >= deadline or _is_tavily_circuit_open():
            break
        err_start = len(local_errors)
        try:
            results = tavily_search(
                q,
                api_key=api_key,
                max_results=max_results,
                timelimit=timelimit,
                topic=topic,
                errors_out=local_errors,
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
            if _is_tavily_fatal_batch_error(exc):
                break
            continue
        if any(_is_tavily_fatal_batch_error(e) for e in local_errors[err_start:]):
            logger.info("Aborting remaining Tavily queries after terminal auth/rate-limit error")
            break

    return ts.dedupe_items(items)[:eff_limit]
