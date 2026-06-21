import json
import logging
import time
from typing import Optional

import requests
from ddgs import DDGS
# Monkeypatch ddgs.engines.yahoo_news.extract_url to handle direct Yahoo News URLs
# that do not contain "/RU=" redirect parameters, which causes an IndexError.
try:
    import ddgs.engines.yahoo_news
    from urllib.parse import unquote_plus

    def _extract_url_safe(u: str) -> str:
        """Sanitize URL safely without raising IndexError for direct Yahoo URLs."""
        if "/RU=" in u:
            try:
                url = u.split("/RU=", 1)[1].split("/RK=", 1)[0].split("?", 1)[0]
                return unquote_plus(url)
            except Exception:
                pass
        return u

    ddgs.engines.yahoo_news.extract_url = _extract_url_safe
except Exception as e:
    logging.getLogger(__name__).debug("Failed to patch ddgs yahoo news extract_url: %s", e)

from requests.exceptions import RequestException
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

import trend_sources as ts
from app_helpers import _get_cached_value, _set_cached_value
from app_state import app_state
from config_utils import _env_int
from constants import LANGSEARCH_TIMEOUT

logger = logging.getLogger(__name__)
MAX_DDGS_QUERY_LEN = 500


LANGSEARCH_BASE_URL = "https://api.langsearch.com"
LANGSEARCH_WEB_SEARCH_ENDPOINT = f"{LANGSEARCH_BASE_URL}/v1/web-search"


def _get_ddgs_timeout() -> int:
    """Read DDGS timeout with validation so malformed env values cannot crash search."""
    return _env_int("DDGS_TIMEOUT", 5, 1, 60)


def ddgs_news_search(
    query,
    region="us-en",
    timelimit="d",
    max_results=8,
    ddgs_session=None,
):
    """DuckDuckGoでニュース検索を実行する。

    ddgs v9.x (deedy5/ddgs)対応版。
    最新版ではパラメータ名が変更され、戻り値は辞書のリスト。
    クエリ長は500文字に制限される。
    """

    def do_search(session, q, t, r):
        # ddgs v9.x: keywords -> query, verify/backendパラメータ削除
        kwargs = {
            "query": q,
            "region": r,
            "safesearch": "moderate",
            "max_results": max_results,
        }
        if t:
            kwargs["timelimit"] = t
        # ddgs v9.x: news()は既にリストを返す
        return session.news(**kwargs) or []

    normalized_query = " ".join(str(query or "").split())
    # Enforce DuckDuckGo query length limit (500 chars)
    if len(normalized_query) > MAX_DDGS_QUERY_LEN:
        logger.warning(
            "DDGS query truncated from %d to %d chars",
            len(normalized_query),
            MAX_DDGS_QUERY_LEN,
        )
        normalized_query = normalized_query[:MAX_DDGS_QUERY_LEN]
    short_query = " ".join(normalized_query.split()[:3]).strip()
    attempts = [
        (normalized_query, timelimit),
        (normalized_query, None),
    ]
    if short_query and short_query != normalized_query:
        attempts.extend(
            [
                (short_query, timelimit),
                (short_query, None),
            ]
        )

    # リージョン失敗時のフォールバック用リスト
    region_fallbacks = [region, "us-en", "wt-wt", None]

    def _execute_search(session):
        seen = set()
        last_error_message = ""

        for reg in region_fallbacks:
            for q, t in attempts:
                key = (q, t, reg)
                if key in seen or not q:
                    continue
                seen.add(key)
                try:
                    results = do_search(session, q, t, reg)
                    if results:
                        return results
                except Exception as exc:
                    message = str(exc)
                    last_error_message = message
                    if "No results found" in message:
                        logger.debug(
                            "DDGS news no result (%s, region=%s, timelimit=%s)",
                            q,
                            reg,
                            t,
                        )
                        continue
                    # 403/429やその他の接続エラー時は次のリージョンを試す
                    logger.warning(
                        "DDGS news search failed (%s, region=%s, timelimit=%s): %s",
                        q,
                        reg,
                        t,
                        exc,
                    )
                    continue

        if last_error_message:
            logger.debug(
                "DDGS news exhausted all fallback attempts (%s): %s",
                normalized_query,
                last_error_message,
            )
        return []

    if ddgs_session is not None:
        return _execute_search(ddgs_session)

    try:
        with DDGS(timeout=_get_ddgs_timeout()) as ddgs:
            return _execute_search(ddgs)
    except Exception as exc:
        logger.error("DDGS news instantiation or search failed: %s", exc)
        return []


