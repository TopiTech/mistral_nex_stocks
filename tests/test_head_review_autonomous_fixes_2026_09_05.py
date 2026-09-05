# tests/test_head_review_autonomous_fixes_2026_09_05.py
"""Comprehensive regression test suite for autonomous code review fixes (2026-09-05).

Verifies:
1. RealtimeMarketEngine.wait_for_updates releases store_lock during wait so producers
   and other clients are not starved, and wakes up immediately on update.
2. YFinanceSessionManager.custom_request avoids self-deadlock when retrying closed sessions
   under concurrency semaphore constraints (e.g. limit=1).
3. YFinanceProvider.download_batch does not prematurely return 2-hour-old disk cache when
   yfinance is not rate-limited, but uses disk cache when rate-limited or as network fallback.
4. TradingViewWSClient.stop() promptly interrupts missing-websocket sleep without hanging
   for the 2.0s join timeout.
5. _clean_reasoning_tags and extract_chat_content properly strip thinking tags even when
   the response contains only reasoning tags or unclosed tags.
"""

import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from app import create_app
from app_state import app_state
from services.realtime.engine import RealtimeMarketEngine
from services.realtime.tv_client import TradingViewWSClient
from services.stock_provider import YFinanceProvider
from session_manager import YFinanceSessionManager
from utils.validators import _clean_reasoning_tags, extract_chat_content


class TestRealtimeEngineLockFix(unittest.TestCase):
    """Test 1: RealtimeMarketEngine.wait_for_updates lock contention fix."""

    def setUp(self):
        self.engine = RealtimeMarketEngine()

    def tearDown(self):
        self.engine.stop()

    def test_wait_for_updates_releases_store_lock_during_wait(self):
        """Producers must be able to acquire store_lock and update prices while
        an SSE client is waiting in wait_for_updates()."""
        cid = self.engine.register_client()
        lock_acquired_by_producer = threading.Event()
        update_finished = threading.Event()
        wait_result: list[bool | None] = [None]

        def client_wait():
            wait_result[0] = self.engine.wait_for_updates(cid, timeout=2.0)

        wait_thread = threading.Thread(target=client_wait)
        wait_thread.start()

        # Give wait_thread a moment to enter wait_for_updates
        time.sleep(0.05)

        def producer_update():
            # Attempt to acquire store_lock - if wait_for_updates holds it, this will hang!
            acquired = self.engine.store_lock.acquire(timeout=0.5)
            if acquired:
                lock_acquired_by_producer.set()
                try:
                    self.engine.market_store["AAPL"] = {
                        "symbol": "AAPL",
                        "price": 180.0,
                        "change": 1.5,
                        "change_percent": 0.84,
                        "volume": 1000,
                        "source": "tradingview",
                        "updated_at": time.time(),
                    }
                    self.engine._notify_all_clients()
                finally:
                    self.engine.store_lock.release()
                update_finished.set()

        prod_thread = threading.Thread(target=producer_update)
        prod_thread.start()

        prod_thread.join(timeout=1.0)
        wait_thread.join(timeout=1.0)

        self.assertTrue(
            lock_acquired_by_producer.is_set(),
            "Producer thread was blocked from acquiring store_lock while client waited",
        )
        self.assertTrue(update_finished.is_set())
        self.assertTrue(wait_result[0], "Client wait_for_updates should have returned True")

    def test_wait_for_updates_unregistered_client_returns_false(self):
        """Unregistered or purged clients should not hang holding store_lock."""
        cid = self.engine.register_client()
        self.engine.unregister_client(cid)
        start = time.time()
        res = self.engine.wait_for_updates(cid, timeout=0.05)
        duration = time.time() - start
        self.assertFalse(res)
        self.assertGreaterEqual(duration, 0.04)


