# tests/test_yfinance_overhaul.py
import unittest
from unittest.mock import patch
import pandas as pd
import time
from services.stock_provider import YFinanceProvider
from app_state import app_state

class YFinanceOverhaulTestCase(unittest.TestCase):
    def setUp(self):
        self.provider = YFinanceProvider()
        from session_manager import YFinanceSessionManager
        YFinanceSessionManager._reset_for_testing()
        
    def test_merge_quote_into_history_same_day(self):
        # 過去履歴データを作成
        dates = pd.to_datetime(["2026-07-10", "2026-07-11"])
        df = pd.DataFrame({
            "Open": [100.0, 101.0],
            "High": [102.0, 103.0],
            "Low": [99.0, 100.0],
            "Close": [101.0, 102.0],
            "Volume": [1000, 1100]
        }, index=dates)

        # 本日の quote 情報 (同じ日付 2026-07-11 の更新を想定)
        quote = {
            "regularMarketPrice": 105.0,
            "regularMarketDayHigh": 106.0,
            "regularMarketDayLow": 104.0,
            "regularMarketOpen": 102.0,
            "regularMarketVolume": 1500,
            "regularMarketTime": int(time.mktime(time.strptime("2026-07-11 15:00:00", "%Y-%m-%d %H:%M:%S")))
        }

        merged = self.provider._merge_quote_into_history(df, quote, "AAPL")
        self.assertEqual(len(merged), 2)  # 行数は増えない
        self.assertEqual(merged["Close"].iloc[-1], 105.0)
        self.assertEqual(merged["Volume"].iloc[-1], 1500)
        self.assertEqual(merged["High"].iloc[-1], 106.0)

    def test_merge_quote_into_history_new_day(self):
        # 過去履歴データを作成
        dates = pd.to_datetime(["2026-07-10", "2026-07-11"])
        df = pd.DataFrame({
            "Open": [100.0, 101.0],
            "High": [102.0, 103.0],
            "Low": [99.0, 100.0],
            "Close": [101.0, 102.0],
            "Volume": [1000, 1100]
        }, index=dates)

        # 翌日の quote 情報 (2026-07-12)
        quote = {
            "regularMarketPrice": 108.0,
            "regularMarketDayHigh": 109.0,
            "regularMarketDayLow": 107.0,
            "regularMarketOpen": 105.0,
            "regularMarketVolume": 2000,
            "regularMarketTime": int(time.mktime(time.strptime("2026-07-12 15:00:00", "%Y-%m-%d %H:%M:%S")))
        }

        merged = self.provider._merge_quote_into_history(df, quote, "AAPL")
        self.assertEqual(len(merged), 3)  # 行数が増える
        self.assertEqual(merged["Close"].iloc[-1], 108.0)
        self.assertEqual(merged.index[-1].strftime("%Y-%m-%d"), "2026-07-12")

    @patch("yfinance.data.YfData.get_raw_json")
    def test_fetch_quotes_batch_success(self, mock_get_raw):
        # モックレスポンスの定義
        mock_get_raw.return_value = {
            "quoteResponse": {
                "result": [
                    {"symbol": "AAPL", "regularMarketPrice": 180.0, "shortName": "Apple"},
                    {"symbol": "MSFT", "regularMarketPrice": 400.0, "shortName": "Microsoft"}
                ]
            }
        }

        quotes = self.provider.fetch_quotes_batch(["AAPL", "MSFT"])
        self.assertIn("AAPL", quotes)
        self.assertIn("MSFT", quotes)
        self.assertEqual(quotes["AAPL"]["regularMarketPrice"], 180.0)
        self.assertEqual(quotes["MSFT"]["shortName"], "Microsoft")

    def test_pre_warm_caches(self):
        quotes = {
            "AAPL": {
                "symbol": "AAPL",
                "regularMarketPrice": 180.0,
                "regularMarketPreviousClose": 178.0,
                "currency": "USD",
                "trailingPE": 28.5,
                "earningsTimestamp": int(time.time() + 86400)
            }
        }
        
        m_state = app_state.market
        self.provider._pre_warm_caches_from_quotes(quotes, m_state)
        
        # インメモリキャッシュの確認
        with m_state.yfinance_short_cache_lock:
            cached_fast = m_state.yfinance_short_cache.get("fastinfo_AAPL")
            cached_info = m_state.yfinance_short_cache.get("info_short_AAPL")
            
        self.assertIsNotNone(cached_fast)
        self.assertEqual(cached_fast["previousClose"], 178.0)
        
        self.assertIsNotNone(cached_info)
        self.assertEqual(cached_info["trailingPE"], 28.5)
        
        # グローバルキャッシュの確認
        from utils.caching import _get_cached_value
        global_cal = _get_cached_value("cal_AAPL", 3600)
        self.assertIsNotNone(global_cal)
        self.assertIn("Earnings Date", global_cal)
