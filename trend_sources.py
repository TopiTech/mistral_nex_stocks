# pylint: disable=missing-class-docstring,missing-function-docstring,too-many-branches,too-many-locals,too-many-statements,too-many-return-statements,too-many-arguments,too-many-positional-arguments
"""トレンド・ニュース収集モジュール"""

from __future__ import annotations

import atexit
import logging
import math
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable
from urllib.parse import quote_plus

import requests

try:
    import feedparser
except ImportError:  # pragma: no cover - optional dependency
    feedparser = None

try:
    from pytrends_modern.request import TrendReq

    try:
        from pytrends_modern import BrowserConfig
    except ImportError:
        BrowserConfig = None  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    TrendReq = None  # type: ignore
    BrowserConfig = None  # type: ignore

try:
    from pytrends_modern import exceptions as _pytrends_exceptions
except ImportError:  # pragma: no cover - optional dependency
    _pytrends_exceptions = None  # type: ignore

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = (4.0, 8.0)  # 個人利用向けに最適化: より迅速なタイムアウト
SOURCE_RESULT_TIMEOUT_SEC = 12  # 個人利用向けに最適化
SYMBOL_QUERY_LIMIT = 3
REDDIT_SEARCH_QUERY_LIMIT = 2
REDDIT_SEARCH_SUBREDDIT_LIMIT = 2
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)
REDDIT_USER_AGENT = "python:mistral_nex_stocks:v3.0 (by /u/local-app)"

# トレンド収集用 executor（各ソースを並列取得）
_TRENDING_EXECUTOR = ThreadPoolExecutor(max_workers=4)
atexit.register(
    lambda: _TRENDING_EXECUTOR.shutdown(wait=False, cancel_futures=True)
)

# Google Trends global rate limiter
_GOOGLE_TRENDS_LOCK = threading.Lock()
_GOOGLE_TRENDS_LAST_CALL = 0.0
_GOOGLE_TRENDS_MIN_INTERVAL = 1.5  # seconds between calls


class QueryTemplates:
    """クエリテンプレートを管理する定数クラス"""

    MARKET = {
        "jp": [
            "日本株 市場 最新ニュース",
            "日経平均 円相場 金利 日本市場",
            "東京証券取引所 主要ニュース",
            "日本企業 決算 市場インパクト",
            "日本株 site:minkabu.jp",
            "日本株 site:investing.com",
        ],
        "us": [
            "US stock market latest news",
            "S&P 500 Nasdaq Dow market movers",
            "Fed rates inflation earnings market news",
            "US equities sector rotation latest",
            "US stock market site:minkabu.jp",
            "US stock market site:investing.com",
        ],
    }
    SYMBOL = {
        "jp": [
            "{name} {symbol} 決算",
            "{name} {symbol} 業績 見通し",
            "{name} {symbol} 最新ニュース",
            "{name} {symbol} レーティング",
            "{name} {symbol} 新製品 提携",
            "{name} {symbol} 訴訟 規制",
            "{name} {symbol} site:minkabu.jp",
            "{name} {symbol} site:investing.com",
        ],
        "us": [
            "{name} {symbol} earnings guidance",
            "{name} {symbol} latest news",
            "{name} {symbol} analyst rating target",
            "{name} {symbol} product launch partnership",
            "{name} {symbol} lawsuit regulation",
            "{name} {symbol} quarterly results",
            "{name} {symbol} site:minkabu.jp",
            "{name} {symbol} site:investing.com",
        ],
    }

    @classmethod
    def get_market_queries(cls, market):
        """市場クエリを取得"""
        return cls.MARKET.get(market, [])

    @classmethod
    def get_symbol_queries(cls, symbol, name, market):
        """シンボルクエリを取得"""
        templates = cls.SYMBOL.get(market, [])
        return [t.format(symbol=symbol, name=name) for t in templates]


REDDIT_MARKET_SUBREDDITS = {
    "jp": ["japanstocks", "japan", "worldnews", "news", "technology"],
    "us": ["stocks", "investing", "wallstreetbets", "worldnews", "technology", "news"],
}

