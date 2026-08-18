import json
import logging
import os
import time
from contextvars import ContextVar
from typing import Any

import requests
from curl_cffi import requests as curl_requests
from curl_cffi.requests import exceptions as curl_exceptions
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    stop_any,
    stop_before_delay,
    wait_exponential,
)
from tenacity.stop import stop_base

import trend_sources as ts
from app_state import app_state
from constants import LANGSEARCH_TIMEOUT, LANGSEARCH_TOTAL_TIMEOUT_SEC
from utils.http_utils import parse_retry_after

logger = logging.getLogger(__name__)

LANGSEARCH_BASE_URL = os.environ.get("LANGSEARCH_BASE_URL", "https://api.langsearch.com").rstrip(
    "/"
)
LANGSEARCH_WEB_SEARCH_ENDPOINT = f"{LANGSEARCH_BASE_URL}/v1/web-search"

_LANGSEARCH_SHARED_DEADLINE: ContextVar[float | None] = ContextVar(
    "langsearch_shared_deadline", default=None
)


def _request_json_post(url, payload, headers, timeout=LANGSEARCH_TIMEOUT):
    """Perform a bounded JSON POST and validate the LangSearch response.

    ``requests`` treats its read timeout as a maximum *idle* interval. An
    upstream that continuously trickles bytes can therefore occupy the worker
    indefinitely even when the logical operation has a deadline. ``curl_cffi``
    maps the same ``(connect, read)`` tuple to libcurl's CONNECTTIMEOUT plus a
    total TIMEOUT equal to their sum, so the deadline passed by
    ``_langsearch_timeout_within`` also bounds an in-progress transfer.

    Translate transport exceptions back to ``requests`` exceptions to preserve
    this module's public retry/fallback contract.
    """
    try:
        response: Any = curl_requests.post(url, json=payload, headers=headers, timeout=timeout)
    except curl_exceptions.Timeout as exc:
        raise requests.Timeout(str(exc)) from exc
    except curl_exceptions.ConnectionError as exc:
        raise requests.ConnectionError(str(exc)) from exc
    except curl_exceptions.RequestException as exc:
        raise requests.RequestException(str(exc)) from exc

    parsed = {}
    try:
        parsed = response.json()
    except (ValueError, TypeError, json.JSONDecodeError):
        pass

    if not response.ok:
        status_code = response.status_code
        error_msg = "Unknown LangSearch error"
        if isinstance(parsed, dict):
            error_msg = str(parsed.get("msg") or parsed.get("message") or f"HTTP {status_code}")
            code = parsed.get("code")
            if code is not None:
                error_msg = f"LangSearch code={code} msg={error_msg}"

        raise requests.HTTPError(error_msg, response=response)

    if isinstance(parsed, dict):
        code = parsed.get("code")
        if code is not None:
            try:
                code_int = int(code)
            except (ValueError, TypeError):
                code_int = None
            if code_int is not None and code_int != 200:
                msg = str(parsed.get("msg") or "LangSearch application-level error")
                raise requests.HTTPError(f"LangSearch code={code_int} msg={msg}", response=response)
    return parsed


def _langsearch_request_retryable(exc: BaseException) -> bool:
    """Predicate to determine if a LangSearch error should be retried."""
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(exc, requests.HTTPError):
        msg = str(exc).lower()
        if any(x in msg for x in ["insufficient balance", "quota exceeded", "balance not enough"]):
            return False

        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        return status in (429, 503)
    return False


# Upper bound for the LangSearch rate-limit slot wait. After a 429 the
# cooldown (default 90s) tells us to back off, but tenacity re-runs
# ``_langsearch_post_json`` (and therefore this function) on every retry attempt,
# so an unbounded sleep would let ONE search call block its request/worker
# thread for minutes (4 attempts × ~90s). Calls arriving inside an active
# cooldown instead fail fast with a non-retryable error; callers already treat
# LangSearch errors as degradable (empty results / next provider).
_LANGSEARCH_SLOT_MAX_WAIT_SEC = 15.0


def _langsearch_acquire_slot(deadline: float | None = None):
    """Acquires a rate-limit slot for LangSearch calls.

    The wait is bounded by ``_LANGSEARCH_SLOT_MAX_WAIT_SEC``. When the next
    allowed time is farther out than the bound (an active 429 cooldown), raise
    a non-retryable ``RuntimeError`` instead of sleeping for minutes inside a
    tenacity retry loop.
    """
    with app_state.ai.langsearch_rate_lock:
        now = time.time()
        wait_seconds = max(0.0, app_state.ai.langsearch_next_allowed_ts - now)
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise requests.Timeout("LangSearch operation deadline exceeded")
            if wait_seconds > remaining:
                raise RuntimeError("LangSearch operation deadline would expire during cooldown")
        if wait_seconds > _LANGSEARCH_SLOT_MAX_WAIT_SEC:
            raise RuntimeError(f"LangSearch rate-limit cooldown active ({wait_seconds:.0f}s)")
        app_state.ai.langsearch_next_allowed_ts = (
            max(app_state.ai.langsearch_next_allowed_ts, now)
            + app_state.ai.langsearch_min_interval_sec
        )
    if wait_seconds > 0:
        time.sleep(wait_seconds)