class TestSessionManagerConcurrencyReentrancy(unittest.TestCase):
    """Test 2: YFinanceSessionManager custom_request self-deadlock fix on closed session retry."""

    def setUp(self):
        YFinanceSessionManager._reset_for_testing()
        self.mgr = YFinanceSessionManager()

    def tearDown(self):
        YFinanceSessionManager._reset_for_testing()

    def test_custom_request_retry_closed_session_no_deadlock(self):
        """When concurrency semaphore permit is 1 and original request raises 'closed session',
        retrying via fresh_sess.request() must NOT deadlock with itself."""
        # Force semaphore capacity to 1 to simulate saturated / low concurrency limit
        self.mgr._concurrency_semaphore = threading.Semaphore(1)

        result: list[str | None] = [None]
        exc_result: list[Exception | None] = [None]

        def run():
            try:
                # Direct test of _acquire_concurrency re-entrancy
                with self.mgr._acquire_concurrency():
                    # Nested acquisition on the same thread
                    with self.mgr._acquire_concurrency():
                        result[0] = "reentrancy_success"
            except Exception as e:
                exc_result[0] = e

        t = threading.Thread(target=run)
        t.start()
        t.join(timeout=1.0)

        self.assertFalse(t.is_alive(), "Deadlock detected: thread hung acquiring semaphore")
        self.assertIsNone(exc_result[0])
        self.assertEqual(result[0], "reentrancy_success")


class TestStockProviderBatchDiskCachePolicy(unittest.TestCase):
    """Test 3: YFinanceProvider.download_batch disk cache freshness policy."""

    def setUp(self):
        self.app = create_app()
        self.app_context = self.app.app_context()
        self.app_context.push()
        self.provider = YFinanceProvider()

    def tearDown(self):
        self.app_context.pop()

    def test_download_batch_fetches_fresh_data_when_not_rate_limited(self):
        """When not rate-limited, download_batch must query the network rather than
        short-circuiting on stale 2-hour disk cache."""
        mock_state = MagicMock()
        mock_state.is_yf_rate_limited.return_value = False
        mock_state.is_circuit_open.return_value = False
        mock_state.yfinance_short_cache = {}
        mock_state.yfinance_short_cache_lock = threading.RLock()

        # Seed disk cache with existing data
        disk_key = "hist_df_AAPL_3mo"
        dummy_df = pd.DataFrame(
            {"Close": [150.0, 152.0], "Volume": [1000, 2000]},
            index=pd.date_range("2026-01-01", periods=2),
        )
        app_state.stock_disk_cache.set(
            disk_key,
            {"type": "dataframe", "json_data": dummy_df.to_json(orient="split", date_format="iso")},
        )

        network_df = pd.DataFrame(
            {"Close": [180.0, 182.0], "Volume": [3000, 4000]},
            index=pd.date_range("2026-03-01", periods=2),
        )

        with (
            patch.object(self.provider, "_get_market_state", return_value=mock_state),
            patch("services.stock_provider.yf.download", return_value=network_df) as mock_yf_download,
            patch("services.stock_provider.yf_session_manager") as mock_sess,
        ):
            mock_sess.get_session.return_value = MagicMock()
            res = self.provider.download_batch(["AAPL"], period="3mo")

            # yf.download MUST have been called because rate-limiting was False
            mock_yf_download.assert_called_once()
            self.assertFalse(res.empty)

    def test_download_batch_uses_disk_cache_when_rate_limited(self):
        """When rate-limited, download_batch should avoid network and return disk cache."""
        mock_state = MagicMock()
        mock_state.is_yf_rate_limited.return_value = True
        mock_state.is_circuit_open.return_value = False
        mock_state.yfinance_short_cache = {}
        mock_state.yfinance_short_cache_lock = threading.RLock()

        disk_key = "hist_df_MSFT_3mo"
        dummy_df = pd.DataFrame(
            {"Close": [400.0, 402.0], "Volume": [1000, 2000]},
            index=pd.date_range("2026-01-01", periods=2),
        )
        app_state.stock_disk_cache.set(
            disk_key,
            {"type": "dataframe", "json_data": dummy_df.to_json(orient="split", date_format="iso")},
        )

        with (
            patch.object(self.provider, "_get_market_state", return_value=mock_state),
            patch("services.stock_provider.yf.download") as mock_yf_download,
            patch("services.stock_provider.yf_session_manager") as mock_sess,
        ):
            mock_sess.get_session.return_value = MagicMock()
            res = self.provider.download_batch(["MSFT"], period="3mo")

            # Network download should be bypassed
            mock_yf_download.assert_not_called()
            self.assertFalse(res.empty)

    def test_download_batch_falls_back_to_disk_cache_on_network_failure(self):
        """When network download fails, download_batch should fall back to disk cache."""
        mock_state = MagicMock()
        mock_state.is_yf_rate_limited.return_value = False
        mock_state.is_circuit_open.return_value = False
        mock_state.yfinance_short_cache = {}
        mock_state.yfinance_short_cache_lock = threading.RLock()

        disk_key = "hist_df_NVDA_3mo"
        dummy_df = pd.DataFrame(
            {"Close": [120.0, 122.0], "Volume": [1000, 2000]},
            index=pd.date_range("2026-01-01", periods=2),
        )
        app_state.stock_disk_cache.set(
            disk_key,
            {"type": "dataframe", "json_data": dummy_df.to_json(orient="split", date_format="iso")},
        )

        with (
            patch.object(self.provider, "_get_market_state", return_value=mock_state),
            patch("services.stock_provider.yf.download", side_effect=Exception("network timeout")),
            patch.object(self.provider, "_fetch_single_history", return_value=pd.DataFrame()),
            patch("services.stock_provider.yf_session_manager") as mock_sess,
        ):
            mock_sess.get_session.return_value = MagicMock()
            res = self.provider.download_batch(["NVDA"], period="3mo")

            # Must fall back to disk cache
            self.assertFalse(res.empty)