def ddgs_text_search(
    query,
    region="us-en",
    timelimit="w",
    max_results=8,
    ddgs_session=None,
):
    """DuckDuckGoでテキスト検索を実行する。

    ddgs v9.x (deedy5/ddgs)対応:
    - queryパラメータを使用
    - 戻り値はリスト形式
    - クエリ長は500文字に制限される
    """
    # Enforce DuckDuckGo query length limit (500 chars)
    normalized_query = str(query or "").strip()
    if len(normalized_query) > MAX_DDGS_QUERY_LEN:
        logger.warning(
            "DDGS text query truncated from %d to %d chars",
            len(normalized_query),
            MAX_DDGS_QUERY_LEN,
        )
        normalized_query = normalized_query[:MAX_DDGS_QUERY_LEN]
    try:

        def do_search(session):
            # ddgs v9.x: queryパラメータを使用、戻り値はリスト。backend引数は除外
            return (
                session.text(
                    query=normalized_query,
                    region=region,
                    safesearch="moderate",
                    timelimit=timelimit,
                    max_results=max_results,
                )
                or []
            )

        if ddgs_session:
            return do_search(ddgs_session)
        # ddgs v9.x: timeoutパラメータ
        with DDGS(
            timeout=_get_ddgs_timeout(),
        ) as ddgs:
            return do_search(ddgs)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        message = str(exc)
        if "No results found" in message:
            logger.debug("DDGS text no result (%s, region=%s)", query, region)
        elif "DecodeError" in message:
            logger.debug(
                "DDGS text decode error (%s, region=%s): %s", query, region, message
            )
        else:
            logger.error("DDGS text search failed (%s): %s", query, exc)
        return []


def _dedupe_items(items):
    return ts.dedupe_items(items)


def _format_ddgs_news_items(items):
    rows = []
    if not isinstance(items, list):
        return rows
    for x in items:
        if not isinstance(x, dict):
            continue
        rows.append(
            {
                "title": x.get("title", ""),
                "summary": x.get("body", ""),
                "url": x.get("url", ""),
                "source": x.get("source", "ddgs_news"),
                "date": x.get("date", ""),
            }
        )
    return rows


def _format_ddgs_text_items(items):
    rows = []
    if not isinstance(items, list):
        return rows
    for x in items:
        if not isinstance(x, dict):
            continue
        rows.append(
            {
                "title": x.get("title", ""),
                "summary": x.get("body", ""),
                "url": x.get("href", ""),
                "source": "ddgs_text",
                "date": "",
            }
        )
    return rows


def _request_json_post(url, payload, headers, timeout=LANGSEARCH_TIMEOUT):
    """Helper to perform a JSON POST request and validate the response."""
    response = requests.post(url, json=payload, headers=headers, timeout=timeout)

    # Try to parse JSON even on failure to get descriptive error messages
    parsed = {}
    try:
        parsed = response.json()
    except (ValueError, TypeError, json.JSONDecodeError):
        pass

    if not response.ok:
        status_code = response.status_code
        error_msg = "Unknown LangSearch error"
        if isinstance(parsed, dict):
            # Try to get 'msg' from LangSearch's standard error format
            error_msg = str(
                parsed.get("msg") or parsed.get("message") or f"HTTP {status_code}"
            )
            code = parsed.get("code")
            if code is not None:
                error_msg = f"LangSearch code={code} msg={error_msg}"

        # Raise HTTPError with the detailed message
        raise requests.HTTPError(error_msg, response=response)

    # If status is 200, still check for app-level error codes if present
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
        # Do not retry if it looks like a quota or balance issue
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


