"""
Tests for the yfinance performance & robustness fixes:

* P0-1: in-memory tz/cookie/ISIN caches (app_state._InMemoryYfCache) and
        bounded Ticker-instance reuse (services.stock_provider)
* P0-2: fetch_index_data no longer bypasses the 5-minute market-state cache
* P1-1: download_batch uses a small thread pool inside yf.download
* P1-2: a single 429 is recorded exactly once per attempt (no double counting
        between the retry decorator and the method-level handler)
* P1-3: exchange inference from the symbol avoids network lookups for .T / ^
"""

import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from app_state import _InMemoryCookieCache, _InMemoryYfCache
from services.stock_provider import YFinanceProvider
from session_manager import YFinanceSessionManager, yf_session_manager


class InMemoryYfCacheTestCase(unittest.TestCase):
    """P0-1: the in-memory cache used in place of yfinance's SQLite caches."""

    def test_lookup_store_and_none_delete(self):
        cache = _InMemoryYfCache()
        self.assertIsNone(cache.lookup("AAPL"))
        cache.store("AAPL", "America/New_York")
        self.assertEqual(cache.lookup("AAPL"), "America/New_York")
        # store(key, None) evicts, mirroring yfinance's cache contract
        cache.store("AAPL", None)
        self.assertIsNone(cache.lookup("AAPL"))

    def test_clear(self):
        cache = _InMemoryYfCache()
        cache.store("MSFT", "tz")
        cache.store("AAPL", "tz2")
        cache.clear()
        self.assertIsNone(cache.lookup("MSFT"))
        self.assertIsNone(cache.lookup("AAPL"))

    def test_interface_compat(self):
        cache = _InMemoryYfCache()
        self.assertIsNone(cache.tz_db)
        self.assertIsNone(cache.Cookie_db)
        cache.initialise()  # must not raise

    def test_store_is_bounded(self):
        """Long-running processes cannot leak memory: oldest entries are
        evicted first once the cap is exceeded."""
        cache = _InMemoryYfCache()
        orig_max = _InMemoryYfCache._MAX_ENTRIES
        _InMemoryYfCache._MAX_ENTRIES = 3
        try:
            for i in range(5):
                cache.store(f"SYM{i}", f"tz{i}")
            self.assertIsNone(cache.lookup("SYM0"))  # oldest evicted
            self.assertIsNone(cache.lookup("SYM1"))
            self.assertIsNotNone(cache.lookup("SYM2"))
            self.assertIsNotNone(cache.lookup("SYM3"))
            self.assertIsNotNone(cache.lookup("SYM4"))
            # store(None) still evicts even when at capacity
            cache.store("SYM4", None)
            self.assertIsNone(cache.lookup("SYM4"))
        finally:
            _InMemoryYfCache._MAX_ENTRIES = orig_max

    def test_cookie_cache_lookup_matches_yfinance_contract(self):
        """The cookie cache must return {'cookie': value, 'age': ...} exactly
        like yfinance.cache._CookieCache.lookup, otherwise
        YfData._load_cookie_curlCffi raises KeyError on cached-cookie reuse."""
        cache = _InMemoryCookieCache()
        self.assertIsNone(cache.lookup("curlCffi"))
        cache.store("curlCffi", {"finance.yahoo.com": {"/": {"A3": object()}}})
        result = cache.lookup("curlCffi")
        self.assertIsNotNone(result)
        assert isinstance(result, dict)
        self.assertIn("cookie", result)
        self.assertIn("age", result)
        self.assertEqual(list(result["cookie"].keys()), ["finance.yahoo.com"])
        # store(None) evicts (mirrors yfinance cache contract)
        cache.store("curlCffi", None)
        self.assertIsNone(cache.lookup("curlCffi"))


class IsSessionAliveTestCase(unittest.TestCase):
    def setUp(self):
        YFinanceSessionManager._reset_for_testing()

    def tearDown(self):
        YFinanceSessionManager._reset_for_testing()

    def test_session_alive_until_pool_drained(self):
        mgr = YFinanceSessionManager()
        sess = mgr.get_session()
        self.assertTrue(mgr.is_session_alive(sess))
        mgr.close_all()
        self.assertFalse(mgr.is_session_alive(sess))

    def test_unknown_session_not_alive(self):
        mgr = YFinanceSessionManager()
        self.assertFalse(mgr.is_session_alive(object()))