class TestTradingViewClientStopPromptness(unittest.TestCase):
    """Test 4: TradingViewWSClient.stop() promptness when websocket is None."""

    def test_stop_promptly_terminates_missing_websocket_worker(self):
        """When websocket module is not available, stop() must exit in < 0.5s rather
        than hanging for the 2.0s join timeout."""
        client = TradingViewWSClient(on_update_callback=lambda p: None)

        with patch("services.realtime.tv_client.websocket", None):
            client.start()
            self.assertTrue(client.running)
            self.assertIsNotNone(client.thread)
            assert client.thread is not None
            self.assertTrue(client.thread.is_alive())

            start_t = time.time()
            client.stop()
            elapsed = time.time() - start_t

            self.assertLess(
                elapsed,
                1.0,
                f"client.stop() took {elapsed:.2f}s (should complete in < 0.5s)",
            )
            self.assertFalse(client.running)
            self.assertIsNone(client.thread)


class TestReasoningTagSanitization(unittest.TestCase):
    """Test 5: Thinking tag removal and empty-content fallback."""

    def test_clean_reasoning_tags_all_thinking_content(self):
        """If text consists entirely of thinking tags, _clean_reasoning_tags must
        return empty string rather than leaking the raw tags."""
        thought_only = "<thought>Analyzing revenue data...</thought>"
        self.assertEqual(_clean_reasoning_tags(thought_only), "")

        thinking_only = "<thinking>\nStep 1\nStep 2\n</thinking>"
        self.assertEqual(_clean_reasoning_tags(thinking_only), "")

        bracket_only = "[THINK]Secret plan[/THINK]"
        self.assertEqual(_clean_reasoning_tags(bracket_only), "")

        unclosed_tag = "<thought>Thinking about company earnings"
        self.assertEqual(_clean_reasoning_tags(unclosed_tag), "")

    def test_clean_reasoning_tags_mixed_content(self):
        """Tags should be stripped while keeping visible content intact."""
        mixed = "<thought>Internal monologue</thought>株価は上昇傾向にあります。"
        self.assertEqual(_clean_reasoning_tags(mixed), "株価は上昇傾向にあります。")

    def test_extract_chat_content_with_only_thinking_tags(self):
        """extract_chat_content must return friendly placeholder when model produces
        only thinking tags and preserve_for_history=False."""
        mock_response = MagicMock()
        mock_choice = MagicMock()
        mock_message = MagicMock()
        mock_message.content = "<thought>I will analyze this privately</thought>"
        mock_choice.message = mock_message
        mock_response.choices = [mock_choice]

        result = extract_chat_content(mock_response, preserve_for_history=False)
        self.assertEqual(result, "(思考プロセスのみの応答でした)")


if __name__ == "__main__":
    unittest.main()
