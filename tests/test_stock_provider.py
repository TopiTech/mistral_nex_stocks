"""
Tests for services/stock_provider.py — retry decorator and provider abstraction.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.stock_provider import (
    with_yfinance_retry,
    BaseStockProvider,
    YFinanceProvider,
)


class _RateLimitError(Exception):
    """Simulates a yfinance rate limit error with 'RateLimit' in the name."""
    pass


class WithYFinanceRetryTestCase(unittest.TestCase):
    """with_yfinance_retry デコレータのテスト"""

    def test_success_first_try(self):
        """正常系: 初回で成功する場合はリトライなしで結果を返す"""
        mock_fn = MagicMock(return_value="success")

        @with_yfinance_retry(max_retries=3, base_delay=0.01, backoff_factor=1.0)
        def test_func():
            return mock_fn()

        result = test_func()
        self.assertEqual(result, "success")
        self.assertEqual(mock_fn.call_count, 1)

    def test_retry_on_timeout(self):
        """タイムアウト時にリトライし、最終的に成功する"""
        mock_fn = MagicMock()
        mock_fn.side_effect = [TimeoutError("timeout"), TimeoutError("timeout"), "success"]

        @with_yfinance_retry(max_retries=3, base_delay=0.01, backoff_factor=1.0)
        def test_func():
            return mock_fn()

        result = test_func()
        self.assertEqual(result, "success")
        self.assertEqual(mock_fn.call_count, 3)

    def test_retry_on_connection_error(self):
        """ConnectionError 時にリトライする"""
        mock_fn = MagicMock()
        mock_fn.side_effect = [ConnectionError("refused"), "ok"]

        @with_yfinance_retry(max_retries=2, base_delay=0.01, backoff_factor=1.0)
        def test_func():
            return mock_fn()

        result = test_func()
        self.assertEqual(result, "ok")
        self.assertEqual(mock_fn.call_count, 2)

    def test_raise_on_non_retriable(self):
        """リトライ不可のエラーは即座に再レイズする"""
        mock_fn = MagicMock(side_effect=ValueError("bad data"))

        @with_yfinance_retry(max_retries=2, base_delay=0.01, backoff_factor=1.0)
        def test_func():
            return mock_fn()

        with self.assertRaises(ValueError):
            test_func()
        self.assertEqual(mock_fn.call_count, 1)

    def test_retry_exhausted_raises_last_error(self):
        """全リトライを使い切った場合、最後のエラーを再レイズする"""
        mock_fn = MagicMock(side_effect=TimeoutError("persistent timeout"))

        @with_yfinance_retry(max_retries=2, base_delay=0.01, backoff_factor=1.0)
        def test_func():
            return mock_fn()

        with self.assertRaises(TimeoutError):
            test_func()
        self.assertEqual(mock_fn.call_count, 3)  # 1 initial + 2 retries

    def test_rate_limit_retry(self):
        """RateLimit エラー（クラス名に 'RateLimit' を含む）はリトライする"""
        mock_fn = MagicMock()
        mock_fn.side_effect = [
            _RateLimitError("rate limited"),
            _RateLimitError("rate limited"),
            "success",
        ]

        @with_yfinance_retry(max_retries=3, base_delay=0.01, backoff_factor=1.0)
        def test_func():
            return mock_fn()

        result = test_func()
        self.assertEqual(result, "success")
        self.assertEqual(mock_fn.call_count, 3)

    def test_decorator_without_parentheses(self):
        """@with_yfinance_retry（引数なしの直接適用）でも動作する"""
        mock_fn = MagicMock(return_value=42)

        @with_yfinance_retry
        def test_func():
            return mock_fn()

        result = test_func()
        self.assertEqual(result, 42)
        self.assertEqual(mock_fn.call_count, 1)


class BaseStockProviderTestCase(unittest.TestCase):
    """BaseStockProvider 抽象クラスのテスト"""

    def test_cannot_instantiate_directly(self):
        """BaseStockProvider は直接インスタンス化できない"""
        with self.assertRaises(TypeError):
            BaseStockProvider()  # type: ignore[abstract]


class YFinanceProviderTestCase(unittest.TestCase):
    """YFinanceProvider のテスト（モック使用）"""

    def setUp(self):
        self.provider = YFinanceProvider()

    @patch("services.stock_provider.yf.Ticker")
    def test_get_ticker_success(self, mock_ticker):
        """get_ticker が Ticker インスタンスを返す"""
        mock_ticker.return_value = MagicMock()

        result = self.provider.get_ticker("AAPL")
        self.assertIsNotNone(result)
        mock_ticker.assert_called_once()

    @patch("services.stock_provider.yf.Ticker")
    def test_get_ticker_failure_returns_none(self, mock_ticker):
        """get_ticker がエラー時に None を返す"""
        mock_ticker.side_effect = ValueError("bad symbol")

        result = self.provider.get_ticker("INVALID")
        self.assertIsNone(result)

    def test_search_returns_formatted_results(self):
        """search が整形された結果リストを返す"""
        mock_search_instance = MagicMock()
        mock_search_instance.quotes = [
            {"symbol": "AAPL", "shortname": "Apple Inc.", "exchange": "NMS"},
            {"symbol": "GOOGL", "shortname": "Alphabet Inc.", "exchange": "NMS"},
        ]

        with patch("services.stock_provider.yf.Search", return_value=mock_search_instance):
            results = self.provider.search("apple", max_results=5)

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["symbol"], "AAPL")
        self.assertEqual(results[1]["name"], "Alphabet Inc.")

    @patch("services.stock_provider.yf.Search")
    def test_search_with_short_query(self, mock_search):
        """短すぎるクエリでは空リストを返す"""
        results = self.provider.search("a", max_results=5)
        self.assertEqual(results, [])
        mock_search.assert_not_called()

    @patch("services.stock_provider.yf.Search")
    def test_search_handles_empty_quotes(self, mock_search):
        """quotes が空の場合、空リストを返す"""
        mock_instance = MagicMock()
        mock_instance.quotes = []
        mock_search.return_value = mock_instance

        results = self.provider.search("test", max_results=5)
        self.assertEqual(results, [])

    @patch("services.stock_provider.yf.Search")
    def test_search_handles_missing_symbol(self, mock_search):
        """symbol がないアイテムをスキップする"""
        mock_instance = MagicMock()
        mock_instance.quotes = [
            {"shortname": "No Symbol", "exchange": "NMS"},
        ]
        mock_search.return_value = mock_instance

        results = self.provider.search("test", max_results=5)
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
