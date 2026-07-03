import unittest
import json
import time
import pandas as pd
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import app
from app_state import app_state
from config_utils import unprotect_data


class SecurityResilienceExtraTestCase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["APPLICATION_ROOT"] = "/"
        self.client = app.test_client()

    def test_shutdown_token_file_is_encrypted_json(self):
        """Verify the shutdown token file is stored as an encrypted JSON structure on disk."""
        # Clean any old token state
        token_file = Path(__file__).resolve().parent.parent / ".mns_shutdown_token"
        used_marker = (
            Path(__file__).resolve().parent.parent / ".mns_shutdown_token.used"
        )
        token_file.unlink(missing_ok=True)
        used_marker.unlink(missing_ok=True)
        app_state.shutdown_manager.shutdown_token = None
        app_state.shutdown_manager.shutdown_token_used = False

        # Generate a new token
        token = app_state.get_or_create_shutdown_token()
        self.assertTrue(token)

        # Read directly from file to verify it is JSON and encrypted
        file_content = token_file.read_text(encoding="utf-8").strip()
        data = json.loads(file_content)
        self.assertIn("scheme", data)
        self.assertIn("value", data)
        self.assertNotEqual(token, data["value"])

        # Decrypt to verify it matches
        decrypted = unprotect_data(data, "shutdown_token")
        self.assertEqual(token, decrypted)

    def test_circuit_breaker_half_open_transitions(self):
        """Test yfinance circuit breaker transitions: CLOSED -> OPEN -> HALF_OPEN -> CLOSED/OPEN."""
        symbol = "TEST_CB_SYM"

        # Reset circuit breaker state for the test symbol
        with app_state.market.history_circuit_lock:
            app_state.market.history_circuit_state.pop(symbol, None)

        class DummyTicker:
            def __init__(self, fail=False):
                self.fail = fail

            def history(self, *args, **kwargs):
                if self.fail:
                    raise TimeoutError("Simulated timeout")
                return pd.DataFrame(
                    {
                        "Open": [100.0],
                        "High": [105.0],
                        "Low": [95.0],
                        "Close": [100.0],
                        "Volume": [1000],
                    },
                    index=pd.to_datetime(["2026-05-21"]),
                )

        # Get context to import or call _history_with_timeout
        # We can simulate calling _history_with_timeout.
        # Since it is defined locally inside api_stock_history, let's extract or mock the logic,
        # or we can test it by calling the route.
        # But wait! We can mock yf.Ticker using safe_get_ticker, or we can test it directly
        # using the _history_with_timeout wrapper if we mock it, or we can simply mock yf.Ticker in yfinance.
        # Let's inspect routes/api_stocks.py to see how ticker history is called.
        # Inside route /api/stock-history:
        # It calls safe_get_ticker(symbol), then _history_with_timeout(t, period, interval)
        # So if we mock safe_get_ticker to return a DummyTicker, we can test it via test_client!

        # Let's mock safe_get_ticker
        import routes.api_stocks as api_stocks_module

        original_safe_get_ticker = api_stocks_module.safe_get_ticker

        ticker_fail = False

        def mock_safe_get_ticker(symbol):
            return DummyTicker(fail=ticker_fail)

        api_stocks_module.safe_get_ticker = mock_safe_get_ticker

        try:
            # 1. Closed state: Successful requests keep it CLOSED
            response = self.client.get(
                f"/api/stock-history?symbol={symbol}&market=us&period=1d"
            )
            self.assertEqual(response.status_code, 200)
            with app_state.market.history_circuit_lock:
                state = app_state.market.history_circuit_state.get(symbol, {})
                self.assertEqual(state.get("status", "CLOSED"), "CLOSED")
                self.assertEqual(state.get("timeout_streak", 0), 0)

            # 2. Trigger timeouts to transition to OPEN
            ticker_fail = True
            from constants import HISTORY_CIRCUIT_BREAKER_THRESHOLD

            for i in range(HISTORY_CIRCUIT_BREAKER_THRESHOLD):
                # Clear the cache first to ensure it actually hits the backend/mock
                from app_helpers import clear_cache_prefix

                clear_cache_prefix(f"hist_{symbol}")
                response = self.client.get(
                    f"/api/stock-history?symbol={symbol}&market=us&period=1d"
                )
                self.assertEqual(response.status_code, 200)

            with app_state.market.history_circuit_lock:
                state = app_state.market.history_circuit_state.get(symbol, {})
                self.assertEqual(state.get("status"), "OPEN")
                self.assertTrue(state.get("open_until", 0.0) > time.time())

            # 3. Request while OPEN should fail fast without calling yfinance
            # Change ticker back to succeed, but it should still fail because circuit is OPEN
            ticker_fail = False
            from app_helpers import clear_cache_prefix

            clear_cache_prefix(f"hist_{symbol}")
            response = self.client.get(
                f"/api/stock-history?symbol={symbol}&market=us&period=1d"
            )
            data = json.loads(response.data)
            self.assertNotIn("history", data)

            # 4. Simulate time passing to open_until to transition to HALF-OPEN
            with app_state.market.history_circuit_lock:
                state = app_state.market.history_circuit_state[symbol]
                state["open_until"] = time.time() - 10  # back in time

            # Now, the next request will transition it to HALF-OPEN and run a test.
            # Since ticker_fail = False, it should succeed and transition to CLOSED.
            clear_cache_prefix(f"hist_{symbol}")
            response = self.client.get(
                f"/api/stock-history?symbol={symbol}&market=us&period=1d"
            )
            self.assertEqual(response.status_code, 200)
            data = json.loads(response.data)
            self.assertIn("history", data)

            with app_state.market.history_circuit_lock:
                state = app_state.market.history_circuit_state.get(symbol, {})
                self.assertEqual(state.get("status"), "CLOSED")
                self.assertEqual(state.get("timeout_streak"), 0)

            # 5. Test failure in HALF-OPEN transitions back to OPEN immediately
            # Trip it again
            ticker_fail = True
            for _ in range(HISTORY_CIRCUIT_BREAKER_THRESHOLD):
                clear_cache_prefix(f"hist_{symbol}")
                self.client.get(
                    f"/api/stock-history?symbol={symbol}&market=us&period=1d"
                )

            with app_state.market.history_circuit_lock:
                state = app_state.market.history_circuit_state.get(symbol, {})
                self.assertEqual(state.get("status"), "OPEN")
                state["open_until"] = time.time() - 10  # back in time

            # Request in HALF-OPEN fails
            clear_cache_prefix(f"hist_{symbol}")
            response = self.client.get(
                f"/api/stock-history?symbol={symbol}&market=us&period=1d"
            )
            data = json.loads(response.data)
            self.assertNotIn("history", data)

            with app_state.market.history_circuit_lock:
                state = app_state.market.history_circuit_state.get(symbol, {})
                # Should trip back to OPEN immediately, streak resets, open_until set
                self.assertEqual(state.get("status"), "OPEN")
                self.assertTrue(state.get("open_until", 0.0) > time.time())

        finally:
            api_stocks_module.safe_get_ticker = original_safe_get_ticker

    def test_yfinance_session_manager_rotation_on_401_and_429(self):
        """Verify that YFinanceSessionManager rotates User-Agents and increments epoch on 401/429 status codes."""
        from unittest.mock import MagicMock, patch
        from app_state import yf_session_manager, CURL_CFFI_AVAILABLE

        # Reset session manager state
        yf_session_manager.close_all()
        yf_session_manager._session_epoch = 0
        yf_session_manager._ua_index = 0

        initial_ua = yf_session_manager.get_user_agent()
        initial_epoch = yf_session_manager._session_epoch

        # Prepare mock response with 401 status code
        mock_resp_401 = MagicMock()
        mock_resp_401.status_code = 401

        patch_path = (
            "curl_cffi.requests.Session.request"
            if CURL_CFFI_AVAILABLE
            else "requests.Session.request"
        )

        # 1. Test 401 Unauthorized/Invalid Crumb rotation
        with patch(patch_path, return_value=mock_resp_401):
            session = yf_session_manager.get_session()
            session.request("GET", "https://query1.finance.yahoo.com/v1/test/getcrumb")

        # UA should be rotated, epoch incremented
        self.assertEqual(yf_session_manager._session_epoch, initial_epoch + 1)
        self.assertNotEqual(yf_session_manager.get_user_agent(), initial_ua)

        # Record UA and epoch after first rotation
        ua_after_401 = yf_session_manager.get_user_agent()
        epoch_after_401 = yf_session_manager._session_epoch

        # Prepare mock response with 429 status code
        mock_resp_429 = MagicMock()
        mock_resp_429.status_code = 429

        # 2. Test 429 Rate Limited rotation
        with patch(patch_path, return_value=mock_resp_429):
            # Fetch session again (since epoch changed, it will instantiate a new one)
            session = yf_session_manager.get_session()
            session.request(
                "GET", "https://query1.finance.yahoo.com/v8/finance/chart/AAPL"
            )

        # UA should be rotated again, epoch incremented again
        self.assertEqual(yf_session_manager._session_epoch, epoch_after_401 + 1)
        self.assertNotEqual(yf_session_manager.get_user_agent(), ua_after_401)

    def test_yfinance_session_manager_requests_spacing_and_serialization(self):
        """Verify that YFinanceSessionManager enforces a minimum of 0.25s spacing between requests."""
        from unittest.mock import MagicMock, patch
        from app_state import yf_session_manager, CURL_CFFI_AVAILABLE

        yf_session_manager.close_all()
        session = yf_session_manager.get_session()

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        patch_path = (
            "curl_cffi.requests.Session.request"
            if CURL_CFFI_AVAILABLE
            else "requests.Session.request"
        )

        with patch(patch_path, return_value=mock_resp):
            t1 = time.time()
            session.request("GET", "https://example.com/1")
            session.request("GET", "https://example.com/2")
            t2 = time.time()

            elapsed = t2 - t1
            # Since min_interval is 0.25s, the second request must have slept for at least ~0.25s.
            self.assertTrue(elapsed >= 0.22, f"Elapsed time was too short: {elapsed}s")


if __name__ == "__main__":
    unittest.main()
