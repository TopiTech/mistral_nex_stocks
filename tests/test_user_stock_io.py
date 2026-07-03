import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from app import app
from app_state import app_state
from app_helpers import load_user_stocks
from constants import YFINANCE_TIMEOUT_SINGLE


class UserStockLoadTests(unittest.TestCase):
    def setUp(self):
        with app_state.market.user_stocks_lock:
            self._original_user_us = app_state.market.user_us.copy()
            self._original_user_jp = app_state.market.user_jp.copy()
            self._original_user_idx = app_state.market.user_idx.copy()
            self._original_last_modified_ns = app_state.market.last_modified_ns

    def tearDown(self):
        with app_state.market.user_stocks_lock:
            app_state.market.user_us = self._original_user_us
            app_state.market.user_jp = self._original_user_jp
            app_state.market.user_idx = self._original_user_idx
            app_state.market.last_modified_ns = self._original_last_modified_ns

    def test_load_user_stocks_resets_invalid_json_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stocks_file = Path(tmpdir) / "user_stocks.json"
            stocks_file.write_text(json.dumps(["unexpected"]), encoding="utf-8")

            with app.app_context():
                with app_state.market.user_stocks_lock:
                    app_state.market.user_us = {"AAPL": "Apple"}
                    app_state.market.user_jp = {"7203.T": "Toyota"}
                    app_state.market.user_idx = {"^DJI": "Dow"}
                    app_state.market.last_modified_ns = 0

                with patch("app_helpers.USER_STOCKS_FILE", str(stocks_file)):
                    load_user_stocks(force=True)

                with app_state.market.user_stocks_lock:
                    self.assertEqual(app_state.market.user_us, {})
                    self.assertEqual(app_state.market.user_jp, {})
                    self.assertEqual(app_state.market.user_idx, {})


class StockHistoryTimeoutTests(unittest.TestCase):
    def test_stock_history_passes_timeout_to_yfinance(self):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame(
            {
                "Open": [1.0],
                "High": [2.0],
                "Low": [0.5],
                "Close": [1.5],
                "Volume": [100],
            },
            index=pd.to_datetime(["2026-05-21"]),
        )

        with app.app_context():
            with app_state.market.history_circuit_lock:
                app_state.market.history_circuit_state.pop("AAPL", None)

            with patch("routes.api_stocks.safe_get_ticker", return_value=mock_ticker):
                response = app.test_client().get(
                    "/api/stock-history",
                    query_string={"symbol": "AAPL", "market": "us", "period": "1mo"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.is_json)
        self.assertEqual(mock_ticker.history.call_count, 1)
        self.assertEqual(
            mock_ticker.history.call_args.kwargs.get("timeout"),
            YFINANCE_TIMEOUT_SINGLE,
        )
        self.assertTrue(mock_ticker.history.call_args.kwargs.get("auto_adjust"))


if __name__ == "__main__":
    unittest.main()