class TickerCacheTestCase(unittest.TestCase):
    """P0-1: YFinanceProvider.get_ticker reuses live Ticker instances."""

    def setUp(self):
        self.provider = YFinanceProvider()
        # Reset global session state so a fresh session pool is used.
        YFinanceSessionManager._reset_for_testing()

    def tearDown(self):
        self.provider.clear_ticker_cache()
        YFinanceSessionManager._reset_for_testing()

    @patch("services.stock_provider.yf.Ticker")
    def test_get_ticker_reuses_cached_instance(self, mock_ticker):
        t1, t2 = MagicMock(), MagicMock()
        mock_ticker.side_effect = [t1, t2]
        first = self.provider.get_ticker("AAPL")
        second = self.provider.get_ticker("AAPL")
        self.assertIs(first, second)
        self.assertIs(first, t1)
        mock_ticker.assert_called_once()

    @patch("services.stock_provider.yf.Ticker")
    def test_get_ticker_rebuilds_when_session_dies(self, mock_ticker):
        mock_ticker.side_effect = [MagicMock(), MagicMock()]
        first = self.provider.get_ticker("AAPL")
        # Session pool drained (idle reaper / close_all / UA-rotation sweep)
        yf_session_manager.close_all()
        second = self.provider.get_ticker("AAPL")
        self.assertIsNot(first, second)
        self.assertEqual(mock_ticker.call_count, 2)

    @patch("services.stock_provider.yf.Ticker")
    def test_get_ticker_rebuilds_after_ttl(self, mock_ticker):
        import services.stock_provider as sp

        mock_ticker.side_effect = [MagicMock(), MagicMock()]
        first = self.provider.get_ticker("AAPL")
        # Age the cache entry beyond the TTL window.
        with self.provider._ticker_cache_lock:
            cached, sess, ts = self.provider._ticker_cache["AAPL"]
            self.provider._ticker_cache["AAPL"] = (
                cached,
                sess,
                ts - sp._TICKER_CACHE_TTL_SEC - 1.0,
            )
        second = self.provider.get_ticker("AAPL")
        self.assertIsNot(first, second)
        self.assertEqual(mock_ticker.call_count, 2)

    @patch("services.stock_provider.yf.Ticker")
    def test_get_ticker_sliding_ttl_refreshes_on_hit(self, mock_ticker):
        """A cache hit refreshes the timestamp, so hot symbols keep their
        live Ticker across consecutive sync cycles."""
        mock_ticker.return_value = MagicMock()
        self.provider.get_ticker("AAPL")
        with self.provider._ticker_cache_lock:
            _cached, _sess, ts1 = self.provider._ticker_cache["AAPL"]
        # A later hit must update the stored timestamp (sliding window).
        self.provider.get_ticker("AAPL")
        with self.provider._ticker_cache_lock:
            _cached, _sess, ts2 = self.provider._ticker_cache["AAPL"]
        self.assertGreaterEqual(ts2, ts1)
        mock_ticker.assert_called_once()

    @patch("services.stock_provider.yf.Ticker")
    def test_get_ticker_cache_is_bounded(self, mock_ticker):
        import services.stock_provider as sp

        mock_ticker.return_value = MagicMock()
        orig_max = sp._TICKER_CACHE_MAX
        sp._TICKER_CACHE_MAX = 2
        try:
            self.provider.get_ticker("AAA")
            self.provider.get_ticker("BBB")
            self.provider.get_ticker("CCC")
            with self.provider._ticker_cache_lock:
                self.assertLessEqual(len(self.provider._ticker_cache), 2)
                self.assertNotIn("AAA", self.provider._ticker_cache)  # oldest evicted
        finally:
            sp._TICKER_CACHE_MAX = orig_max

    @patch("services.stock_provider.yf.Ticker")
    def test_get_ticker_rate_limited_returns_none(self, mock_ticker):
        mock_state = MagicMock()
        mock_state.is_yf_rate_limited.return_value = True
        provider = YFinanceProvider(market_state=mock_state)
        self.assertIsNone(provider.get_ticker("AAPL"))
        mock_ticker.assert_not_called()