def _langsearch_mark_retry_after_429(retry_after_sec=None):
    """Flags that LangSearch has rate-limited our requests."""
    cooldown = (
        retry_after_sec if retry_after_sec is not None else app_state.ai.langsearch_429_cooldown_sec
    )
    with app_state.ai.langsearch_rate_lock:
        app_state.ai.langsearch_next_allowed_ts = max(
            app_state.ai.langsearch_next_allowed_ts,
            time.time() + max(0.0, cooldown),
        )


def _langsearch_timeout_within(deadline: float) -> tuple[float, float]:
    """Return a requests timeout tuple that fits within the operation deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise requests.Timeout("LangSearch operation deadline exceeded")

    # requests applies connect and read values independently. Partition the
    # remaining budget so even a worst-case connect followed by a worst-case
    # read cannot exceed the logical operation deadline by design.
    connect_timeout = min(float(LANGSEARCH_TIMEOUT[0]), remaining / 3.0)
    read_timeout = min(float(LANGSEARCH_TIMEOUT[1]), remaining - connect_timeout)
    if connect_timeout <= 0 or read_timeout <= 0:
        raise requests.Timeout("LangSearch operation deadline exhausted")
    return connect_timeout, read_timeout


class _StopBeforeSharedLangSearchDeadline(stop_base):
    """Stop before retry sleep would cross the current collection deadline."""

    def __call__(self, retry_state: Any) -> bool:
        deadline = _LANGSEARCH_SHARED_DEADLINE.get()
        if deadline is None:
            return False
        upcoming_sleep = float(getattr(retry_state, "upcoming_sleep", 0.0) or 0.0)
        return time.monotonic() + upcoming_sleep >= deadline


_STOP_BEFORE_SHARED_LANGSEARCH_DEADLINE = _StopBeforeSharedLangSearchDeadline()


@retry(
    retry=retry_if_exception(_langsearch_request_retryable),
    # Bound one logical HTTP operation, not just each requests timeout. This
    # keeps transient LangSearch failures from occupying an executor worker for
    # the full sum of four request timeouts plus exponential backoff.
    stop=stop_any(
        stop_after_attempt(4),
        stop_before_delay(LANGSEARCH_TOTAL_TIMEOUT_SEC),
        _STOP_BEFORE_SHARED_LANGSEARCH_DEADLINE,
    ),
    wait=wait_exponential(multiplier=1, min=1, max=16),
    reraise=True,
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def _langsearch_post_json_attempt(endpoint, payload, headers, deadline: float):
    """One retryable LangSearch attempt within a bounded operation deadline."""
    if app_state.market.is_circuit_open("langsearch"):
        logger.warning("LangSearch circuit is OPEN. Skipping API call.")
        raise requests.HTTPError("LangSearch circuit is OPEN", response=None)

    try:
        _langsearch_acquire_slot(deadline)
        result = _request_json_post(
            endpoint,
            payload,
            headers,
            timeout=_langsearch_timeout_within(deadline),
        )
        app_state.market.report_circuit_result("langsearch", success=True)
        return result
    except requests.HTTPError as exc:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)

        if status_code == 429:
            logger.warning("LangSearch rate limited (429): %s", exc)

            # Parse and clamp the Retry-After header with the shared helper so a
            # malformed value ("inf"/"NaN") or an absurdly large value cannot
            # set ``langsearch_next_allowed_ts`` to (near-)infinity and disable
            # LangSearch for the rest of the process lifetime. Invalid/non-finite
            # values resolve to None, which falls back to the default cooldown.
            # Pass the exception so the helper reads its ``.response`` (the
            # header source); the helper accepts either shape.
            retry_after = parse_retry_after(exc)
            _langsearch_mark_retry_after_429(retry_after)
        elif status_code is None or status_code >= 500:
            app_state.market.report_circuit_result(
                "langsearch", success=False, threshold=3, open_sec=60
            )

        raise
    except (requests.Timeout, requests.ConnectionError):
        app_state.market.report_circuit_result(
            "langsearch", success=False, threshold=3, open_sec=60
        )
        raise


def _langsearch_post_json(endpoint, payload, headers):
    """Run a bounded LangSearch operation with retry and circuit protection."""
    if app_state.market.is_circuit_open("langsearch"):
        logger.warning("LangSearch circuit is OPEN. Skipping API call.")
        raise requests.HTTPError("LangSearch circuit is OPEN", response=None)

    circuit_probe_claimed = False
    circuit_state = app_state.market.get_circuit_state("langsearch")
    if circuit_state.get("status") == "HALF_OPEN":
        if not app_state.market.try_claim_circuit_probe("langsearch"):
            # A HALF_OPEN circuit already has a live recovery probe. Do not
            # send concurrent probes; callers degrade to their next provider.
            raise RuntimeError("LangSearch circuit recovery probe already in progress")
        circuit_probe_claimed = True

    deadline = _LANGSEARCH_SHARED_DEADLINE.get()
    deadline_token = None
    if deadline is None:
        deadline = time.monotonic() + LANGSEARCH_TOTAL_TIMEOUT_SEC
        deadline_token = _LANGSEARCH_SHARED_DEADLINE.set(deadline)
    try:
        if deadline <= time.monotonic():
            raise RuntimeError("LangSearch operation deadline exceeded")
        return _langsearch_post_json_attempt(endpoint, payload, headers, deadline)
    finally:
        if deadline_token is not None:
            _LANGSEARCH_SHARED_DEADLINE.reset(deadline_token)
        if circuit_probe_claimed:
            # Success/failure reporting normally clears this flag. Release it
            # defensively for 429 and other non-circuit errors so a probe can
            # never leave the service permanently stuck in HALF_OPEN.
            app_state.market.release_circuit_probe("langsearch")


def _summarize_http_error(exc: Exception) -> str:
    """Extracts a human-readable summary from a requests exception."""
    response = getattr(exc, "response", None)
    if response is None:
        return str(exc)
    status = getattr(response, "status_code", "?")
    body = ""
    try:
        body = (response.text or "").strip()
    except (OSError, ValueError, TypeError):
        body = ""
    if len(body) > 300:
        body = body[:300] + "..."
    return f"status={status} body={body or '<empty>'}"


def _extract_langsearch_entries(payload):
    """Locates the list of search results within a LangSearch response."""
    if not isinstance(payload, dict):
        return []

    data = payload.get("data")
    if isinstance(data, dict):
        web_pages = data.get("webPages")
        if isinstance(web_pages, dict) and isinstance(web_pages.get("value"), list):
            return web_pages.get("value")

    candidates = []
    if isinstance(data, dict):
        candidates.extend(
            [
                data.get("results"),
                data.get("items"),
                (
                    data.get("webPages", {}).get("value")
                    if isinstance(data.get("webPages"), dict)
                    else None
                ),
            ]
        )
    candidates.extend(
        [
            payload.get("results"),
            payload.get("items"),
            (
                payload.get("webPages", {}).get("value")
                if isinstance(payload.get("webPages"), dict)
                else None
            ),
        ]
    )

    for candidate in candidates:
        if isinstance(candidate, list):
            return candidate
    return []


def _format_langsearch_items(items):
    """Normalizes LangSearch result items into a common internal format."""
    rows: list[dict[str, Any]] = []
    for x in items:
        if not isinstance(x, dict):
            continue
        rows.append(
            {
                "title": x.get("title") or x.get("name") or "",
                "summary": x.get("snippet")
                or x.get("summary")
                or x.get("description")
                or x.get("body")
                or "",
                "url": x.get("url") or x.get("link") or x.get("href") or "",
                "source": x.get("source")
                or x.get("siteName")
                or x.get("site")
                or x.get("displayUrl")
                or "langsearch",
                "date": x.get("datePublished")
                or x.get("published_at")
                or x.get("publishedAt")
                or x.get("date")
                or x.get("time")
                or "",
            }
        )
    return rows


def _map_langsearch_freshness(timelimit):
    """Maps internal freshness identifiers to LangSearch strings."""
    mapping = {
        "d": "oneDay",
        "w": "oneWeek",
        "m": "oneMonth",
        "y": "oneYear",
        "none": "noLimit",
        "": "noLimit",
        None: "noLimit",
    }
    return mapping.get(str(timelimit).lower(), "noLimit")


def langsearch_search(query, api_key, max_results=8, timelimit="d", errors_out=None):
    """Performs a web search via LangSearch API."""
    normalized_query = " ".join(str(query or "").split())
    if not normalized_query:
        return []
    if not api_key:
        raise ValueError("LangSearch API key is required")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "query": normalized_query,
        "freshness": _map_langsearch_freshness(timelimit),
        "summary": True,
        "count": max(1, int(max_results or 8)),
    }
    try:
        return _extract_langsearch_entries(
            _langsearch_post_json(LANGSEARCH_WEB_SEARCH_ENDPOINT, payload, headers)
        )
    except requests.HTTPError as exc:
        if isinstance(errors_out, list):
            errors_out.append(exc)
        raise
    except Exception as exc:
        if isinstance(errors_out, list):
            errors_out.append(exc)
        raise


def langsearch_rerank(query, documents, api_key):
    """LangSearch Semantic Rerank APIを使用してドキュメントを再評価し、関連性の高い順にソートする"""
    if not api_key or not documents or len(documents) < 2:
        return documents

    # クエリの検証と正規化。空のクエリの場合はリランクせずにそのまま返す
    normalized_query = " ".join(str(query or "").split())
    if not normalized_query:
        return documents

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    # ドキュメントテキストの抽出とサニタイズ。
    doc_texts = []
    for d in documents[:50]:
        if isinstance(d, dict):
            text = (d.get("summary") or d.get("title") or "").strip()
        elif isinstance(d, str):
            text = d.strip()
        else:
            text = str(d).strip()
        if not text:
            text = "[no content]"
        doc_texts.append(text[:1000])

    payload = {
        "model": "langsearch-reranker-v1",
        "query": normalized_query,
        "documents": doc_texts,
    }

    try:
        parsed = _langsearch_post_json(f"{LANGSEARCH_BASE_URL}/v1/rerank", payload, headers)
        raw_results = (
            parsed.get("results")
            or (
                parsed.get("data", {}).get("results")
                if isinstance(parsed.get("data"), dict)
                else parsed.get("data")
            )
            or []
        )
        results = raw_results if isinstance(raw_results, list) else []

        # スコアに基づいてドキュメントをマッピング
        scored_docs = []
        for result in results:
            if not isinstance(result, dict):
                continue
            idx = result.get("index")
            if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < len(documents):
                raw_doc = documents[idx]
                doc = raw_doc.copy() if isinstance(raw_doc, dict) else {"text": str(raw_doc)}
                doc["relevance_score"] = result.get("relevance_score", 0)
                scored_docs.append(doc)

        if not scored_docs:
            return documents

        # スコア降順でソート
        return sorted(scored_docs, key=lambda x: x.get("relevance_score", 0), reverse=True)
    # RuntimeError is the fail-fast signal from ``_langsearch_acquire_slot``
    # when a 429 cooldown is active (wait > _LANGSEARCH_SLOT_MAX_WAIT_SEC). It
    # is a degradable condition, not a hard failure: the search path already
    # treats it that way (``_collect_langsearch_items`` catches RuntimeError per
    # query), so the rerank step must degrade to the un-reranked documents
    # instead of letting the exception escape and fail the whole caller (e.g.
    # /api/analyze-v2 turning an active cooldown into a 500).
    except (requests.RequestException, RuntimeError, ValueError, TypeError, KeyError) as exc:
        logger.warning("LangSearch rerank failed: %s", exc)
        return documents


def _collect_langsearch_items(
    queries, api_key, timelimit, max_results=6, limit=10, query_limit=3, errors_out=None
):
    """Sequentially searches multiple queries and collects unique results."""
    if not api_key:
        return []

    items: list[dict[str, Any]] = []
    deadline_token = _LANGSEARCH_SHARED_DEADLINE.set(
        time.monotonic() + LANGSEARCH_TOTAL_TIMEOUT_SEC
    )
    try:
        for q in queries[: max(1, int(query_limit))]:
            if len(items) >= limit * 2:
                break
            try:
                results = langsearch_search(
                    q,
                    api_key=api_key,
                    max_results=max_results,
                    timelimit=timelimit,
                    errors_out=errors_out,
                )
                items.extend(_format_langsearch_items(results))
            except (ValueError, RuntimeError, requests.RequestException) as exc:
                logger.warning("LangSearch search failed (%s): %s", q, _summarize_http_error(exc))
                continue

        unique_items = ts.dedupe_items(items)

        # Rerank only when the deduplicated result set exceeds the requested
        # limit: the extra rerank API call (latency + quota) is only justified
        # when we actually need to pick the best subset. In the common case the
        # results already fit within the limit, so skip the extra round trip.
        if len(unique_items) > limit and queries:
            unique_items = langsearch_rerank(queries[0], unique_items, api_key)

        return unique_items[:limit]
    finally:
        _LANGSEARCH_SHARED_DEADLINE.reset(deadline_token)
