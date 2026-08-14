"""
test_coverage_stock_provider.py - Thorough coverage tests for services/stock_provider.py
"""

import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

from services.stock_provider import YFinanceProvider, _is_yfinance_rate_limit_error


class StockProviderCoverageTestCase(unittest.TestCase):
    def setUp(self):
        self.provider = YFinanceProvider()

    def test_rate_limit_error_detection(self):
        self.assertTrue(_is_yfinance_rate_limit_error(Exception("429 Too Many Requests")))
        self.assertTrue(_is_yfinance_rate_limit_error(Exception("Rate limited. Try after a while")))
        self.assertTrue(_is_yfinance_rate_limit_error(Exception("401 Unauthorized Invalid Crumb")))
        self.assertFalse(_is_yfinance_rate_limit_error(ValueError("Invalid syntax")))

    def test_df_to_records_empty_and_none(self):
        self.assertEqual(self.provider._df_to_records(None), [])
        self.assertEqual(self.provider._df_to_records(pd.DataFrame()), [])

    def test_df_to_records_datetime_index_and_nan(self):
        dates = pd.date_range("2026-01-01", periods=3, freq="D")
        df = pd.DataFrame(
            {"val": [1.0, np.nan, 3.0], "text": ["a", "b", None]},
            index=dates,
        )
        records = self.provider._df_to_records(df, limit=2)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["val"], 1.0)
        self.assertIsNone(records[1]["val"])

    def test_derive_quote_from_history_valid_and_edge_cases(self):
        self.assertIsNone(self.provider._derive_quote_from_history(None, "TEST"))
        self.assertIsNone(self.provider._derive_quote_from_history(pd.DataFrame(), "TEST"))

        # Single row
        df_single = pd.DataFrame(
            {"Open": [10.0], "High": [12.0], "Low": [9.0], "Close": [11.0], "Volume": [5000]},
            index=pd.to_datetime(["2026-01-01"]),
        )
        q1 = self.provider._derive_quote_from_history(df_single, "TEST")
        self.assertIsNotNone(q1)
        self.assertEqual(q1["regularMarketPrice"], 11.0)
        self.assertIsNone(q1["regularMarketPreviousClose"])
        self.assertEqual(q1["regularMarketVolume"], 5000)

        # Multiple rows
        df_multi = pd.DataFrame(
            {
                "Open": [9.0, 10.0],
                "High": [10.0, 12.0],
                "Low": [8.5, 9.5],
                "Close": [9.5, 11.5],
                "Volume": [1000, 2000],
            },
            index=pd.to_datetime(["2026-01-01", "2026-01-02"]),
        )
        q2 = self.provider._derive_quote_from_history(df_multi, "TEST")
        self.assertIsNotNone(q2)
        self.assertEqual(q2["regularMarketPrice"], 11.5)
        self.assertEqual(q2["regularMarketPreviousClose"], 9.5)

    def test_merge_quote_into_history(self):
        self.assertTrue(self.provider._merge_quote_into_history(None, {}, "TEST").empty)
        ts = pd.Timestamp("2026-01-01 12:00:00", tz="America/New_York")
        df = pd.DataFrame(
            {"Open": [10.0], "High": [11.0], "Low": [9.0], "Close": [10.5], "Volume": [100]},
            index=pd.DatetimeIndex([ts.strftime("%Y-%m-%d")]),
        )
        # Empty quote returns unchanged df
        res1 = self.provider._merge_quote_into_history(df, {}, "TEST")
        self.assertEqual(len(res1), 1)

        # Merge same date update
        quote_same = {
            "regularMarketPrice": 12.0,
            "regularMarketVolume": 200,
            "regularMarketTime": ts.timestamp(),
        }
        res2 = self.provider._merge_quote_into_history(df, quote_same, "TEST")
        self.assertEqual(len(res2), 1)
        self.assertEqual(res2["Close"].iloc[-1], 12.0)

        # Merge new date
        ts_new = pd.Timestamp("2026-01-02 12:00:00", tz="America/New_York")
        quote_new = {
            "regularMarketPrice": 13.0,
            "regularMarketVolume": 300,
            "regularMarketTime": ts_new.timestamp(),
        }
        res3 = self.provider._merge_quote_into_history(df, quote_new, "TEST")
        self.assertEqual(len(res3), 2)
        self.assertEqual(res3["Close"].iloc[-1], 13.0)

    def test_pre_warm_caches_from_history(self):
        m_state = MagicMock()
        df = pd.DataFrame(
            {"Open": [10.0], "High": [11.0], "Low": [9.0], "Close": [10.5], "Volume": [100]},
            index=pd.to_datetime(["2026-01-01"]),
        )
        with patch.object(self.provider, "get_ticker", return_value=None):
            self.provider._pre_warm_caches_from_history({"AAPL": df, "7203.T": df}, m_state)

    def test_get_ticker_info_and_auxiliary_data(self):
        mock_ticker = MagicMock()
        mock_ticker.info = {
            "trailingPE": 25.5,
            "forwardPE": 22.0,
            "marketCap": 2000000000,
            "sector": "Technology",
        }
        mock_ticker.fast_info = MagicMock()
        mock_ticker.fast_info.last_price = 150.0
        mock_ticker.fast_info.previous_close = 148.0
        mock_ticker.fast_info.currency = "USD"
        mock_ticker.fast_info.exchange = "NMS"

        sample_df = pd.DataFrame({"col": [1, 2]}, index=pd.date_range("2026-01-01", periods=2))
        mock_ticker.get_earnings_dates.return_value = sample_df
        mock_ticker.get_recommendations.return_value = sample_df
        mock_ticker.get_institutional_holders.return_value = sample_df
        mock_ticker.get_major_holders.return_value = sample_df
        mock_ticker.get_analyst_price_targets.return_value = {"mean": 180.0, "high": 200.0, "low": 160.0}
        mock_ticker.get_calendar.return_value = {"Earnings Date": ["2026-04-20"]}
        mock_ticker.get_news.return_value = [{"content": {"title": "Apple Q2 results", "provider": {"displayName": "Reuters"}}}]
        mock_ticker.options = ("2026-06-19",)
        chain_mock = MagicMock()
        chain_mock.calls = sample_df
        chain_mock.puts = sample_df
        mock_ticker.option_chain.return_value = chain_mock
        mock_ticker.get_revenue_estimate.return_value = sample_df
        mock_ticker.get_earnings_estimate.return_value = sample_df
        mock_ticker.get_valuation_measures.return_value = sample_df

        with patch.object(self.provider, "get_ticker", return_value=mock_ticker):
            # Test get_info
            funds = self.provider.get_info("AAPL")
            self.assertEqual(funds.get("trailingPE"), 25.5)

            # Test get_fast_info
            finfo = self.provider.get_fast_info("AAPL")
            self.assertEqual(finfo.get("currency"), "USD")

            # Test earnings dates
            ed = self.provider.get_earnings_dates("AAPL")
            self.assertEqual(len(ed), 2)

            # Test recommendations
            rec = self.provider.get_recommendations("AAPL")
            self.assertEqual(len(rec), 2)

            # Test institutional holders
            ih = self.provider.get_institutional_holders("AAPL")
            self.assertEqual(len(ih), 2)

            # Test major holders
            mh = self.provider.get_major_holders("AAPL")
            self.assertIsInstance(mh, (dict, list))

            # Test analyst targets
            at = self.provider.get_analyst_targets("AAPL")
            self.assertEqual(at.get("mean"), 180.0)

            # Test calendar
            cal = self.provider.get_calendar("AAPL")
            self.assertIn("Earnings Date", cal)

            # Test news
            news = self.provider.get_news("AAPL")
            self.assertEqual(len(news), 1)
            self.assertEqual(news[0]["title"], "Apple Q2 results")

            # Test option chain
            oc = self.provider.get_option_chain("AAPL")
            self.assertEqual(oc.get("expiry"), "2026-06-19")
            self.assertIn("calls", oc)

            # Test revenue estimate
            rev = self.provider.get_revenue_estimate("AAPL")
            self.assertEqual(len(rev), 2)

            # Test earnings estimate
            ee = self.provider.get_earnings_estimate("AAPL")
            self.assertEqual(len(ee), 2)

            # Test valuation measures
            vm = self.provider.get_valuation_measures("AAPL")
            self.assertEqual(len(vm), 2)

    def test_auxiliary_methods_error_and_none_ticker(self):
        with patch.object(self.provider, "get_ticker", return_value=None):
            self.assertEqual(self.provider.get_info("NONEXISTENT"), {})
            self.assertEqual(self.provider.get_fast_info("NONEXISTENT"), {})
            self.assertEqual(self.provider.get_earnings_dates("NONEXISTENT"), [])
            self.assertEqual(self.provider.get_recommendations("NONEXISTENT"), [])
            self.assertEqual(self.provider.get_institutional_holders("NONEXISTENT"), [])
            self.assertEqual(self.provider.get_major_holders("NONEXISTENT"), {})
            self.assertEqual(self.provider.get_analyst_targets("NONEXISTENT"), {})
            self.assertEqual(self.provider.get_calendar("NONEXISTENT"), {})
            self.assertEqual(self.provider.get_news("NONEXISTENT"), [])
            self.assertEqual(self.provider.get_option_chain("NONEXISTENT"), {})
            self.assertEqual(self.provider.get_revenue_estimate("NONEXISTENT"), [])
            self.assertEqual(self.provider.get_earnings_estimate("NONEXISTENT"), [])
            self.assertEqual(self.provider.get_valuation_measures("NONEXISTENT"), [])

    def test_auxiliary_methods_handle_rate_limit_exceptions(self):
        mock_ticker = MagicMock()
        rate_err = Exception("429 Too Many Requests Rate limit exceeded")
        mock_ticker.get_earnings_dates.side_effect = rate_err
        mock_ticker.get_recommendations.side_effect = rate_err
        mock_ticker.get_institutional_holders.side_effect = rate_err
        mock_ticker.get_major_holders.side_effect = rate_err
        mock_ticker.get_analyst_price_targets.side_effect = rate_err
        mock_ticker.get_calendar.side_effect = rate_err
        mock_ticker.get_news.side_effect = rate_err
        mock_ticker.option_chain.side_effect = rate_err
        mock_ticker.get_revenue_estimate.side_effect = rate_err
        mock_ticker.get_earnings_estimate.side_effect = rate_err
        mock_ticker.get_valuation_measures.side_effect = rate_err

        with patch.object(self.provider, "get_ticker", return_value=mock_ticker):
            self.assertEqual(self.provider.get_earnings_dates("AAPL"), [])
            self.assertEqual(self.provider.get_recommendations("AAPL"), [])
            self.assertEqual(self.provider.get_institutional_holders("AAPL"), [])
            self.assertEqual(self.provider.get_major_holders("AAPL"), {})
            self.assertEqual(self.provider.get_analyst_targets("AAPL"), {})
            self.assertEqual(self.provider.get_calendar("AAPL"), {})
            self.assertEqual(self.provider.get_news("AAPL"), [])
            self.assertEqual(self.provider.get_option_chain("AAPL"), {})
            self.assertEqual(self.provider.get_revenue_estimate("AAPL"), [])
            self.assertEqual(self.provider.get_earnings_estimate("AAPL"), [])
            self.assertEqual(self.provider.get_valuation_measures("AAPL"), [])

    def test_search_and_fallback(self):
        # Short query
        self.assertEqual(self.provider.search("a"), [])

        # Mock fallback search
        m_state = MagicMock()
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "quotes": [
                {"symbol": "AAPL", "shortname": "Apple Inc", "exchange": "NMS"},
                {"symbol": "MSFT", "longname": "Microsoft Corp", "exchDisp": "NASDAQ"},
            ]
        }
        mock_session.get.return_value = mock_resp
        with patch("session_manager.yf_session_manager.get_session", return_value=mock_session):
            res = self.provider._search_fallback("Apple", 5, m_state)
            self.assertEqual(len(res), 2)
            self.assertEqual(res[0]["symbol"], "AAPL")
            self.assertEqual(res[1]["symbol"], "MSFT")


if __name__ == "__main__":
    unittest.main()
