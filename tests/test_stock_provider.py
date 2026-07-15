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
    _is_yfinance_rate_limit_error,
    yf,
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


class YFinanceErrorDetectionTestCase(unittest.TestCase):
    """_is_yfinance_rate_limit_error の 401/402/429/439 検出テスト."""

    class _FakeResp:
        def __init__(self, code, body=None):
            self.status_code = code
            self._body = body

        def json(self):
            if self._body is None:
                raise ValueError("no json")
            return self._body

    class _FakeExc(Exception):
        def __init__(self, code=None, body=None, text=""):
            super().__init__(text)
            self.response = (
                YFinanceErrorDetectionTestCase._FakeResp(code, body)
                if code is not None
                else None
            )

    def test_detects_blocking_status_codes(self):
        """401/402/429/439 は全て検出対象。"""
        for code in (401, 402, 429, 439):
            with self.subTest(code=code):
                self.assertTrue(
                    _is_yfinance_rate_limit_error(self._FakeExc(code)),
                    f"status {code} should be detected",
                )

    def test_allows_non_blocking_status_codes(self):
        """200/500 などは検出されない。"""
        for code in (200, 404, 500):
            with self.subTest(code=code):
                self.assertFalse(
                    _is_yfinance_rate_limit_error(self._FakeExc(code)),
                    f"status {code} should NOT be detected",
                )

    def test_detects_yahoo_json_body_code(self):
        """Yahoo の JSON エラー本文内のコード (439/402) も検出する。"""
        self.assertTrue(
            _is_yfinance_rate_limit_error(
                self._FakeExc(200, {"finance": {"error": {"code": "439"}}})
            )
        )
        self.assertTrue(
            _is_yfinance_rate_limit_error(self._FakeExc(200, {"code": 402}))
        )

    def test_detects_block_text_markers(self):
        """本文テキストのブロック系キーワードも検出する。"""
        for text in (
            "HTTP Error 402: Payment Required",
            "your request was denied",
            "temporarily unavailable",
        ):
            with self.subTest(text=text):
                self.assertTrue(
                    _is_yfinance_rate_limit_error(self._FakeExc(None, None, text)),
                    f"text '{text}' should be detected",
                )

    def test_detects_yfinance_yfratelimiterror(self):
        """yfinance 1.5.1 は 429 を YFRateLimitError として投げる。

        この例外は response/status_code 属性を持たず、メッセージのみである。
        型ベースの検知がないと、429 がレート制限として認識されずハンマリング
        を続けてしまう。実際の yfinance 例外型で検証する。
        """
        try:
            from yfinance.exceptions import YFRateLimitError
        except ImportError:
            self.skipTest("yfinance.exceptions.YFRateLimitError not available")
        exc = YFRateLimitError()
        # Guard: ensure the exception carries no status_code/response attr,
        # which is exactly why type-based detection is required.
        self.assertIsNone(getattr(exc, "response", None))
        self.assertTrue(_is_yfinance_rate_limit_error(exc))


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

    @patch("services.stock_provider.yf.Ticker")
    def test_get_fast_info_returns_currency_even_without_previous_close(self, mock_ticker):
        """fast_info に previous_close がなくても currency を返す"""
        mock_ticker_instance = MagicMock()

        class FastInfo:
            currency = "USD"
            market_cap = 123456789
            exchange = "NMS"
            quote_type = "EQUITY"

        mock_ticker_instance.fast_info = FastInfo()
        mock_ticker_instance.info = {"currency": "USD"}
        mock_ticker.return_value = mock_ticker_instance

        result = self.provider.get_fast_info("AAPL")

        self.assertEqual(result["currency"], "USD")
        self.assertEqual(result["marketCap"], 123456789)
        self.assertEqual(result["exchange"], "NMS")
        self.assertEqual(result["quoteType"], "EQUITY")
        self.assertEqual(result["symbol"], "AAPL")
        self.assertNotIn("previousClose", result)

    @patch("services.stock_provider.yf.Ticker")
    def test_get_fast_info_returns_currency_from_history_metadata(self, mock_ticker):
        """fast_info に currency がない場合、history_metadata から取得する"""
        mock_ticker_instance = MagicMock()

        class FastInfo:
            currency = None
            market_cap = 123456789
            exchange = "NMS"
            quote_type = "EQUITY"

        mock_ticker_instance.fast_info = FastInfo()
        mock_ticker_instance.history_metadata = {"currency": "EUR"}
        mock_ticker.return_value = mock_ticker_instance

        result = self.provider.get_fast_info("AAPL")

        self.assertEqual(result["currency"], "EUR")

    def test_infer_currency_from_symbol_forex_and_index(self):
        """_infer_currency_from_symbol が為替ペアや主要インデックスの通貨を正しく推測できる"""
        # Forex symbol inference
        self.assertEqual(self.provider._infer_currency_from_symbol("USDJPY=X"), "JPY")
        self.assertEqual(self.provider._infer_currency_from_symbol("EURUSD=X"), "USD")
        self.assertEqual(self.provider._infer_currency_from_symbol("AUDNZD=X"), "NZD")

        # Major indexes inference
        self.assertEqual(self.provider._infer_currency_from_symbol("^N225"), "JPY")
        self.assertEqual(self.provider._infer_currency_from_symbol("^KS11"), "KRW")
        self.assertEqual(self.provider._infer_currency_from_symbol("^HSI"), "HKD")
        self.assertEqual(self.provider._infer_currency_from_symbol("^FTSE"), "GBP")
        self.assertEqual(self.provider._infer_currency_from_symbol("^STOXX50E"), "EUR")

        # Default fallbacks
        self.assertEqual(self.provider._infer_currency_from_symbol("^GSPC"), "USD")
        self.assertEqual(self.provider._infer_currency_from_symbol("AAPL"), "USD")

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

    # --- Fallback tests (yfinance < 0.2.51) ---

    @patch.object(yf, "Search", None)  # Simulate older yfinance without Search
    def test_search_fallback_when_search_missing(self):
        """yfinance.Search が存在しない場合、フォールバックを使用する"""
        mock_response = MagicMock()
        mock_response.json.return_value = {"quotes": []}
        mock_response.raise_for_status.return_value = None
        mock_session = MagicMock()
        mock_session.get.return_value = mock_response

        with patch("services.stock_provider.yf_session_manager.get_session", return_value=mock_session):
            results = self.provider.search("test", max_results=5)
            self.assertEqual(results, [])

    @patch.object(yf, "Search", None)
    def test_search_fallback_calls_http_endpoint(self):
        """フォールバックが HTTP リクエストを試みる"""
        self.provider._get_market_state = MagicMock()
        mock_state = MagicMock()
        mock_state.is_yf_rate_limited.return_value = False
        self.provider._get_market_state.return_value = mock_state

        # _search_fallback が直接呼ばれることを確認
        original_fallback = self.provider._search_fallback
        with patch.object(self.provider, "_search_fallback", wraps=original_fallback) as mock_fallback:
            self.provider.search("apple", max_results=5)
            mock_fallback.assert_called_once()

    def test_search_fallback_empty_quotes(self):
        """フォールバック: quotes が空の場合、空リストを返す"""
        mock_response = MagicMock()
        mock_response.json.return_value = {"quotes": []}
        mock_response.raise_for_status.return_value = None
        mock_session = MagicMock()
        mock_session.get.return_value = mock_response

        with patch("services.stock_provider.yf_session_manager.get_session", return_value=mock_session):
            results = self.provider._search_fallback("test", 5, MagicMock())
            self.assertEqual(results, [])

    def test_search_fallback_returns_formatted_results(self):
        """フォールバックが整形された結果リストを返す"""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "quotes": [
                {"symbol": "AAPL", "shortname": "Apple Inc.", "exchange": "NMS"},
                {"symbol": "GOOGL", "shortname": "Alphabet Inc.", "exchange": "NMS"},
            ]
        }
        mock_response.raise_for_status.return_value = None
        mock_session = MagicMock()
        mock_session.get.return_value = mock_response

        with patch("services.stock_provider.yf_session_manager.get_session", return_value=mock_session):
            results = self.provider._search_fallback("apple", 5, MagicMock())

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["symbol"], "AAPL")
        self.assertEqual(results[1]["name"], "Alphabet Inc.")

    def test_search_fallback_skips_missing_symbol(self):
        """フォールバック: symbol がないアイテムをスキップする"""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "quotes": [
                {"shortname": "No Symbol", "exchange": "NMS"},
            ]
        }
        mock_response.raise_for_status.return_value = None
        mock_session = MagicMock()
        mock_session.get.return_value = mock_response

        with patch("services.stock_provider.yf_session_manager.get_session", return_value=mock_session):
            results = self.provider._search_fallback("test", 5, MagicMock())
            self.assertEqual(results, [])

    def test_search_fallback_handles_http_error(self):
        """フォールバック: HTTPエラー時に空リストを返す"""
        mock_session = MagicMock()
        mock_session.get.side_effect = ConnectionError("HTTP error")

        with patch("services.stock_provider.yf_session_manager.get_session", return_value=mock_session):
            results = self.provider._search_fallback("test", 5, MagicMock())
            self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