def _langsearch_mark_retry_after_429(retry_after_sec: Optional[float] = None):
    """Flags that LangSearch has rate-limited our requests.

    If the server provides a Retry-After header, use that value;
    otherwise fall back to the default cooldown.
    """
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

        # 429はレート制限として別途処理、5xxやタイムアウトはサーキット対象
        if status_code == 429:
            # Log the detailed message from the exception (which now includes the body msg)
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
    rows = []
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
    # 空のテキストをAPIに渡すと、サーバー側（LangSearchエンジン）で500エラー（rerank engine error）を引き起こす可能性があるため、
    # 空のテキストはプレースホルダー "[no content]" に置き換えてインデックスの順序と数を維持する。
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
        parsed = _langsearch_post_json(
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
    except Exception as exc:
        logger.warning("LangSearch rerank failed: %s", exc)
        return documents


def _collect_langsearch_items(
    queries, api_key, timelimit, max_results=6, limit=10, query_limit=3
):
    """Sequentially searches multiple queries and collects unique results."""
    if not api_key:
        return []

    items = []
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
        except (ValueError, RuntimeError, RequestException) as exc:
            logger.warning(
                "LangSearch search failed (%s): %s", q, _summarize_http_error(exc)
            )
            continue

    unique_items = _dedupe_items(items)

    # 項目数が多い場合は、最初のクエリを基準にリランクを実行して精度を高める
    if len(unique_items) > 5 and queries:
        unique_items = langsearch_rerank(queries[0], unique_items, api_key)

    return unique_items[:limit]


def _market_ddgs_queries(market="us"):
    """Returns search queries for market-wide news via DDGS."""
    key = "jp" if str(market).lower() == "jp" else "us"
    region = "jp-ja" if key == "jp" else "us-en"
    return region, ts.market_queries(key)


def _symbol_ddgs_queries(symbol, name, market="us"):
    """Returns search queries for specific stock research via DDGS."""
    key = "jp" if str(market).lower() == "jp" else "us"
    region = "jp-ja" if key == "jp" else "us-en"
    return region, ts.symbol_queries(symbol, name, key)


def _collect_ddgs_items(
    queries, region, timelimit, news_n, text_n, limit=10, query_limit=3
):
    """Uses DuckDuckGo Search to collect news and text snippets."""
    items = []
    try:
        # ddgs v9.x: verifyパラメータは削除、timeoutのみ使用。backend指定も削除
        with DDGS(timeout=_get_ddgs_timeout()) as ddgs:
            for q in queries[: max(1, int(query_limit))]:
                if len(items) >= limit * 2:
                    break
                items.extend(
                    _format_ddgs_news_items(
                        ddgs_news_search(
                            q,
                            region=region,
                            timelimit=timelimit,
                            max_results=news_n,
                            ddgs_session=ddgs,
                        )
                    )
                )
                items.extend(
                    _format_ddgs_text_items(
                        ddgs_text_search(
                            q,
                            region=region,
                            timelimit=timelimit,
                            max_results=text_n,
                            ddgs_session=ddgs,
                        )
                    )
                )
    except Exception as exc:
        logger.error("DDGS context collection failed: %s", exc)
    return _dedupe_items(items)[:limit]


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


def collect_market_news_context(market="us", langsearch_api_key=""):
    """Fetches and merges market-wide context from multiple sources."""
    region, queries = _market_ddgs_queries(market)
    ts_items = ts.collect_market_news_items_fast(market)
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
    merged = ts.dedupe_items(list(ts_items) + list(search_items))
    return _compact_small_model_context(merged, limit=6, max_chars=1400)


def collect_symbol_research_context(symbol, name, market="us", langsearch_api_key=""):
    """Collects deep research context for a specific stock ticker."""
    region, queries = _symbol_ddgs_queries(symbol, name, market)
    ts_items = ts.collect_symbol_research_items(symbol, name, market)
    search_items = _collect_langsearch_items(
        queries,
        api_key=langsearch_api_key,
        timelimit="m",
        max_results=3,
        limit=8,
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
    merged = ts.dedupe_items(list(ts_items) + list(search_items))
    return _compact_small_model_context(merged, limit=8, max_chars=2200)


def collect_market_trending_titles(market="us", count=10, langsearch_api_key=""):
    """Retrieve trending market titles for UI display."""
    capped = min(count, 15)
    search_source_hint = "ls" if langsearch_api_key else "ddgs"
    return _get_market_trending_titles(market, search_source_hint, langsearch_api_key)[
        :capped
    ]


def _market_trends_cache_key(market: str, search_source_hint: str) -> str:
    return f"market_trends_{market}_{search_source_hint}"


def _build_market_trending_titles(market: str, langsearch_api_key: str) -> list[str]:
    try:
        trend_target = 12
        region, queries = _market_ddgs_queries(market)
        ts_titles = ts.collect_market_trending_titles(market, count=trend_target)
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
    market: str, search_source_hint: str, langsearch_api_key: str
) -> bool:
    cache_key = _market_trends_cache_key(market, search_source_hint)

    with app_state.trends_refresh_lock:
        if cache_key in app_state.trends_refresh_inflight:
            return False
        app_state.trends_refresh_inflight.add(cache_key)

    def _job():
        try:
            trend_titles = _build_market_trending_titles(market, langsearch_api_key)
            _set_cached_value(cache_key, trend_titles, duration=300)
            logger.info(
                "News trends async refresh completed: market=%s source=%s cache_key=%s items=%s",
                market,
                search_source_hint,
                cache_key,
                len(trend_titles),
            )
        except (RuntimeError, RequestException, ValueError) as exc:
            logger.warning(
                "News trends async refresh failed: market=%s source=%s error=%s",
                market,
                search_source_hint,
                exc,
            )
        finally:
            with app_state.trends_refresh_lock:
                app_state.trends_refresh_inflight.discard(cache_key)

    app_state.executor.submit(_job)
    return True


def _get_market_trending_titles(
    market: str, search_source_hint: str, langsearch_api_key: str
) -> list[str]:
    cache_key = _market_trends_cache_key(market, search_source_hint)
    cached = _get_cached_value(cache_key, duration=300, default=None)

    if isinstance(cached, list) and cached:
        return cached
    if isinstance(cached, str) and cached.strip():
        return [t.strip() for t in cached.split("、") if t.strip()]

    logger.info(
        "Market trending cache miss, building synchronously: market=%s source=%s",
        market,
        search_source_hint,
    )
    trend_titles = _build_market_trending_titles(market, langsearch_api_key)
    if trend_titles:
        _set_cached_value(cache_key, trend_titles, duration=300)
        return trend_titles

    started = _schedule_market_trends_refresh_async(
        market, search_source_hint, langsearch_api_key
    )
    logger.info(
        "Market trending refresh %s after cache miss: market=%s source=%s",
        "started" if started else "already-running",
        market,
        search_source_hint,
    )
    return []
