"""Coverage tests for utils: storage, chat_history, disk_cache, threading, market_utils time-based paths."""

import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import utils.storage as storage
import utils.chat_history as chat_history
import utils.threading as threading_utils
import utils.market_utils as market_utils
import crypto_utils
from app_state import app_state


class StorageTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = Path(__file__).parent.parent / "test_storage_tmp"
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        self._tmpdir.mkdir(parents=True, exist_ok=True)
        storage.USER_STOCKS_FILE = str(self._tmpdir / "user_stocks.json")
        if os.path.exists(storage.USER_STOCKS_FILE):
            os.remove(storage.USER_STOCKS_FILE)

    def _set_app_stocks(self, us, jp, idx, rate=150.0):
        app_state.market.user_us = us
        app_state.market.user_jp = jp
        app_state.market.user_idx = idx
        app_state.market.last_usdjpy_rate = rate
        # force reload gap
        app_state.market.user_stocks_rev = 5
        app_state.market.last_loaded_rev = 1

    def test_save_and_load_roundtrip(self):
        self._set_app_stocks({"AAPL": {}}, {"7203.T": {}}, {})
        storage.save_user_stocks()
        app_state.market.user_us = {}
        app_state.market.last_loaded_rev = 99  # force reload
        storage.load_user_stocks()
        self.assertIn("AAPL", app_state.market.user_us)
        self.assertIn("7203.T", app_state.market.user_jp)

    def test_load_no_file(self):
        if os.path.exists(storage.USER_STOCKS_FILE):
            os.remove(storage.USER_STOCKS_FILE)
        # should not raise and should leave state unchanged
        app_state.market.last_loaded_rev = app_state.market.user_stocks_rev
        storage.load_user_stocks()

    def test_load_legacy_plaintext(self):
        # A raw dict (not scheme/value encrypted) loads as-is
        data = {"us": {"AAPL": {}}, "jp": {}, "idx": {}, "last_usdjpy_rate": 150.0}
        with open(storage.USER_STOCKS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
        app_state.market.last_loaded_rev = -1
        storage.load_user_stocks()
        self.assertIn("AAPL", app_state.market.user_us)

    def test_load_corrupt_encrypted_backs_up(self):
        # A scheme/value file whose decrypted content is not valid JSON must be
        # backed up and treated as empty (no crash).
        bad = {"scheme": "fernet", "value": "garbage-not-json"}
        with open(storage.USER_STOCKS_FILE, "w", encoding="utf-8") as f:
            json.dump(bad, f, ensure_ascii=False, indent=2)
        app_state.market.last_loaded_rev = -1
        # Force unprotect to return non-JSON so the backup branch is exercised
        with patch.object(crypto_utils, "unprotect_data", return_value="{not valid json"):
            storage.load_user_stocks()
        parent = Path(storage.USER_STOCKS_FILE).parent
        backups = list(parent.glob("user_stocks*.bak.*"))
        self.assertTrue(any(b.exists() for b in backups))

    def test_save_os_error_path(self):
        with patch("utils.storage.os.replace", side_effect=OSError("boom")):
            self._set_app_stocks({}, {}, {})
            # save swallows the error
            storage.save_user_stocks()


class ChatHistoryTestCase(unittest.TestCase):
    def setUp(self):
        self.store = chat_history.SQLiteChatHistoryStore(max_sessions=5, max_msgs_per_session=3)
        # Clean slate
        self.store.clear()

    def test_set_get(self):
        self.store["s1"] = [{"role": "user", "content": "hi"}]
        self.assertEqual(self.store["s1"], [{"role": "user", "content": "hi"}])

    def test_set_get_empty_session(self):
        self.store["empty"] = []
        self.assertEqual(self.store["empty"], [])

    def test_missing_key_raises(self):
        with self.assertRaises(KeyError):
            _ = self.store["does-not-exist"]

    def test_contains(self):
        self.store["s2"] = [{"role": "user", "content": "x"}]
        self.assertIn("s2", self.store)
        self.assertNotIn("nope", self.store)

    def test_add_message(self):
        self.store.add_message("s3", {"role": "user", "content": "a"})
        self.store.add_message("s3", {"role": "assistant", "content": "b"})
        msgs = self.store["s3"]
        self.assertEqual(len(msgs), 2)

    def test_message_limit_enforced(self):
        self.store.max_msgs_per_session = 3
        for i in range(10):
            self.store.add_message("lim", {"role": "user", "content": str(i)})
        msgs = self.store["lim"]
        self.assertLessEqual(len(msgs), 3)

    def test_move_to_end_and_popitem(self):
        self.store["p1"] = [{"role": "user", "content": "1"}]
        self.store.move_to_end("p1")
        self.store.popitem()
        self.assertNotIn("p1", self.store)

    def test_len_and_clear(self):
        self.store["a"] = [{"role": "user", "content": "1"}]
        self.store["b"] = [{"role": "user", "content": "2"}]
        self.assertGreaterEqual(len(self.store), 2)
        self.store.clear()
        self.assertEqual(len(self.store), 0)

    def test_session_limit_enforced(self):
        store = chat_history.SQLiteChatHistoryStore(max_sessions=3, max_msgs_per_session=3)
        store.clear()
        for i in range(10):
            store[f"sess{i}"] = [{"role": "user", "content": str(i)}]
        self.assertLessEqual(len(store), 3)


class ThreadingTestCase(unittest.TestCase):
    def test_submit_and_done(self):
        ex = threading_utils.DaemonThreadPoolExecutor(max_workers=2, thread_name_prefix="t")
        fut = ex.submit(lambda x: x * 2, 21)
        self.assertEqual(fut.result(timeout=2), 42)
        ex.shutdown(wait=True)

    def test_queue_full(self):
        ex = threading_utils.DaemonThreadPoolExecutor(max_workers=1, max_queue_size=1, thread_name_prefix="q")
        # Fill the bounded semaphore: submit more than capacity
        with patch.object(threading_utils.queue, "Full", Exception):
            with patch.object(ex._semaphore, "acquire", return_value=False):
                with self.assertRaises(Exception):
                    ex.submit(lambda: None)
        ex.shutdown(wait=True)

    def test_get_executor_threads_fallback(self):
        ex = threading_utils.DaemonThreadPoolExecutor(max_workers=1, thread_name_prefix="fb")
        ex._thread_name_prefix = "fb"
        with patch.object(ex, "_threads", side_effect=AttributeError):
            # trigger fallback path
            threads = ex._get_executor_threads()
            self.assertIsInstance(threads, list)
        ex.shutdown(wait=True)


class MarketUtilsTestCase(unittest.TestCase):
    def test_is_market_session_open(self):
        from datetime import time as dt_time
        self.assertTrue(market_utils._is_market_session_open(dt_time(10, 0), dt_time(9, 0), dt_time(11, 30)))
        self.assertFalse(market_utils._is_market_session_open(dt_time(12, 0), dt_time(9, 0), dt_time(11, 30)))
        self.assertTrue(market_utils._is_market_session_open(dt_time(13, 0), dt_time(9, 0), dt_time(11, 30), dt_time(12, 30), dt_time(15, 0)))

    def test_market_status_symbol(self):
        self.assertEqual(market_utils._market_status_symbol("jp"), "^N225")
        self.assertEqual(market_utils._market_status_symbol("us"), "^GSPC")
        self.assertEqual(market_utils._market_status_symbol("idx"), "^GSPC")
        self.assertIsNone(market_utils._market_status_symbol("xx"))

    def test_market_state_from_metadata(self):
        self.assertEqual(market_utils._market_state_from_metadata({"marketState": "REGULAR"}), "REGULAR")
        self.assertEqual(market_utils._market_state_from_metadata({"marketState": "CLOSED"}), "CLOSED")
        self.assertIsNone(market_utils._market_state_from_metadata({"marketState": ""}))
        self.assertIsNone(market_utils._market_state_from_metadata(None))
        # currentTradingPeriod path
        now = time.time()
        meta = {"currentTradingPeriod": {"regular": {"start": now - 100, "end": now + 100}}}
        self.assertEqual(market_utils._market_state_from_metadata(meta), "REGULAR")
        meta_closed = {"currentTradingPeriod": {"regular": {"start": now - 200, "end": now - 100}}}
        self.assertEqual(market_utils._market_state_from_metadata(meta_closed), "CLOSED")
        self.assertIsNone(market_utils._market_state_from_metadata({"currentTradingPeriod": {"regular": {}}}))

    def test_acquire_yfinance_slot(self):
        from session_manager import yf_session_manager
        yf_session_manager.clear_rate_limit("yfinance")
        self.assertTrue(market_utils.acquire_yfinance_slot())
        yf_session_manager.mark_rate_limited("yfinance", 30)
        self.assertFalse(market_utils.acquire_yfinance_slot())
        yf_session_manager.clear_rate_limit("yfinance")

    def test_is_market_open_cached_path(self):
        # bypass_cache=False hits cache which will call _fetch_live_market_state; stub it to return CLOSED.
        # ignore_weekend avoids the weekend early-return so the cached value is actually consulted
        # (makes the test deterministic regardless of the real calendar day).
        with patch.object(market_utils, "get_cached", return_value="CLOSED"):
            self.assertFalse(market_utils.is_market_open("us", bypass_cache=False, ignore_weekend=True))
        with patch.object(market_utils, "get_cached", return_value="REGULAR"):
            self.assertTrue(market_utils.is_market_open("jp", bypass_cache=False, ignore_weekend=True))

    def test_is_market_open_time_fallback(self):
        # Force live fetch to fail -> time-based heuristic; weekend -> closed
        import datetime

        class _FakeDateTime:
            @staticmethod
            def now(tz=None):
                return _FakeDateTime._fixed

        with patch.object(market_utils, "_fetch_live_market_state", return_value=None):
            _FakeDateTime._fixed = datetime.datetime(2026, 7, 11, 12, 0, tzinfo=datetime.timezone.utc)
            with patch.object(market_utils, "datetime", _FakeDateTime):
                self.assertFalse(market_utils.is_market_open("us", bypass_cache=True))
                self.assertFalse(market_utils.is_market_open("jp", bypass_cache=True))
        # Weekday open session in US (15:00 UTC == 11:00 EDT, open)
        with patch.object(market_utils, "_fetch_live_market_state", return_value=None):
            _FakeDateTime._fixed = datetime.datetime(2026, 7, 8, 15, 0, tzinfo=datetime.timezone.utc)
            with patch.object(market_utils, "datetime", _FakeDateTime):
                self.assertTrue(market_utils.is_market_open("us", bypass_cache=True))
        # Weekday open session in JP (04:00 UTC == 13:00 JST, afternoon open)
        with patch.object(market_utils, "_fetch_live_market_state", return_value=None):
            _FakeDateTime._fixed = datetime.datetime(2026, 7, 8, 4, 0, tzinfo=datetime.timezone.utc)
            with patch.object(market_utils, "datetime", _FakeDateTime):
                self.assertTrue(market_utils.is_market_open("jp", bypass_cache=True))

    def test_safe_get_ticker(self):
        fake_ticker = object()
        with patch.object(app_state.stock_provider, "get_ticker", return_value=fake_ticker):
            self.assertIs(market_utils.safe_get_ticker("AAPL"), fake_ticker)


if __name__ == "__main__":
    unittest.main()
