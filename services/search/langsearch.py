import json
import logging
from typing import Any
import time
import requests
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)
from app_state import app_state
from constants import LANGSEARCH_TIMEOUT
import trend_sources as ts

logger = logging.getLogger(__name__)

LANGSEARCH_BASE_URL = "https://api.langsearch.com"
LANGSEARCH_WEB_SEARCH_ENDPOINT = f"{LANGSEARCH_BASE_URL}/v1/web-search"


def _request_json_post(url, payload, headers, timeout=LANGSEARCH_TIMEOUT):
    """Helper to perform a JSON POST request and validate the response."""
    response = requests.post(url, json=payload, headers=headers, timeout=timeout)

    parsed = {}
    try:
        parsed = response.json()
    except (ValueError, TypeError, json.JSONDecodeError):
        pass

    if not response.ok:
        status_code = response.status_code
        error_msg = "Unknown LangSearch error"
        if isinstance(parsed, dict):
            error_msg = str(
                parsed.get("msg") or parsed.get("message") or f"HTTP {status_code}"
            )
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
                raise requests.HTTPError(
                    f"LangSearch code={code_int} msg={msg}", response=response
                )
    return parsed


def _langsearch_request_retryable(exc: BaseException) -> bool:
    """Predicate to determine if a LangSearch error should be retried."""
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(exc, requests.HTTPError):
        msg = str(exc).lower()
        if any(
            x in msg
            for x in ["insufficient balance", "quota exceeded", "balance not enough"]
        ):
            return False

        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        return status in (429, 503)
    return False


def _langsearch_acquire_slot():
    """Acquires a rate-limit slot for LangSearch calls."""
    with app_state.langsearch_rate_lock:
        now = time.time()
        wait_seconds = max(0.0, app_state.langsearch_next_allowed_ts - now)
        app_state.langsearch_next_allowed_ts = (
            max(app_state.langsearch_next_allowed_ts, now)
            + app_state.langsearch_min_interval_sec
        )
    if wait_seconds > 0:
        time.sleep(wait_seconds)


def _langsearch_mark_retry_after_429(retry_after_sec=None):
    """Flags that LangSearch has rate-limited our requests."""
    cooldown = (
        retry_after_sec
        if retry_after_sec is not None
        else app_state.langsearch_429_cooldown_sec
    )
    with app_state.langsearch_rate_lock:
        app_state.langsearch_next_allowed_ts = max(
            app_state.langsearch_next_allowed_ts,
            time.time() + max(0.0, cooldown),
        )


@retry(
    retry=retry_if_exception(_langsearch_request_retryable),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=16),
    reraise=True,
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def _langsearch_post_json(endpoint, payload, headers):
    """Execution wrapper for LangSearch POST with retry logic."""
    if app_state.is_circuit_open("langsearch"):
        logger.warning("LangSearch circuit is OPEN. Skipping API call.")
        raise requests.HTTPError("LangSearch circuit is OPEN", response=None)

    _langsearch_acquire_slot()
    try:
        result = _request_json_post(
            endpoint, payload, headers, timeout=LANGSEARCH_TIMEOUT
        )
        app_state.report_circuit_result("langsearch", success=True)
        return result
    except requests.HTTPError as exc:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)

        if status_code == 429:
            logger.warning("LangSearch rate limited (429): %s", exc)

            retry_after = None
            if response is not None:
                retry_after_raw = response.headers.get(
                    "Retry-After"
                ) or response.headers.get("retry-after")
                if retry_after_raw:
                    try:
                        retry_after = float(retry_after_raw)
                    except (ValueError, TypeError):
                        retry_after = None
            _langsearch_mark_retry_after_429(retry_after)
        elif status_code is None or status_code >= 500:
            app_state.report_circuit_result(
                "langsearch", success=False, threshold=3, open_sec=60
            )

        raise
    except (requests.Timeout, requests.ConnectionError):
        app_state.report_circuit_result(
            "langsearch", success=False, threshold=3, open_sec=60
        )
        raise


def _summarize_http_error(exc: Exception) -> str:
    """Extracts a human-readable summary from a requests exception."""
    response = getattr(exc, "response", None)
    if response is None:
        return str(exc)
    status = getattr(response, "status_code", "?")
    body = ""
    try:
        body = (response.text or "").strip()
    except (IOError, ValueError, TypeError):
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


def langsearch_search(query, api_key, max_results=8, timelimit="d"):
    """Performs a web search via LangSearch API."""
    import services.search_service
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
            services.search_service._langsearch_post_json(LANGSEARCH_WEB_SEARCH_ENDPOINT, payload, headers)
        )
    except requests.HTTPError as exc:
        response = getattr(exc, "response", None)
        if getattr(response, "status_code", None) == 429:
            _langsearch_mark_retry_after_429()
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
        text = (d.get("summary") or d.get("title") or "").strip()
        if not text:
            text = "[no content]"
        doc_texts.append(text[:1000])

    payload = {
        "model": "langsearch-reranker-v1",
        "query": normalized_query,
        "documents": doc_texts,
    }

    try:
        import services.search_service
        parsed = services.search_service._langsearch_post_json(
            f"{LANGSEARCH_BASE_URL}/v1/rerank", payload, headers
        )
        results = parsed.get("results", [])

        # スコアに基づいてドキュメントをマッピング
        scored_docs = []
        for result in results:
            idx = result.get("index")
            if idx is not None and idx < len(documents):
                doc = documents[idx].copy()
                doc["relevance_score"] = result.get("relevance_score", 0)
                scored_docs.append(doc)

        if not scored_docs:
            return documents

        # スコア降順でソート
        return sorted(
            scored_docs, key=lambda x: x.get("relevance_score", 0), reverse=True
        )
    except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
        logger.warning("LangSearch rerank failed: %s", exc)
        return documents


def _collect_langsearch_items(
    queries, api_key, timelimit, max_results=6, limit=10, query_limit=3
):
    """Sequentially searches multiple queries and collects unique results."""
    if not api_key:
        return []

    items: list[dict[str, Any]] = []
    for q in queries[: max(1, int(query_limit))]:
        if len(items) >= limit * 2:
            break
        try:
            results = langsearch_search(
                q,
                api_key=api_key,
                max_results=max_results,
                timelimit=timelimit,
            )
            items.extend(_format_langsearch_items(results))
        except (ValueError, RuntimeError, requests.RequestException) as exc:
            logger.warning(
                "LangSearch search failed (%s): %s", q, _summarize_http_error(exc)
            )
            continue

    unique_items = ts.dedupe_items(items)

    # 項目数が多い場合は、最初のクエリを基準にリランクを実行して精度を高める
    if len(unique_items) > 5 and queries:
        unique_items = langsearch_rerank(queries[0], unique_items, api_key)

    return unique_items[:limit]
