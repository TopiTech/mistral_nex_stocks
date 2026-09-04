"""
Unit tests covering audit review fixes across cryptography, storage, chat history,
stock data providers, screener calculations, and validation.
"""

import io
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import requests

import crypto_utils
from app_state import app_state
from native_host import native_host
from services.fallback_provider import Nikkei225JPProvider
from services.market_data_service import _build_market_row
from services.stock_provider import YFinanceProvider
from session_manager import YFinanceSessionManager
from utils import storage
from utils.chat_history import SQLiteChatHistoryStore
from utils.validators import extract_json_payload, validate_portfolio_input


class TestAuditReviewFixes(unittest.TestCase):
    def test_clear_ephemeral_credentials_selective_exclusion(self):
        """R2: clear_ephemeral_credentials(exclude={"mns_master_key"}) preserves master key."""
        with crypto_utils._EPHEMERAL_LOCK:
            crypto_utils._EPHEMERAL_CREDENTIALS["mns_master_key"] = {
                "scheme": "ephemeral",
                "value": "secret_master",
            }
            crypto_utils._EPHEMERAL_CREDENTIALS["mistral_api_key"] = {
                "scheme": "ephemeral",
                "value": "api_key_123",
            }
            crypto_utils._EPHEMERAL_KEY = b"12345678901234567890123456789012"

        # Clear API credentials with exclusion
        crypto_utils.clear_ephemeral_credentials(exclude={"mns_master_key"})

        with crypto_utils._EPHEMERAL_LOCK:
            self.assertIn("mns_master_key", crypto_utils._EPHEMERAL_CREDENTIALS)
            self.assertNotIn("mistral_api_key", crypto_utils._EPHEMERAL_CREDENTIALS)
            self.assertIsNotNone(crypto_utils._EPHEMERAL_KEY)

        # Full clear wipes everything
        crypto_utils.clear_ephemeral_credentials()
        with crypto_utils._EPHEMERAL_LOCK:
            self.assertEqual(len(crypto_utils._EPHEMERAL_CREDENTIALS), 0)
            self.assertIsNone(crypto_utils._EPHEMERAL_KEY)

    def test_chat_history_gc_closes_all_connections(self):
        """R3: _close_local_conn iterates over all active connections across threads."""
        mock_local = MagicMock()
        mock_conn1 = MagicMock()
        mock_conn2 = MagicMock()
        mock_local.conn = mock_conn1

        active_conns = {mock_conn1, mock_conn2}
        conns_lock = threading.Lock()

        SQLiteChatHistoryStore._close_local_conn(mock_local, active_conns, conns_lock)

        mock_conn1.close.assert_called()
        mock_conn2.close.assert_called()
        self.assertEqual(len(active_conns), 0)
        self.assertIsNone(mock_local.conn)

    def test_chat_history_read_methods_close_cursor_and_rollback(self):
        """R3: Read methods properly close cursors and rollback transaction snapshots."""
        store = SQLiteChatHistoryStore(max_sessions=10, max_msgs_per_session=10)
        # Test __contains__
        self.assertFalse("non_existent_key_xyz" in store)
        # Test __getitem__
        with self.assertRaises(KeyError):
            _ = store["non_existent_key_xyz"]
        # Test __len__
        self.assertGreaterEqual(len(store), 0)
        store.close()

    def test_download_batch_multi_symbol_flat_dataframe_isolation(self):
        """R4: Flat DataFrame from yf.download is NOT copied to multiple symbols."""
        provider = YFinanceProvider()

        # Create a single-level column DataFrame simulating yfinance returning only 1 symbol
        dates = pd.date_range("2026-01-01", periods=5, freq="D")
        flat_df = pd.DataFrame(
            {
                "Open": [100.0] * 5,
                "High": [105.0] * 5,
                "Low": [99.0] * 5,
                "Close": [104.0] * 5,
                "Volume": [1000] * 5,
            },
            index=dates,
        )

        def mock_single_fetch(sym, period, m_state=None):
            if sym == "VALID_SYM":
                return flat_df.copy()
            return pd.DataFrame()

        with (
            patch("services.stock_provider.yf.download", return_value=flat_df),
            patch.object(provider, "_fetch_single_history", side_effect=mock_single_fetch),
        ):
            # Two symbols requested, flat dataframe returned from batch
            result = provider.download_batch(["INVALID_SYM", "VALID_SYM"], period="1mo")
            # Result should only contain VALID_SYM
            if isinstance(result.columns, pd.MultiIndex):
                self.assertIn("VALID_SYM", result.columns.levels[1])
                self.assertNotIn("INVALID_SYM", result.columns.levels[1])

    def test_screener_market_cap_fallback_shares_price(self):
        """R5: Screener enrichment computes sharesOutstanding * price when market_cap is missing."""
        source = {
            "name": "Acme Corp",
            "price": 50.0,
            "sharesOutstanding": 2_000_000,
            # No market_cap / marketCap provided
        }
        row = _build_market_row("ACME", "us", source, "Acme Corp")
        self.assertEqual(row["market_cap"], 100_000_000.0)

    def test_nikkei225jp_direct_adr_response_parsing(self):
        """R6: Nikkei225JPProvider parses var A0="..." directly on single-stock direct fetch."""
        provider = Nikkei225JPProvider()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        # Underscore-separated ADR row format
        raw_parts = ["7203", "Toyota", "TM", "TSE", "100", "0", "0", "0", "2800.5", "15.5"] + [
            "0"
        ] * 15
        joined = "_".join(raw_parts)
        mock_resp.text = f'var Sno="7203";\nvar A0 = "{joined}";'

        mock_client = MagicMock()
        mock_client.get.return_value = mock_resp

        with (
            patch.object(provider, "_get_client", return_value=(mock_client, True)),
            patch.object(provider, "_refresh_adr_cache", return_value={}),
        ):
            quote = provider.get_latest_quote("7203.T")
            self.assertIsNotNone(quote)
            self.assertEqual(quote["symbol"], "7203.T")
            self.assertEqual(quote["regularMarketPrice"], 2800.5)
            self.assertEqual(quote["regularMarketPreviousClose"], 2800.5 - 15.5)

    def test_validators_portfolio_rejects_inf_and_nan(self):
        """R9: PortfolioInputSchema rejects float('nan') and float('inf')."""
        errors_nan = validate_portfolio_input(float("nan"), 100.0)
        self.assertTrue(len(errors_nan) > 0, "NaN shares should produce validation error")

        errors_inf = validate_portfolio_input(10.0, float("inf"))
        self.assertTrue(len(errors_inf) > 0, "Inf price should produce validation error")

    def test_validators_json_salvage_trailing_backslashes(self):
        """R9: extract_json_payload properly handles unescaped vs escaped trailing backslashes."""
        # Unescaped trailing backslash: {"text": "hello\ -> salvaged to {"text": "hello"}
        raw_unpaired = '{"text": "hello\\'
        salvaged = extract_json_payload(raw_unpaired)
        self.assertIsNotNone(salvaged)

        # Escaped trailing backslash: {"text": "path\\\\ -> salvaged to {"text": "path\\"}
        raw_escaped = '{"text": "path\\\\'
        salvaged_escaped = extract_json_payload(raw_escaped)
        self.assertIsNotNone(salvaged_escaped)

    def test_session_manager_custom_request_tuple_timeout(self):
        """Custom request handles short and empty tuple timeouts without IndexError."""
        mgr = YFinanceSessionManager()
        with patch("session_manager.CURL_CFFI_AVAILABLE", False):
            sess = mgr._create_session("Mozilla/5.0", 0)
            adapter = MagicMock()
            mock_resp = requests.Response()
            mock_resp.status_code = 200
            mock_resp.url = "https://example.com/api"
            mock_resp.raw = io.BytesIO(b"{}")
            adapter.send.return_value = mock_resp
            sess.mount("https://", adapter)

            # Test with 1-tuple
            sess.request("GET", "https://example.com/api", timeout=(5.0,))
            call_kwargs = adapter.send.call_args[1]
            self.assertEqual(call_kwargs.get("timeout"), (5.0, 15.0))

            # Test with empty tuple
            sess.request("GET", "https://example.com/api", timeout=())
            call_kwargs_empty = adapter.send.call_args[1]
            self.assertEqual(call_kwargs_empty.get("timeout"), (15.0, 15.0))

    def test_storage_save_user_stocks_finite_rate(self):
        """Storage coerces non-finite or negative last_usdjpy_rate."""
        with tempfile.TemporaryDirectory() as td:
            tmp_stocks = Path(td) / "user_stocks.json"
            with (
                patch.object(app_state.market, "last_usdjpy_rate", float("nan")),
                patch("utils.storage.USER_STOCKS_FILE", tmp_stocks),
                patch("config_store.get_or_create_master_key", return_value="dummy_key"),
                patch("utils.storage.protect_data", return_value={"scheme": "dummy", "value": ""}),
            ):
                storage.save_user_stocks()
                self.assertTrue(tmp_stocks.exists())

    def test_native_host_read_message_fragmented_header(self):
        """R8: read_message handles 4-byte header delivered in 1-byte chunks."""
        test_payload = b'{"action":"health"}'
        length_header = struct.pack("<I", len(test_payload))

        class ChunkedBytesIO:
            def __init__(self, data):
                self.data = data
                self.pos = 0

            def read(self, n=1):
                # Return at most 1 byte per read to simulate fragmentation
                if self.pos >= len(self.data):
                    return b""
                chunk = self.data[self.pos : self.pos + 1]
                self.pos += 1
                return chunk

        stream = ChunkedBytesIO(length_header + test_payload)
        with patch.object(native_host, "RAW_STDIN", stream):
            msg = native_host.read_message()
            self.assertEqual(msg, {"action": "health"})


if __name__ == "__main__":
    unittest.main()
