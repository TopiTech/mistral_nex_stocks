import logging
import os
import random
import threading
import time
from concurrent.futures import as_completed
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
from utils.threading import DaemonThreadPoolExecutor

logger = logging.getLogger(__name__)
_DDGS_SEARCH_POOL = DaemonThreadPoolExecutor(
    max_workers=3, max_queue_size=30, thread_name_prefix="ddgs_search"
)
MAX_DDGS_QUERY_LEN = 500
MAX_DDGS_QUERY_BYTES = 1000

# Reliable default search backends:
# Text: prioritize yahoo, google, duckduckgo (avoid grokipedia TLS issues and mojeek timeouts)
# News: prioritize bing, yahoo, duckduckgo (bing is the most reliable news backend in ddgs)
DEFAULT_DDGS_BACKENDS_TEXT = "yahoo,google,duckduckgo"
DEFAULT_DDGS_BACKENDS_NEWS = "bing,yahoo,duckduckgo"


class _DDGSAvailabilityTracker:
    """Thread-safe tracker for DDGS health and consecutive failure detection.

    Scraping failures are normal and expected, so they are logged as INFO.
    Only when failures consecutively exceed threshold (meaning DDGS is completely
    unavailable) does it emit an ERROR log.
    """

    def __init__(self, max_consecutive_failures: int = 10):
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._is_unavailable = False
        self._max_consecutive_failures = max_consecutive_failures

    def get_threshold(self) -> int:
        return _env_int("DDGS_MAX_CONSECUTIVE_FAILURES", self._max_consecutive_failures, 1, 100)

    def record_success(self) -> None:
        with self._lock:
            if self._is_unavailable:
                logger.info("DDGS service has recovered and is now functional.")
                self._is_unavailable = False
            self._consecutive_failures = 0

    def record_failure(self, context: str, exc: BaseException) -> None:
        with self._lock:
            self._consecutive_failures += 1
            threshold = self.get_threshold()
            if self._consecutive_failures >= threshold:
                if not self._is_unavailable:
                    self._is_unavailable = True
                    logger.error(
                        "DDGS is completely unavailable: %d consecutive failures context=%s error_type=%s",
                        self._consecutive_failures,
                        context,
                        type(exc).__name__,
                    )
                else:
                    logger.info(
                        "DDGS remains unavailable consecutive_failures=%d context=%s error_type=%s",
                        self._consecutive_failures,
                        context,
                        type(exc).__name__,
                    )
            else:
                logger.info(
                    "DDGS %s failed consecutive=%d/%d error_type=%s",
                    context,
                    self._consecutive_failures,
                    threshold,
                    type(exc).__name__,
                )

    @property
    def is_unavailable(self) -> bool:
        with self._lock:
            return self._is_unavailable

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self._consecutive_failures

    def reset_for_testing(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._is_unavailable = False


_availability_tracker = _DDGSAvailabilityTracker()


def _get_ddgs_timeout() -> int:
    """Read DDGS timeout with validation so malformed env values cannot crash search."""
    return _env_int("DDGS_TIMEOUT", 5, 1, 60)


def _get_ddgs_backends(category: str = "text") -> str:
    """Get comma-separated backend list from env or safe defaults.

    Excludes known problematic engines (like grokipedia which throws TLS
    IllegalParameter, and mojeek which times out).
    """
    if category == "news":
        val = os.environ.get("DDGS_BACKENDS_NEWS", "").strip()
        return val or DEFAULT_DDGS_BACKENDS_NEWS
    val = os.environ.get("DDGS_BACKENDS_TEXT", "").strip()
    return val or DEFAULT_DDGS_BACKENDS_TEXT


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
    backend=None,
):
    """DuckDuckGoでニュース検索を実行する。

    ddgs v9.x (deedy5/ddgs)対応版。
    - 安定したバックエンド（bing,yahoo,duckduckgo等）を優先指定
    - 短縮クエリおよびフォールバック試行
    - スクレイピング失敗時はINFOログに出力（完全利用不能時のみERROR）
    """
    normalized_query = _sanitize_ddgs_query(query)
    if not normalized_query:
        return []

    selected_backend = backend or _get_ddgs_backends("news")
    short_query = " ".join(normalized_query.split()[:3]).strip()

    def do_search(session, q, t, r, b):
        kwargs = {
            "query": q,
            "region": r,
            "safesearch": "moderate",
            "max_results": max_results,
        }
        if t:
            kwargs["timelimit"] = t
        if b:
            kwargs["backend"] = b
        return session.news(**kwargs) or []

    def _execute_search(session):
        seen = set()
        last_exc = None

        # Fallback cascade:
        # 1. Primary query + preferred backend + timelimit
        # 2. Shortened query + preferred backend + no timelimit
        # 3. Primary query + fallback backend ('auto') if preferred != 'auto'
        plans = [
            (normalized_query, timelimit, selected_backend),
            (short_query, None, selected_backend),
        ]
        if selected_backend != "auto":
            plans.append((normalized_query, None, "auto"))

        for q, t, b in plans:
            key = (q, t, b)
            if key in seen or not q:
                continue
            if q == short_query and q == normalized_query and t is None and b == selected_backend:
                continue
            seen.add(key)
            try:
                results = do_search(session, q, t, region, b)
                if results:
                    _availability_tracker.record_success()
                    return results
            except Exception as exc:
                last_exc = exc
                message = str(exc)
                if "No results found" in message:
                    logger.debug(
                        "DDGS news no result query_length=%d region=%s timelimit=%s backend=%s",
                        len(q),
                        region,
                        t,
                        b,
                    )
                else:
                    logger.info(
                        "DDGS news search attempt failed query_length=%d region=%s timelimit=%s backend=%s error_type=%s",
                        len(q),
                        region,
                        t,
                        b,
                        type(exc).__name__,
                    )
                time.sleep(random.uniform(0.1, 0.25))

        if last_exc:
            _availability_tracker.record_failure("news search", last_exc)
        return []

    try:
        if ddgs_session is not None:
            return _execute_search(ddgs_session)
        with DDGS(timeout=_get_ddgs_timeout()) as ddgs:
            return _execute_search(ddgs)
    except Exception as exc:
        _availability_tracker.record_failure("news session", exc)
        return []