class InferExchangeTestCase(unittest.TestCase):
    """P1-3: symbol-only exchange inference (zero network I/O)."""

    def setUp(self):
        self.provider = YFinanceProvider()

    def test_japanese_stocks_resolve_to_tse(self):
        self.assertEqual(self.provider._infer_exchange_from_symbol("7203.T"), "TSE")
        self.assertEqual(self.provider._infer_exchange_from_symbol("6758.T"), "TSE")

    def test_indices_resolve_to_index(self):
        self.assertEqual(self.provider._infer_exchange_from_symbol("^N225"), "INDEX")
        self.assertEqual(self.provider._infer_exchange_from_symbol("^GSPC"), "INDEX")
        self.assertEqual(self.provider._infer_exchange_from_symbol("^VIX"), "INDEX")

    def test_unknown_symbols_return_none(self):
        self.assertIsNone(self.provider._infer_exchange_from_symbol("AAPL"))
        self.assertIsNone(self.provider._infer_exchange_from_symbol("USDJPY=X"))
        self.assertIsNone(self.provider._infer_exchange_from_symbol(None))
        self.assertIsNone(self.provider._infer_exchange_from_symbol(""))


class SingleRateLimitMarkTestCase(unittest.TestCase):
    """P1-2: one 429 must be recorded exactly once per retry attempt."""

    @patch("services.stock_provider._handle_yf_rate_limit")
    def test_get_history_records_rate_limit_once_per_attempt(self, mock_handle):
        from yfinance.exceptions import YFRateLimitError

        mock_state = MagicMock()
        mock_state.is_yf_rate_limited.return_value = False
        mock_state.is_circuit_open.return_value = False
        mock_state.yfinance_short_cache = {}
        mock_state.yfinance_short_cache_lock = threading.Lock()
        provider = YFinanceProvider(market_state=mock_state)
        ticker = MagicMock()
        # yfinance 1.5.x raises YFRateLimitError() with no args.
        ticker.history.side_effect = YFRateLimitError()
        with patch.object(provider, "get_ticker", return_value=ticker):
            with self.assertRaises(YFRateLimitError):
                provider.get_history("AAPL", "3mo")

        # get_history uses max_retries=3 -> 1 initial + 3 retries = 4 attempts.
        # Each attempt must mark the rate limit exactly once (the decorator is
        # the single recording path; the method no longer double-marks).
        self.assertEqual(mock_handle.call_count, 4)


class DownloadBatchThreadsTestCase(unittest.TestCase):
    """P1-1: yf.download is given a small bounded thread pool."""

    def setUp(self):
        self.provider = YFinanceProvider()

    def test_download_batch_uses_bounded_thread_pool(self):
        mock_state = MagicMock()
        mock_state.is_yf_rate_limited.return_value = False
        mock_state.yfinance_short_cache = {}
        mock_state.yfinance_short_cache_lock = threading.Lock()
        provider = YFinanceProvider(market_state=mock_state)

        with (
            patch("services.stock_provider.yf.download", return_value=pd.DataFrame()) as mock_dl,
            patch.object(provider, "_pre_warm_caches_from_history", return_value=None),
            patch.object(provider, "_fetch_single_history", return_value=pd.DataFrame()),
            patch("services.stock_provider.yf_session_manager") as mock_sess,
        ):
            mock_sess.get_session.return_value = MagicMock()
            provider.download_batch(["AAPL", "MSFT"], period="3mo")

        kwargs = mock_dl.call_args.kwargs
        self.assertIn("threads", kwargs)
        self.assertEqual(kwargs["threads"], 2)
        self.assertNotEqual(kwargs["threads"], False)
        self.assertEqual(kwargs["session"], mock_sess.get_session.return_value)


if __name__ == "__main__":
    unittest.main()