REDDIT_SEARCH_SUBREDDITS = {
    "jp": ["japanstocks", "japan", "worldnews", "news"],
    "us": ["stocks", "investing", "wallstreetbets", "worldnews", "news"],
}

WIKIPEDIA_PROJECT = {
    "jp": "ja.wikipedia.org",
    "us": "en.wikipedia.org",
}

GOOGLE_TRENDS_PN = {
    "jp": "japan",
    "us": "united_states",
}

GOOGLE_TRENDS_HL = {
    "jp": "ja-JP",
    "us": "en-US",
}

GOOGLE_TRENDS_TZ = {
    "jp": 540,
    "us": 360,
}

YAHOO_NEWS_RSS_FEEDS = {
    "jp": [
        "https://news.yahoo.co.jp/rss/topics/business.xml",
        "https://news.yahoo.co.jp/rss/topics/top-picks.xml",
    ],
    "us": [
        "https://news.yahoo.com/rss/",
        "https://news.yahoo.com/business/rss/",
    ],
}


def normalize_url(url: str | None) -> str:
    """URLを正規化して返す"""
    if not url:
        return ""
    return str(url).strip().rstrip("/")


def _safe_text(value) -> str:
    """値を安全に文字列化して前後空白を除去"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def make_item(
    type_: str,
    title: str,
    summary: str = "",
    url: str = "",
    source: str = "",
    date: str = "",
    metadata: dict | None = None,
) -> dict:
    """収集アイテムの統一辞書を生成"""
    return {
        "type": type_,
        "title": _safe_text(title),
        "summary": _safe_text(summary),
        "url": normalize_url(url),
        "source": _safe_text(source),
        "date": _safe_text(date),
        "metadata": metadata or {},
    }


def dedupe_items(
    items: Iterable[dict], url_key: str = "url", title_key: str = "title"
) -> list[dict]:
    """URLとタイトルでアイテムを重複除去"""
    seen: set[str] = set()
    out: list[dict] = []
    for item in items:
        url = normalize_url(item.get(url_key, ""))
        title = _safe_text(item.get(title_key, "")).lower()
        key = url or title
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def compact_context(items: Iterable[dict], limit: int = 12) -> str:
    """アイテムリストをコンパクトなテキスト形式に整形"""
    rows = []
    for i, item in enumerate(dedupe_items(items)[:limit], start=1):
        title = _safe_text(item.get("title", ""))[:180]
        summary = _safe_text(item.get("summary", ""))[:320]
        source = _safe_text(item.get("source", ""))
        date = _safe_text(item.get("date", ""))
        url = _safe_text(item.get("url", ""))
        rows.append(
            f"[{i}] {title}\n"
            f"source: {source}\n"
            f"date: {date}\n"
            f"summary: {summary}\n"
            f"url: {url}"
        )
    return "\n\n".join(rows)


def extract_titles(items: Iterable[dict], limit: int = 15) -> list[str]:
    """アイテムリストからタイトル一覧を抽出"""
    titles: list[str] = []
    for item in dedupe_items(items)[:limit]:
        title = _safe_text(item.get("title", ""))
        if title:
            titles.append(title)
    return titles


def _request_json(
    url: str, params: dict | None = None, headers: dict | None = None
) -> dict:
    req_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if isinstance(headers, dict):
        req_headers.update(headers)
    response = requests.get(
        url,
        params=params,
        timeout=REQUEST_TIMEOUT,
        headers=req_headers,
    )
    response.raise_for_status()
    return response.json()


def _fetch_rss_feed(rss_url: str):
    # Fetch feed body with explicit timeout so one hung feed does not block the whole analysis.
    response = requests.get(
        rss_url,
        timeout=REQUEST_TIMEOUT,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
        },
    )
    response.raise_for_status()
    return feedparser.parse(response.text)


def _market_key(market: str) -> str:
    return "jp" if str(market).lower() == "jp" else "us"


def _google_trends_rss_url(market: str) -> str:
    market_key = _market_key(market)
    geo = "JP" if market_key == "jp" else "US"
    return f"https://trends.google.com/trending/rss?geo={geo}"


def _collect_google_trends_rss_items(market: str = "us", limit: int = 10) -> list[dict]:
    """
    Google TrendsのRSSフィードから急上昇ワードを収集します。

    Args:
        market: 対象市場 ("us" または "jp")。
        limit: 収集する最大アイテム数。

    Returns:
        収集されたトレンド項目のリスト。
    """
    if feedparser is None:
        logger.warning(
            "feedparser is not installed; Google Trends RSS collection is disabled"
        )
        return []

    market_key = _market_key(market)
    items: list[dict] = []
    try:
        feed = _fetch_rss_feed(_google_trends_rss_url(market_key))
        feed_title = (
            _safe_text(getattr(feed.feed, "title", "Daily Search Trends"))
            or "Daily Search Trends"
        )
        for entry in getattr(feed, "entries", [])[:limit]:
            title = _safe_text(entry.get("title"))
            if not title:
                continue
            metadata = {}
            approx_traffic = _safe_text(
                entry.get("ht_approx_traffic") or entry.get("approx_traffic")
            )
            if approx_traffic:
                metadata["approx_traffic"] = approx_traffic
            news_item_title = _safe_text(
                entry.get("ht_news_item_title") or entry.get("news_item_title")
            )
            if news_item_title:
                metadata["news_item_title"] = news_item_title
            news_item_source = _safe_text(
                entry.get("ht_news_item_source") or entry.get("news_item_source")
            )
            if news_item_source:
                metadata["news_item_source"] = news_item_source
            news_item_url = _safe_text(
                entry.get("ht_news_item_url") or entry.get("news_item_url")
            )
            if news_item_url:
                metadata["news_item_url"] = news_item_url
            items.append(
                make_item(
                    "trend",
                    title,
                    summary=_safe_text(
                        entry.get("summary")
                        or entry.get("description")
                        or f"google_trends market={market_key}"
                    ),
                    url=news_item_url or _safe_text(entry.get("link")),
                    source=feed_title,
                    date=_safe_text(entry.get("published") or entry.get("updated")),
                    metadata=metadata,
                )
            )
    except (requests.RequestException, ValueError, AttributeError) as exc:
        logger.warning("Google Trends RSS collection failed (%s): %s", market_key, exc)
    return dedupe_items(items)[:limit]


def collect_google_trends_rss_items(market: str = "us", count: int = 10) -> list[dict]:
    """
    Google TrendsのRSSフィードから急上昇ワードを収集する外部用関数。
    """
    return _collect_google_trends_rss_items(market, limit=count)


def _dataframe_first_column_values(df, limit: int = 10) -> list[str]:
    if df is None:
        return []
    try:
        if getattr(df, "empty", False):
            return []
        first_column = df.columns[0]
        values = []
        for value in df[first_column].tolist()[:limit]:
            text = _safe_text(value)
            if text:
                values.append(text)
        return values
    except (AttributeError, IndexError, TypeError):
        return []


def _google_trends_client(market: str):
    if TrendReq is None:
        raise RuntimeError(
            "pytrends-modern is not available. "
            "Install pytrends-modern to enable Google Trends keyword lookup."
        )
    market_key = _market_key(market)

    # Use BrowserConfig if available and explicitly enabled via env
    browser_cfg = None
    if (
        BrowserConfig is not None
        and os.environ.get("MNS_USE_BROWSER_TRENDS", "0") == "1"
    ):
        try:
            browser_cfg = BrowserConfig(headless=True)
        except Exception as exc:
            logger.debug("Failed to initialize BrowserConfig: %s", exc)

    return TrendReq(
        hl=GOOGLE_TRENDS_HL[market_key],
        tz=GOOGLE_TRENDS_TZ[market_key],
        retries=5,
        backoff_factor=2.0,
        browser_config=browser_cfg,
    )


def _trend_queries_for_keyword(keyword: str, market: str, limit: int = 5) -> list[str]:
    if TrendReq is None:
        return []

    global _GOOGLE_TRENDS_LAST_CALL

    # Build exception tuple dynamically so tests pass when
    # pytrends-modern is absent.
    _trend_exc_types: tuple = (
        RuntimeError,
        ValueError,
        KeyError,
        AttributeError,
        requests.RequestException,
    )
    if _pytrends_exceptions is not None:
        _trend_exc_types = _trend_exc_types + (
            _pytrends_exceptions.ResponseError,
            _pytrends_exceptions.TooManyRequestsError,
        )

    try:
        with _GOOGLE_TRENDS_LOCK:
            elapsed = time.time() - _GOOGLE_TRENDS_LAST_CALL
            if elapsed < _GOOGLE_TRENDS_MIN_INTERVAL:
                time.sleep(_GOOGLE_TRENDS_MIN_INTERVAL - elapsed)

            pytrends = _google_trends_client(market)
            suggestions = pytrends.suggestions(keyword) or []
            out: list[str] = []
            try:
                for entry in suggestions:
                    title = _safe_text(entry.get("title"))
                    if title and title not in out:
                        out.append(title)
                    if len(out) >= limit:
                        return out

                try:
                    market_key = _market_key(market)
                    geo = "JP" if market_key == "jp" else "US"
                    pytrends.build_payload([keyword], geo=geo, timeframe="today 12-m")
                    related = pytrends.related_queries() or {}
                    related_data: Dict[str, Any] = related.get(keyword) or next(
                        iter(related.values()), {}
                    )
                    for key in ("top", "rising"):
                        df = related_data.get(key)
                        if df is None or getattr(df, "empty", True):
                            continue
                        for value in df.iloc[:, 0].tolist():
                            text = _safe_text(value)
                            if text and text not in out:
                                out.append(text)
                            if len(out) >= limit:
                                return out
                except (AttributeError, KeyError, TypeError, ValueError):
                    pass

                return out[:limit]
            finally:
                # 呼び出し時刻を確実に更新（途中 return でも）
                _GOOGLE_TRENDS_LAST_CALL = time.time()
    except _trend_exc_types as exc:
        with _GOOGLE_TRENDS_LOCK:
            _GOOGLE_TRENDS_LAST_CALL = time.time()
        logger.debug("Google Trends keyword lookup failed for %s: %s", keyword, exc)
        return []


def collect_google_trends_items(
    market: str = "us", count: int = 10, enabled: bool = False
) -> list[dict]:
    """
    市場全体のトレンドを収集します（現在はRSSソースのみ）。
    """
    market_key = _market_key(market)
    if not enabled:
        logger.info(
            "Google Trends collection is disabled by default (market=%s)", market_key
        )
        return []
    return collect_google_trends_rss_items(market_key, count=count)


def collect_google_trends_keyword_items(
    keyword: str, market: str = "us", limit: int = 5
) -> list[dict]:
    """指定キーワードのGoogle Trends関連語を収集"""
    items: list[dict] = []
    for title in _trend_queries_for_keyword(keyword, market, limit=limit):
        items.append(
            make_item(
                "trend",
                title,
                summary=f"google_trends keyword={_safe_text(keyword)} market={_market_key(market)}",
                url="https://trends.google.com/trends/explore",
                source="google_trends",
            )
        )
    return dedupe_items(items)[:limit]


def collect_rss_items(
    feed_urls: Iterable[str], feed_source: str = "rss", max_per_feed: int = 4
) -> list[dict]:
    """
    複数のRSSフィードからニュース記事を収集します。

    Args:
        feed_urls: フィードURLのリスト。
        feed_source: データソースの識別名。
        max_per_feed: フィードあたりの最大収集数。
    """
    if feedparser is None:
        logger.warning("feedparser is not installed; RSS collection is disabled")
        return []

    items: list[dict] = []
    for feed_url in list(feed_urls):
        try:
            feed = _fetch_rss_feed(feed_url)
            feed_title = (
                _safe_text(getattr(feed.feed, "title", feed_source)) or feed_source
            )
            for entry in getattr(feed, "entries", [])[:max_per_feed]:
                title = _safe_text(entry.get("title"))
                if not title:
                    continue
                items.append(
                    make_item(
                        "news",
                        title,
                        summary=_safe_text(
                            entry.get("summary")
                            or entry.get("description")
                            or feed_title
                        ),
                        url=_safe_text(entry.get("link")),
                        source=feed_title,
                        date=_safe_text(entry.get("published") or entry.get("updated")),
                        metadata={"feed_url": feed_url},
                    )
                )
        except (requests.RequestException, ValueError) as exc:
            logger.debug("RSS fetch failed (%s): %s", feed_url, exc)
    return dedupe_items(items)


def collect_yahoo_news_rss_items(market: str = "us", count: int = 8) -> list[dict]:
    """
    Yahoo NewsのRSSから市場ニュースを収集します。
    """
    market_key = _market_key(market)
    feed_urls = YAHOO_NEWS_RSS_FEEDS.get(market_key) or YAHOO_NEWS_RSS_FEEDS["us"]
    max_per_feed = max(1, math.ceil(count / max(1, len(feed_urls))))
    items = collect_rss_items(
        feed_urls,
        feed_source="yahoo_news",
        max_per_feed=max_per_feed,
    )
    return dedupe_items(items)[:count]


def collect_reddit_hot_items(
    market: str = "us", limit_per_subreddit: int = 5
) -> list[dict]:
    """
    Redditの特定サブレディットからHotな投稿を収集します。
    """
    items: list[dict] = []
    subreddits = REDDIT_MARKET_SUBREDDITS[_market_key(market)]
    for subreddit in subreddits:
        url = (
            f"https://www.reddit.com/r/{subreddit}/hot.json?limit={limit_per_subreddit}"
        )
        try:
            payload = _request_json_retry_on_429(
                url, headers={"User-Agent": REDDIT_USER_AGENT}
            )
            for child in payload.get("data", {}).get("children", [])[
                :limit_per_subreddit
            ]:
                data = child.get("data", {})
                title = _safe_text(data.get("title"))
                if not title:
                    continue
                items.append(
                    make_item(
                        "social",
                        title,
                        summary=_safe_text(
                            data.get("selftext") or data.get("subreddit_name_prefixed")
                        ),
                        url=f"https://www.reddit.com{_safe_text(data.get('permalink'))}",
                        source=f"reddit:{subreddit}",
                        date=str(data.get("created_utc", "")),
                        metadata={
                            "score": data.get("score"),
                            "comments": data.get("num_comments"),
                        },
                    )
                )
        except (requests.RequestException, ValueError, KeyError, RuntimeError) as exc:
            logger.debug("Reddit hot fetch failed (%s): %s", subreddit, exc)
    return dedupe_items(items)


def collect_reddit_search_items(
    queries: Iterable[str], market: str = "us", limit_per_query: int = 4
) -> list[dict]:
    """Redditでクエリ検索して投稿を収集"""
    items: list[dict] = []
    subreddits = REDDIT_SEARCH_SUBREDDITS[_market_key(market)][
        :REDDIT_SEARCH_SUBREDDIT_LIMIT
    ]
    for query in list(queries)[:REDDIT_SEARCH_QUERY_LIMIT]:
        for subreddit in subreddits:
            url = (
                f"https://www.reddit.com/r/{subreddit}/search.json?"
                f"q={quote_plus(query)}&restrict_sr=1&sort=hot&t=month&limit={limit_per_query}"
            )
            try:
                payload = _request_json_retry_on_429(
                    url, headers={"User-Agent": REDDIT_USER_AGENT}
                )
                for child in payload.get("data", {}).get("children", [])[
                    :limit_per_query
                ]:
                    data = child.get("data", {})
                    title = _safe_text(data.get("title"))
                    if not title:
                        continue
                    items.append(
                        make_item(
                            "social",
                            title,
                            summary=_safe_text(data.get("selftext") or query),
                            url=f"https://www.reddit.com{_safe_text(data.get('permalink'))}",
                            source=f"reddit:{subreddit}",
                            date=str(data.get("created_utc", "")),
                            metadata={
                                "score": data.get("score"),
                                "comments": data.get("num_comments"),
                            },
                        )
                    )
            except (
                requests.RequestException,
                ValueError,
                KeyError,
                RuntimeError,
            ) as exc:
                logger.debug(
                    "Reddit search failed (%s / %s): %s", subreddit, query, exc
                )
    return dedupe_items(items)


def _request_json_retry_on_429(
    url: str, params: dict | None = None, headers: dict | None = None
) -> dict:
    try:
        return _request_json(url, params=params, headers=headers)
    except requests.HTTPError as exc:
        response = getattr(exc, "response", None)
        if getattr(response, "status_code", None) != 429:
            raise

        # Second attempt with exponential backoff and jitter for rate limited requests
        logger.debug("Request rate limited (429); applying backoff. url=%s", url)
        time.sleep(2.0 + random.random())
        try:
            return _request_json(url, params=params, headers=headers)
        except requests.HTTPError as exc2:
            if getattr(getattr(exc2, "response", None), "status_code", None) == 429:
                logger.warning("Rate limit persists after retry; skipping. url=%s", url)
                raise
            raise


def _wikipedia_project(market: str) -> str:
    """市場に応じたWikipediaプロジェクトドメインを返す"""
    return WIKIPEDIA_PROJECT[_market_key(market)]


def collect_wikipedia_top_items(market: str = "us", limit: int = 10) -> list[dict]:
    """Wikipediaのページビュー上位記事を収集する"""
    market_key = _market_key(market)
    project = _wikipedia_project(market_key)
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    url = (
        "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/"
        f"{project}/all-access/{yesterday:%Y/%m/%d}"
    )
    items: list[dict] = []
    try:
        payload = _request_json(url)
        payload_items = payload.get("items") if isinstance(payload, dict) else None
        top_item = (
            payload_items[0]
            if isinstance(payload_items, list) and payload_items
            else {}
        )
        articles = top_item.get("articles", []) if isinstance(top_item, dict) else []
        for article in articles[:limit]:
            title = _safe_text(article.get("article"))
            if not title or title == "Main_Page":
                continue
            items.append(
                make_item(
                    "reference",
                    title.replace("_", " "),
                    summary=f"wikipedia pageviews project={project} views={article.get('views')}",
                    url=f"https://{project}/wiki/{quote_plus(title)}",
                    source="wikipedia_pageviews",
                    metadata={"views": article.get("views")},
                )
            )
    except (
        requests.RequestException,
        ValueError,
        KeyError,
        IndexError,
        AttributeError,
    ) as exc:
        logger.debug("Wikipedia top pageviews failed (%s): %s", market_key, exc)
    return dedupe_items(items)


def collect_wikipedia_search_items(
    queries: Iterable[str], market: str = "us", limit_per_query: int = 2
) -> list[dict]:
    """Wikipediaでクエリ検索して記事を収集"""
    market_key = _market_key(market)
    project = _wikipedia_project(market_key)
    items: list[dict] = []
    for query in queries:
        try:
            search_payload = _request_json(
                f"https://{project}/w/api.php",
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": query,
                    "format": "json",
                    "srlimit": limit_per_query,
                },
            )
            for entry in search_payload.get("query", {}).get("search", [])[
                :limit_per_query
            ]:
                title = _safe_text(entry.get("title"))
                if not title:
                    continue
                summary = _safe_text(entry.get("snippet"))
                page_url = (
                    f"https://{project}/wiki/{quote_plus(title.replace(' ', '_'))}"
                )
                items.append(
                    make_item(
                        "reference",
                        title,
                        summary=summary,
                        url=page_url,
                        source="wikipedia_search",
                    )
                )
        except (requests.RequestException, ValueError, KeyError, AttributeError) as exc:
            logger.debug("Wikipedia search failed (%s): %s", query, exc)
    return dedupe_items(items)


def _gdelt_query_url(query: str, market: str) -> str:
    """GDELT APIのクエリURLを生成"""
    market_key = _market_key(market)
    lang = "japanese" if market_key == "jp" else "english"
    return (
        "https://api.gdeltproject.org/api/v2/doc/doc?"
        f"query={quote_plus(query)}&mode=ArtList&maxrecords=10"
        f"&format=json&sort=HybridRel&lang={lang}"
    )


def collect_gdelt_items(
    queries: Iterable[str], market: str = "us", max_per_query: int = 4
) -> list[dict]:
    """GDELT APIからニュース記事を収集する"""
    items: list[dict] = []
    for query in queries:
        try:
            payload = _request_json(_gdelt_query_url(query, market))
            articles = payload.get("articles") or []
            for article in articles[:max_per_query]:
                title = _safe_text(article.get("title"))
                url = _safe_text(article.get("url"))
                if not title and not url:
                    continue
                items.append(
                    make_item(
                        "news",
                        title or url,
                        summary=_safe_text(
                            article.get("seendate")
                            or article.get("sourceCountry")
                            or query
                        ),
                        url=url,
                        source=_safe_text(
                            article.get("domain")
                            or article.get("sourceCountry")
                            or "gdelt"
                        ),
                        date=_safe_text(article.get("seendate")),
                        metadata={
                            "language": article.get("language"),
                            "sourcecountry": article.get("sourceCountry"),
                        },
                    )
                )
        except (requests.RequestException, ValueError, KeyError, AttributeError) as exc:
            logger.debug("GDELT fetch failed (%s): %s", query, exc)
    return dedupe_items(items)


def market_queries(market: str = "us") -> list[str]:
    """Get generalized search queries for a market."""
    return QueryTemplates.get_market_queries(market)


def symbol_queries(symbol: str, name: str, market: str = "us") -> list[str]:
    """Get specific search queries for a given stock symbol and name."""
    return QueryTemplates.get_symbol_queries(symbol, name, market)


def collect_market_trending_items(market: str = "us", count: int = 10) -> list[dict]:
    """Collect trending items for a market from various generic sources."""
    market_key = _market_key(market)
    queries = market_queries(market_key)[:4]
    items: list[dict] = []
    tasks = []

    try:
        tasks.append(
            _TRENDING_EXECUTOR.submit(
                collect_google_trends_rss_items, market_key, count
            )
        )
        tasks.append(
            _TRENDING_EXECUTOR.submit(
                collect_reddit_hot_items,
                market_key,
                max(2, count // 3 or 1),
            )
        )
        tasks.append(
            _TRENDING_EXECUTOR.submit(
                collect_wikipedia_top_items,
                market_key,
                max(2, count // 2 or 1),
            )
        )
        tasks.append(
            _TRENDING_EXECUTOR.submit(
                collect_gdelt_items, queries, market_key, 2
            )
        )

        done, not_done = wait(tasks, timeout=SOURCE_RESULT_TIMEOUT_SEC)
        for fut in not_done:
            fut.cancel()

        _exc_types = (RuntimeError, ValueError, KeyError, AttributeError)
        for fut in done:
            try:
                items.extend(fut.result() or [])
            except _exc_types:
                logger.debug("Market trending source failed (market=%s)", market_key)

        if not_done:
            logger.debug(
                "Market trending source timeout (market=%s, timed_out=%s)",
                market_key,
                len(not_done),
            )

    except Exception:
        logger.debug("Market trending collection aborted (market=%s)", market_key)

    return dedupe_items(items)


def collect_market_news_items(market: str = "us") -> list[dict]:
    """Collect broad market news items from various sources."""
    market_key = _market_key(market)
    queries = market_queries(market_key)[:4]
    items: list[dict] = []

    items.extend(collect_yahoo_news_rss_items(market_key, count=8))
    items.extend(collect_reddit_hot_items(market_key, limit_per_subreddit=4))
    items.extend(collect_wikipedia_top_items(market_key, limit=4))
    items.extend(collect_gdelt_items(queries, market_key, max_per_query=2))
    return dedupe_items(items)


def collect_market_news_items_fast(market: str = "us") -> list[dict]:
    """Quickly collect core market news items from limited sources."""
    market_key = _market_key(market)
    queries = market_queries(market_key)[:2]
    items: list[dict] = []

    items.extend(collect_yahoo_news_rss_items(market_key, count=4))
    items.extend(collect_gdelt_items(queries, market_key, max_per_query=1))
    return dedupe_items(items)


# グローバルなexecutorを使用（毎回作成しない）
_SYMBOL_RESEARCH_EXECUTOR = ThreadPoolExecutor(max_workers=6)
atexit.register(
    lambda: _SYMBOL_RESEARCH_EXECUTOR.shutdown(wait=False, cancel_futures=True)
)


def collect_symbol_research_items(
    symbol: str, name: str, market: str = "us"
) -> list[dict]:
    """Collect specific research items for a given symbol."""
    market_key = _market_key(market)
    queries = symbol_queries(symbol, name, market_key)[:SYMBOL_QUERY_LIMIT]
    items: list[dict] = []
    tasks = []
    try:
        tasks.append(
            _SYMBOL_RESEARCH_EXECUTOR.submit(
                collect_google_trends_keyword_items, name, market_key, 5
            )
        )
        if symbol and symbol != name:
            tasks.append(
                _SYMBOL_RESEARCH_EXECUTOR.submit(
                    collect_google_trends_keyword_items, symbol, market_key, 3
                )
            )

        tasks.append(
            _SYMBOL_RESEARCH_EXECUTOR.submit(
                collect_reddit_search_items, queries, market_key, 2
            )
        )
        tasks.append(
            _SYMBOL_RESEARCH_EXECUTOR.submit(
                collect_wikipedia_search_items, [name, symbol], market_key, 2
            )
        )
        tasks.append(
            _SYMBOL_RESEARCH_EXECUTOR.submit(
                collect_gdelt_items, queries, market_key, 2
            )
        )

        done, not_done = wait(tasks, timeout=SOURCE_RESULT_TIMEOUT_SEC)
        _symbol_exc_types: tuple = (
            RuntimeError,
            ValueError,
            KeyError,
            AttributeError,
            requests.RequestException,
        )
        if _pytrends_exceptions is not None:
            _symbol_exc_types = _symbol_exc_types + (
                _pytrends_exceptions.ResponseError,
                _pytrends_exceptions.TooManyRequestsError,
            )
        for future in done:
            try:
                items.extend(future.result() or [])
            except _symbol_exc_types as exc:
                logger.debug("Symbol research source failed (%s): %s", symbol, exc)

        if not_done:
            logger.debug(
                "Symbol research source timeout (market=%s, symbol=%s, timed_out=%s)",
                market_key,
                symbol,
                len(not_done),
            )
            for future in not_done:
                future.cancel()
    finally:
        # グローバルexecutorはシャットダウンしない
        pass
    return dedupe_items(items)


def collect_market_news_context(market: str = "us") -> str:
    """Format market news items into a compact context string."""
    return compact_context(collect_market_news_items(market), limit=14)


def collect_symbol_research_context(symbol: str, name: str, market: str = "us") -> str:
    """Format symbol research items into a compact context string."""
    return compact_context(
        collect_symbol_research_items(symbol, name, market), limit=14
    )


def collect_market_trending_titles(market: str = "us", count: int = 15) -> list[str]:
    """Extract titles of trending topics for a given market."""
    return extract_titles(
        collect_market_trending_items(market, count=count), limit=count
    )