def ddgs_text_search(
    query,
    region="us-en",
    timelimit="w",
    max_results=8,
    ddgs_session=None,
    backend=None,
):
    """DuckDuckGoでテキスト検索を実行する。

    ddgs v9.x (deedy5/ddgs)対応:
    - queryパラメータを使用
    - 戻り値はリスト形式
    - クエリ長は500文字に制限される
    - 安定したバックエンド（yahoo,google,duckduckgo等）を優先指定し、エラー時はフォールバック
    - スクレイピング失敗時はINFOログに出力（完全利用不能時のみERROR）
    """
    normalized_query = _sanitize_ddgs_query(query)
    if not normalized_query:
        return []

    selected_backend = backend or _get_ddgs_backends("text")
    short_query = " ".join(normalized_query.split()[:3]).strip()

    def do_search(session, q, t, b):
        kwargs = {
            "query": q,
            "region": region,
            "safesearch": "moderate",
            "max_results": max_results,
        }
        if t:
            kwargs["timelimit"] = t
        if b:
            kwargs["backend"] = b
        return session.text(**kwargs) or []

    def _execute(session):
        plans = [(normalized_query, timelimit, selected_backend)]
        if timelimit:
            plans.append((normalized_query, None, selected_backend))
        if short_query and short_query != normalized_query:
            plans.append((short_query, None, selected_backend))
        if selected_backend != "auto":
            plans.append((normalized_query, None, "auto"))

        seen = set()
        last_exc = None
        for q, t, b in plans:
            key = (q, t, b)
            if key in seen or not q:
                continue
            seen.add(key)
            try:
                results = do_search(session, q, t, b)
                if results:
                    _availability_tracker.record_success()
                    return results
            except Exception as exc:
                last_exc = exc
                message = str(exc)
                if "No results found" in message:
                    logger.debug(
                        "DDGS text no result query_length=%d region=%s backend=%s",
                        len(q),
                        region,
                        b,
                    )
                elif "DecodeError" in message:
                    logger.debug(
                        "DDGS text decode error query_length=%d region=%s backend=%s error_type=%s",
                        len(q),
                        region,
                        b,
                        type(exc).__name__,
                    )
                else:
                    logger.info(
                        "DDGS text search attempt failed query_length=%d region=%s backend=%s error_type=%s",
                        len(q),
                        region,
                        b,
                        type(exc).__name__,
                    )
                time.sleep(random.uniform(0.1, 0.25))

        if last_exc:
            _availability_tracker.record_failure("text search", last_exc)
        return []

    try:
        if ddgs_session:
            return _execute(ddgs_session)
        with DDGS(timeout=_get_ddgs_timeout()) as ddgs:
            return _execute(ddgs)
    except Exception as exc:
        _availability_tracker.record_failure("text session", exc)
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
        target_queries = list(queries)[: max(1, int(query_limit))]

        def _collect_one(idx: int, q: str) -> list[dict[str, Any]]:
            if idx > 0:
                time.sleep(idx * random.uniform(0.05, 0.15))
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

        futures = [
            _DDGS_SEARCH_POOL.submit(_collect_one, idx, q) for idx, q in enumerate(target_queries)
        ]
        for fut in as_completed(futures):
            if len(items) >= limit * 2:
                break
            try:
                items.extend(fut.result())
            except Exception as exc:
                if "No results found" in str(exc):
                    logger.debug("DDGS context collection: no results for a query")
                else:
                    logger.info(
                        "DDGS context collection query failed error_type=%s",
                        type(exc).__name__,
                    )
    except Exception as exc:
        if "No results found" in str(exc):
            logger.debug("DDGS context collection: no results for queries")
        else:
            logger.info("DDGS context collection failed error_type=%s", type(exc).__name__)
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
