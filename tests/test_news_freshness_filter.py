import unittest
from datetime import datetime, timedelta, timezone

from utils.formatting import _parse_datetime_to_utc
from services import news_service


class NewsFreshnessFilterTests(unittest.TestCase):
    def test_market_news_freshness_policy_jp_is_stricter(self):
        us_hours, us_undated = news_service._market_news_freshness_policy("us")
        jp_hours, jp_undated = news_service._market_news_freshness_policy("jp")

        self.assertLess(jp_hours, us_hours)
        self.assertLess(jp_undated, us_undated)

    def test_parse_datetime_to_utc_supports_multiple_formats(self):
        values = [
            "2026-04-14T12:34:56+00:00",
            "Tue, 14 Apr 2026 12:34:56 GMT",
            "20260414T123456Z",
            str(int(datetime(2026, 4, 14, 12, 34, 56, tzinfo=timezone.utc).timestamp())),
        ]

        for value in values:
            parsed = _parse_datetime_to_utc(value)
            self.assertIsNotNone(parsed)
            self.assertIsNotNone(parsed.tzinfo)

    def test_filter_recent_market_news_items_drops_old_items(self):
        now = datetime.now(timezone.utc)
        items = [
            {
                "title": "recent item",
                "date": (now - timedelta(hours=2)).isoformat(),
                "url": "https://example.com/recent",
            },
            {
                "title": "old item",
                "date": (now - timedelta(days=10)).isoformat(),
                "url": "https://example.com/old",
            },
        ]

        filtered = news_service._filter_recent_market_news_items(
            items,
            max_age_hours=48,
            allow_undated_limit=0,
            max_items=10,
        )
        titles = [x.get("title") for x in filtered]

        self.assertIn("recent item", titles)
        self.assertNotIn("old item", titles)

    def test_filter_recent_market_news_items_allows_limited_undated(self):
        now = datetime.now(timezone.utc)
        items = [
            {
                "title": "recent item",
                "date": (now - timedelta(hours=2)).isoformat(),
                "url": "https://example.com/recent",
            },
            {
                "title": "undated a",
                "date": "",
                "url": "https://example.com/undated-a",
            },
            {
                "title": "undated b",
                "date": "",
                "url": "https://example.com/undated-b",
            },
            {
                "title": "undated c",
                "date": "",
                "url": "https://example.com/undated-c",
            },
        ]

        filtered = news_service._filter_recent_market_news_items(
            items,
            max_age_hours=48,
            allow_undated_limit=2,
            max_items=10,
        )
        titles = [x.get("title") for x in filtered]

        self.assertIn("recent item", titles)
        self.assertIn("undated a", titles)
        self.assertIn("undated b", titles)
        self.assertNotIn("undated c", titles)


if __name__ == "__main__":
    unittest.main()
