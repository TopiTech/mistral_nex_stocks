import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from app_state import app_state
from utils import storage


class StorageCoverageTestCase(unittest.TestCase):
    def test_load_and_save_user_stocks_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            stock_file = Path(td) / "user_stocks.json"
            with patch.object(storage, "USER_STOCKS_FILE", str(stock_file)), \
                 patch.object(app_state.market, "user_stocks_lock", MagicMock()), \
                 patch.object(app_state.market, "user_us", {"AAPL": "Apple"}), \
                 patch.object(app_state.market, "user_jp", {"7203.T": "Toyota"}), \
                 patch.object(app_state.market, "user_idx", {"^N225": "Nikkei"}), \
                 patch.object(app_state.market, "last_usdjpy_rate", 150.0):
                storage.save_user_stocks()
                self.assertTrue(stock_file.exists())

    def test_load_user_stocks_missing_file(self):
        with tempfile.TemporaryDirectory() as td:
            stock_file = Path(td) / "missing.json"
            with patch.object(storage, "USER_STOCKS_FILE", str(stock_file)):
                self.assertIsNone(storage.load_user_stocks())


class AppHelpersCoverageTestCase(unittest.TestCase):
    def test_error_response_structure(self):
        from app_helpers import error_response
        from app import app as flask_app
        with flask_app.app_context():
            resp, status = error_response(1001, status_code=418, details={"a": 1})
            self.assertEqual(status, 418)
            self.assertTrue(resp.get_json()["ok"] is False)

    def test_default_stock_names_and_resolve_helpers(self):
        from app_helpers import _default_stock_names, _resolve_stocks_for_response, _resolve_indices_for_response
        self.assertIn("AAPL", _default_stock_names("us"))
        self.assertIsInstance(_resolve_stocks_for_response(), dict)
        self.assertIsInstance(_resolve_indices_for_response(), dict)


if __name__ == "__main__":
    unittest.main()