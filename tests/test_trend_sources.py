import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests

import trend_sources as ts


class TrendSourcesTests(unittest.TestCase):
    def test_google_trends_items_is_disabled_by_default(self):
        self.assertEqual(ts.collect_google_trends_items(), [])

    def test_collect_rss_items_uses_generic_feed_urls(self):
        fake_feed = SimpleNamespace(
            feed=SimpleNamespace(title="Reuters Top News"),
            entries=[
                {
                    "title": "Story A",
                    "link": "https://example.com/a",
                    "published": "Sun, 12 Apr 2026 13:40:00 -0700",
                },
                {
                    "title": "Story B",
                    "link": "https://example.com/b",
                    "published": "Sun, 12 Apr 2026 13:35:00 -0700",
                },
            ],
        )

        with patch.object(ts, "_fetch_rss_feed", return_value=fake_feed) as mocked_fetch:
            items = ts.collect_rss_items(
                ["https://example.com/rss"], feed_source="Reuters", max_per_feed=2
            )

        self.assertEqual(len(items), 2)
        self.assertEqual([item["title"] for item in items], ["Story A", "Story B"])
        mocked_fetch.assert_called_once_with("https://example.com/rss")

    def test_collect_yahoo_news_rss_items_uses_market_feeds(self):
        fake_feed = SimpleNamespace(
            feed=SimpleNamespace(title="Yahoo News"),
            entries=[
                {
                    "title": "US Story A",
                    "link": "https://news.yahoo.com/a",
                    "published": "Sun, 12 Apr 2026 13:40:00 -0700",
                }
            ],
        )

        with (
            patch.object(ts, "YAHOO_NEWS_RSS_FEEDS", {"us": ["https://news.yahoo.com/rss/"]}),
            patch.object(ts, "_fetch_rss_feed", return_value=fake_feed) as mocked_fetch,
        ):
            items = ts.collect_yahoo_news_rss_items("us", count=3)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "US Story A")
        mocked_fetch.assert_called_once_with("https://news.yahoo.com/rss/")

    def test_collect_google_trends_rss_items_uses_google_trends_rss(self):
        fake_feed = SimpleNamespace(
            feed=SimpleNamespace(title="Daily Search Trends"),
            entries=[
                {
                    "title": "Trend A",
                    "link": "https://trends.google.com/trending/rss?geo=JP",
                    "ht_news_item_url": "https://example.com/a",
                    "published": "Sun, 12 Apr 2026 13:40:00 -0700",
                }
            ],
        )

        with patch.object(ts, "_fetch_rss_feed", return_value=fake_feed) as mocked_fetch:
            items = ts.collect_google_trends_rss_items("jp", count=1)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Trend A")
        mocked_fetch.assert_called_once_with("https://trends.google.com/trending/rss?geo=JP")

    def test_reddit_hot_items_retries_once_on_429(self):
        fake_response = SimpleNamespace(status_code=429)
        rate_limit_error = requests.HTTPError("429 Too Many Requests")
        rate_limit_error.response = fake_response

        payload = {
            "data": {
                "children": [
                    {
                        "data": {
                            "title": "Reddit Trend",
                            "permalink": "/r/stocks/comments/abc123/reddit_trend/",
                            "created_utc": 1,
                            "score": 42,
                            "num_comments": 3,
                            "subreddit_name_prefixed": "r/stocks",
                        }
                    }
                ]
            }
        }

        call_state = {"count": 0}

        def fake_request_json(*args, **kwargs):
            call_state["count"] += 1
            if call_state["count"] == 1:
                raise rate_limit_error
            return payload

        with (
            patch.object(
                ts,
                "REDDIT_MARKET_SUBREDDITS",
                {"us": ["stocks"], "jp": ["japanstocks"]},
            ),
            patch.object(ts, "_request_json", side_effect=fake_request_json) as mocked_request,
            patch.object(ts.time, "sleep") as mocked_sleep,
        ):
            items = ts.collect_reddit_hot_items("us", limit_per_subreddit=1)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Reddit Trend")
        self.assertEqual(mocked_request.call_count, 2)
        mocked_sleep.assert_called_once()

    def test_collect_market_trending_titles_includes_google_trends_rss(self):
        trend_item = {
            "type": "trend",
            "title": "Keyword X",
            "summary": "google_trends market=us",
            "url": "https://example.com/x",
            "source": "Daily Search Trends",
            "date": "",
            "metadata": {},
        }
        with (
            patch.object(ts, "collect_google_trends_rss_items", return_value=[trend_item]),
            patch.object(ts, "collect_reddit_hot_items", return_value=[]),
            patch.object(ts, "collect_wikipedia_top_items", return_value=[]),
            patch.object(ts, "collect_gdelt_items", return_value=[]),
        ):
            titles = ts.collect_market_trending_titles("us", count=5)

        self.assertEqual(titles, ["Keyword X"])

    def test_collect_market_news_items_includes_yahoo_rss(self):
        yahoo_item = {
            "type": "news",
            "title": "Yahoo Headline",
            "summary": "summary",
            "url": "https://news.yahoo.com/x",
            "source": "Yahoo News",
            "date": "",
            "metadata": {},
        }
        with (
            patch.object(ts, "collect_yahoo_news_rss_items", return_value=[yahoo_item]),
            patch.object(ts, "collect_reddit_hot_items", return_value=[]),
            patch.object(ts, "collect_wikipedia_top_items", return_value=[]),
            patch.object(ts, "collect_gdelt_items", return_value=[]),
        ):
            items = ts.collect_market_news_items("us")

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Yahoo Headline")

    def test_collect_wikipedia_top_items_handles_empty_items_payload(self):
        with patch.object(ts, "_request_json", return_value={"items": []}):
            items = ts.collect_wikipedia_top_items("us", limit=5)

        self.assertEqual(items, [])


if __name__ == "__main__":
    unittest.main()
