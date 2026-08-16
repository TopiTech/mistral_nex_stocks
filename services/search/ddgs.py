import logging
from typing import Any

from ddgs import DDGS

# Monkeypatch ddgs.engines.yahoo_news.extract_url to handle direct Yahoo News URLs
try:
    from urllib.parse import unquote_plus

    import ddgs.engines.yahoo_news

    def _extract_url_safe(u: str) -> str:
        """Sanitize URL safely without raising IndexError for direct Yahoo URLs."""
        if "/RU=" in u:
            try:
                url = u.split("/RU=", 1)[1].split("/RK=", 1)[0].split("?", 1)[0]
                return unquote_plus(url)
            except Exception:  # nosec B110
                pass
        return u

    ddgs.engines.yahoo_news.extract_url = _extract_url_safe
except Exception as e:
    logging.getLogger(__name__).debug("Failed to patch ddgs yahoo news extract_url: %s", e)

import trend_sources as ts
from utils.env_helpers import _env_int

logger = logging.getLogger(__name__)
MAX_DDGS_QUERY_LEN = 500
MAX_DDGS_QUERY_BYTES = 1000


def _get_ddgs_timeout() -> int:
    """Read DDGS timeout with validation so malformed env values cannot crash search."""
    return _env_int("DDGS_TIMEOUT", 5, 1, 60)


def _sanitize_ddgs_query(
    query: Any, max_chars: int = MAX_DDGS_QUERY_LEN, max_bytes: int = MAX_DDGS_QUERY_BYTES
) -> str:
    """Normalize and safely truncate DDGS query by character and UTF-8 byte length."""
    normalized = " ".join(str(query or "").split())
    if len(normalized) > max_chars:
        logger.warning(
            "DDGS query truncated from %d to %d chars",
            len(normalized),
            max_chars,
        )
        normalized = normalized[:max_chars]

    encoded = normalized.encode("utf-8")
    if len(encoded) > max_bytes:
        trimmed = encoded[:max_bytes].decode("utf-8", errors="ignore")
        logger.warning(
            "DDGS query byte length truncated from %d to %d bytes",
            len(encoded),
            len(trimmed.encode("utf-8")),
        )
        normalized = trimmed.rstrip()

    return normalized


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
        return session.news(**kwargs) or []

    normalized_query = _sanitize_ddgs_query(query)
    short_query = " ".join(normalized_query.split()[:3]).strip()

    def _execute_search(session):
        seen = set()
        last_error_message = ""

        # Fast path: original query on the primary region, with timelimit.
        # Only when that yields nothing do we retry once with a shortened
        # query (no timelimit). This keeps the common case at a single DDGS
        # request instead of the previous 4-region x 4-attempt cascade.
        for q, t in [(normalized_query, timelimit), (short_query, None)]:
            key = (q, t, region)
            # Always try the original query; skip the shortened retry when it
            # does not actually differ from the original (<= 3 words).
            if key in seen or not q:
                continue
            if q == short_query and q == normalized_query and t is None:
                continue
            seen.add(key)
            try:
                results = do_search(session, q, t, region)
                if results:
                    return results
            except Exception as exc:
                message = str(exc)
                last_error_message = message
                if "No results found" in message:
                    logger.debug(
                        "DDGS news no result (%s, region=%s, timelimit=%s)",
                        q,
                        region,
                        t,
                    )
                    continue
                logger.warning(
                    "DDGS news search failed (%s, region=%s, timelimit=%s): %s",
                    q,
                    region,
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
    normalized_query = _sanitize_ddgs_query(query)
    try:

        def do_search(session):
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
        with DDGS(
            timeout=_get_ddgs_timeout(),
        ) as ddgs:
            return do_search(ddgs)
    except Exception as exc:
        message = str(exc)
        if "No results found" in message:
            logger.debug("DDGS text no result (%s, region=%s)", query, region)
        elif "DecodeError" in message:
            logger.debug("DDGS text decode error (%s, region=%s): %s", query, region, message)
        else:
            logger.error("DDGS text search failed (%s): %s", query, exc)
        return []


def _format_ddgs_news_items(items):
    rows: list[dict[str, Any]] = []
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
    rows: list[dict[str, Any]] = []
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


def _collect_ddgs_items(queries, region, timelimit, news_n, text_n, limit=10, query_limit=3):
    """Uses DuckDuckGo Search to collect news and text snippets.

    Query fan-out is parallelised (each query gets its own DDGS session;
    ddgs sessions are not thread-safe for concurrent use). The per-query
    count is capped at ``limit * 2`` so the overall request volume stays
    bounded.
    """
    items: list[dict[str, Any]] = []
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        target_queries = list(queries[: max(1, int(query_limit))])

        def _collect_one(q: str) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            out.extend(
                _format_ddgs_news_items(
                    ddgs_news_search(
                        q,
                        region=region,
                        timelimit=timelimit,
                        max_results=news_n,
                        ddgs_session=None,
                    )
                )
            )
            out.extend(
                _format_ddgs_text_items(
                    ddgs_text_search(
                        q,
                        region=region,
                        timelimit=timelimit,
                        max_results=text_n,
                        ddgs_session=None,
                    )
                )
            )
            return out

        with ThreadPoolExecutor(max_workers=min(3, max(1, len(target_queries)))) as pool:
            futures = [pool.submit(_collect_one, q) for q in target_queries]
            for fut in as_completed(futures):
                if len(items) >= limit * 2:
                    break
                try:
                    items.extend(fut.result())
                except Exception as exc:
                    if "No results found" in str(exc):
                        logger.debug("DDGS context collection: no results for a query")
                    else:
                        logger.warning("DDGS context collection query failed: %s", exc)
    except Exception as exc:
        if "No results found" in str(exc):
            logger.debug("DDGS context collection: no results for queries")
        else:
            logger.warning("DDGS context collection failed: %s", exc)
    return ts.dedupe_items(items)[:limit]


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
