"""Tests for services.realtime.utils and scrapers robustness."""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import MagicMock, PropertyMock, patch
from zoneinfo import ZoneInfo

from services.realtime.scrapers import (
    MinkabuScraper,
    SBISecuritiesScraper,
    YahooJPRealtimeScraper,
)
from services.realtime.utils import (
    _extract_next_data_quotes,
    _extract_pts_fields,
    _extract_pts_price_data,
    extract_minkabu_price,
    is_jp_market_open,
)

JST = ZoneInfo("Asia/Tokyo")


class TestRealtimeUtilsMarketOpen(unittest.TestCase):
    """Test is_jp_market_open holiday handling and session boundaries (BE-008)."""

    def test_jp_market_closed_on_weekdays_if_holiday(self):
        # 2026-05-04 is Greenery Day (みどりの日), a Monday holiday in Japan
        holiday_dt = datetime(2026, 5, 4, 10, 0, tzinfo=JST)
        self.assertFalse(is_jp_market_open(holiday_dt))

        # 2026-05-05 is Children's Day (こどもの日), Tuesday
        holiday_dt2 = datetime(2026, 5, 5, 13, 0, tzinfo=JST)
        self.assertFalse(is_jp_market_open(holiday_dt2))

        # 2026-01-01 is New Year's Day (元日), Thursday
        new_year_dt = datetime(2026, 1, 1, 10, 30, tzinfo=JST)
        self.assertFalse(is_jp_market_open(new_year_dt))

    def test_jp_market_open_on_regular_weekday_hours(self):
        # 2026-08-03 is a regular Monday
        mon_morning = datetime(2026, 8, 3, 10, 0, tzinfo=JST)
        self.assertTrue(is_jp_market_open(mon_morning))

        mon_afternoon = datetime(2026, 8, 3, 13, 30, tzinfo=JST)
        self.assertTrue(is_jp_market_open(mon_afternoon))

    def test_jp_market_closed_on_weekend(self):
        # 2026-08-02 is Sunday
        sun = datetime(2026, 8, 2, 10, 0, tzinfo=JST)
        self.assertFalse(is_jp_market_open(sun))


class TestRealtimeScrapersRobustness(unittest.TestCase):
    """Test scraper parser robustness against corrupt HTML/DOM (BE-010)."""

    def test_extract_minkabu_price_handles_corrupt_html(self):
        # Corrupt / empty inputs
        self.assertIsNone(extract_minkabu_price(""))
        self.assertIsNone(extract_minkabu_price("<html>no price here</html>"))

        # Exception injection
        mock_re = MagicMock()
        mock_re.search.side_effect = RuntimeError("Corrupt regex")
        with patch("services.realtime.utils._MINKABU_PRICE_CLASS_RE", mock_re):
            self.assertIsNone(extract_minkabu_price("<div class='stock_price'>1,234.5</div>"))

    def test_extract_pts_price_data_handles_corrupt_html(self):
        self.assertIsNone(_extract_pts_price_data(""))
        self.assertIsNone(_extract_pts_price_data("Malformed { unclosed json"))

        with patch("services.realtime.utils._PTS_PRICE_DATA_MARKERS", side_effect=Exception("Iter error")):
            self.assertIsNone(_extract_pts_price_data('ptsPriceData\":{\"price\":\"100\"}'))

    def test_extract_pts_fields_handles_corrupt_segment(self):
        self.assertEqual(_extract_pts_fields(""), {})
        # Corrupt segment that cannot be parsed
        self.assertEqual(_extract_pts_fields("{not valid json:"), {})

    def test_extract_next_data_quotes_handles_corrupt_html(self):
        self.assertIsNone(_extract_next_data_quotes(""))
        self.assertIsNone(_extract_next_data_quotes('<script id="__NEXT_DATA__">{invalid json</script>'))

    def test_yahoo_extract_quote_field_handles_exceptions(self):
        self.assertIsNone(YahooJPRealtimeScraper._extract_quote_field("", "price"))
        with patch("services.realtime.scrapers.re.search", side_effect=RuntimeError("Regex error")):
            self.assertIsNone(YahooJPRealtimeScraper._extract_quote_field("price: 100", "price"))

    def test_sbi_number_after_label_handles_exceptions(self):
        self.assertIsNone(SBISecuritiesScraper._number_after_label("", "現在値"))
        with patch("services.realtime.scrapers.re.search", side_effect=RuntimeError("Regex error")):
            self.assertIsNone(SBISecuritiesScraper._number_after_label("現在値 1,000", "現在値"))

    def test_kabutan_scraper_handles_beautifulsoup_exception(self):
        scraper = YahooJPRealtimeScraper()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html><body>Corrupted</body></html>"

        with patch.object(scraper, "_get_session") as mock_get_sess:
            mock_sess = MagicMock()
            mock_sess.get.return_value = mock_resp
            mock_get_sess.return_value = mock_sess

            with patch("services.realtime.scrapers.BeautifulSoup", side_effect=Exception("DOM parse crash")):
                # Should not raise exception to caller, safe fallback to None
                res = scraper._fetch_kabutan_symbol("7203")
                self.assertIsNone(res)

                res_pts = scraper._fetch_kabutan_pts_symbol("7203")
                self.assertIsNone(res_pts)

    def test_minkabu_scraper_handles_fetch_exception(self):
        scraper = MinkabuScraper()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        type(mock_resp).text = PropertyMock(side_effect=UnicodeDecodeError("utf-8", b"", 0, 1, "bad"))

        with patch.object(scraper, "_get_session") as mock_get_sess:
            mock_sess = MagicMock()
            mock_sess.get.return_value = mock_resp
            mock_get_sess.return_value = mock_sess

            res = scraper.fetch_quote("7203")
            self.assertIsNone(res)

    def test_fetch_regular_with_fallback_safely_catches_primary_exception(self):
        scraper = YahooJPRealtimeScraper()
        with patch.object(scraper, "fetch_jp_symbol", side_effect=RuntimeError("Primary exploded")):
            fallback_mock = MagicMock()
            fallback_mock.fetch_quote.return_value = {
                "symbol": "7203.T",
                "price": 2500.0,
                "change": 10.0,
                "change_percent": 0.4,
                "source": "minkabu",
                "updated_at": 1000.0,
            }
            scraper.fallback_providers = [fallback_mock]
            res = scraper._fetch_regular_with_fallback("7203.T")
            self.assertIsNotNone(res)
            assert res is not None
            self.assertEqual(res["price"], 2500.0)
