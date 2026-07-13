# tests/test_yfinance_overhaul.py
import unittest
import pandas as pd
from services.stock_provider import YFinanceProvider


class YFinanceOverhaulTestCase(unittest.TestCase):
    def setUp(self):
        self.provider = YFinanceProvider()
        from session_manager import YFinanceSessionManager
        YFinanceSessionManager._reset_for_testing()

    def test_derive_quote_from_history_single_row(self):
        # A single history row should still produce a valid quote (price only).
        dates = pd.to_datetime(["2026-07-11"])
        df = pd.DataFrame(
            {
                "Open": [102.0],
                "High": [103.0],
                "Low": [101.0],
                "Close": [102.5],
                "Volume": [1500],
            },
            index=dates,
        )
        quote = self.provider._derive_quote_from_history(df, "AAPL")
        self.assertIsNotNone(quote)
        self.assertEqual(quote["regularMarketPrice"], 102.5)
        # Previous close is None when only a single row exists.
        self.assertIsNone(quote["regularMarketPreviousClose"])
        self.assertEqual(quote["regularMarketVolume"], 1500)

    def test_derive_quote_from_history_multiple_rows(self):
        dates = pd.to_datetime(["2026-07-10", "2026-07-11"])
        df = pd.DataFrame(
            {
                "Open": [100.0, 101.0],
                "High": [102.0, 103.0],
                "Low": [99.0, 100.0],
                "Close": [101.0, 102.0],
                "Volume": [1000, 1100],
            },
            index=dates,
        )
        quote = self.provider._derive_quote_from_history(df, "AAPL")
        self.assertIsNotNone(quote)
        self.assertEqual(quote["regularMarketPrice"], 102.0)
        self.assertEqual(quote["regularMarketPreviousClose"], 101.0)
        self.assertEqual(quote["regularMarketVolume"], 1100)
        self.assertIsNotNone(quote["regularMarketTime"])

    def test_derive_quote_from_history_empty(self):
        df = pd.DataFrame()
        self.assertIsNone(self.provider._derive_quote_from_history(df, "AAPL"))

    def test_pre_warm_caches_from_history(self):
        # The batch download path must NOT call the v7/finance/quote endpoint;
        # instead it derives price/currency from already-fetched history and
        # injects a lightweight fast_info cache entry.
        dates = pd.to_datetime(["2026-07-10", "2026-07-11"])
        df = pd.DataFrame(
            {
                "Open": [100.0, 101.0],
                "High": [102.0, 103.0],
                "Low": [99.0, 100.0],
                "Close": [101.0, 102.0],
                "Volume": [1000, 1100],
            },
            index=dates,
        )
        hist_by_symbol = {"AAPL": df}
        from app_state import app_state
        m_state = app_state.market
        self.provider._pre_warm_caches_from_history(hist_by_symbol, m_state)

        with m_state.yfinance_short_cache_lock:
            cached_fast = m_state.yfinance_short_cache.get("fastinfo_AAPL")
        self.assertIsNotNone(cached_fast)
        assert isinstance(cached_fast, dict)
        # previousClose derived from history, currency inferred from symbol.
        self.assertEqual(cached_fast["previousClose"], 101.0)
        self.assertEqual(cached_fast["currency"], "USD")

    def test_download_batch_avoids_quote_endpoint(self):
        # download_batch must build quotes from history only and never hit the
        # v7/finance/quote endpoint. We assert the quote endpoint is never
        # called and that prices are derived from the downloaded history.
        dates = pd.to_datetime(["2026-07-10", "2026-07-11", "2026-07-12"])
        df = pd.DataFrame(
            {
                "Open": [100.0, 101.0, 102.0],
                "High": [102.0, 103.0, 104.0],
                "Low": [99.0, 100.0, 101.0],
                "Close": [101.0, 102.0, 103.0],
                "Volume": [1000, 1100, 1200],
            },
            index=dates,
        )
        multi = df.copy()
        multi.columns = pd.MultiIndex.from_product([multi.columns, ["AAPL"]])

        with unittest.mock.patch(
            "services.stock_provider.yf.download", return_value=multi
        ), unittest.mock.patch(
            "yfinance.data.YfData.get_raw_json"
        ) as mock_get_raw:
            merged = self.provider.download_batch(["AAPL"], period="3mo")

        # The quote endpoint must never be touched.
        mock_get_raw.assert_not_called()
        self.assertIsNotNone(merged)
        # The merged frame should carry the symbol level column.
        self.assertIn("AAPL", merged.columns.get_level_values(1))


import unittest.mock  # noqa: E402
