import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from app_state import app_state
from utils.stock_payload import build_stock_payload


class BuildStockPayloadTestCase(unittest.TestCase):
    def _sample_hist(self):
        idx = pd.to_datetime(["2026-01-01", "2026-01-02"])
        return pd.DataFrame(
            {
                "Open": [95.0, 100.0],
                "High": [101.0, 111.0],
                "Low": [90.0, 99.0],
                "Close": [100.0, 110.0],
                "Volume": [1000, 1500],
            },
            index=idx,
        )

    @patch.object(app_state.stock_provider, 'get_calendar', return_value={})
    @patch("utils.stock_payload.is_market_open", return_value=True)
    @patch("utils.stock_payload.get_stock_info_cached", return_value={})
    def test_portfolio_pl_is_computed_when_avg_price_zero(self, _mock_info, _mock_market, _mock_cal):
        payload = build_stock_payload(
            "TEST",
            {"name": "Test Inc", "shares": 10, "avg_price": 0},
            "jp",
            self._sample_hist(),
            snapshot_ts_ms=1234567890,
        )
        self.assertIsNotNone(payload)
        self.assertEqual(payload["portfolio_pl"], 1100.0)

    @patch.object(app_state.stock_provider, 'get_calendar', return_value={})
    @patch("utils.stock_payload.is_market_open", return_value=True)
    @patch("utils.stock_payload.get_stock_info_cached", return_value={})
    def test_build_payload_handles_stock_info_empty(self, _mock_info, _mock_market, _mock_cal):
        payload = build_stock_payload(
            "TEST",
            {"name": "Test Inc", "shares": 1, "avg_price": 100},
            "us",
            self._sample_hist(),
            snapshot_ts_ms=1234567890,
        )
        self.assertIsNotNone(payload)
        self.assertEqual(payload["name"], "Test Inc")
        # get_stock_info_cached never returns None in practice (returns {} on error),
        # so market_state is determined by is_market_open()
        self.assertIn(payload["market_state"], ("REGULAR", "CLOSED"))
        self.assertEqual(payload["sector"], "Other")

    @patch.object(app_state.stock_provider, 'get_calendar', return_value={})
    @patch("utils.stock_payload.is_market_open", return_value=True)
    @patch("utils.stock_payload.get_stock_info_cached", return_value={})
    def test_build_payload_rejects_non_positive_price(self, _mock_info, _mock_market, _mock_cal):
        # Setup history where the latest close price is 0
        hist_zero = self._sample_hist()
        hist_zero.loc[hist_zero.index[-1], "Close"] = 0.0
        payload = build_stock_payload(
            "TEST",
            {"name": "Test Inc", "shares": 1, "avg_price": 100},
            "us",
            hist_zero,
            snapshot_ts_ms=1234567890,
        )
        self.assertIsNone(payload)

    @patch.object(app_state.stock_provider, 'get_calendar', return_value={})
    @patch("utils.stock_payload.is_market_open", return_value=True)
    @patch("utils.stock_payload.get_stock_info_cached", return_value={})
    def test_build_payload_rejects_non_positive_prev(self, _mock_info, _mock_market, _mock_cal):
        # Setup history where previous close price is -5.0
        hist_neg = self._sample_hist()
        hist_neg.loc[hist_neg.index[0], "Close"] = -5.0
        payload = build_stock_payload(
            "TEST",
            {"name": "Test Inc", "shares": 1, "avg_price": 100},
            "us",
            hist_neg,
            snapshot_ts_ms=1234567890,
        )
        self.assertIsNone(payload)

    @patch("utils.stock_payload.is_market_open", return_value=True)
    @patch("utils.stock_payload.get_stock_info_cached", return_value={})
    @patch("utils.stock_payload.get_cached")
    def test_index_market_skips_calendar_lookup(self, mock_get_cached, _mock_info, _mock_market):
        payload = build_stock_payload(
            "^N225",
            {"name": "Nikkei 225"},
            "idx",
            self._sample_hist(),
            snapshot_ts_ms=1234567890,
        )
        self.assertIsNotNone(payload)
        mock_get_cached.assert_not_called()
        self.assertIsNone(payload["next_earnings"])


if __name__ == "__main__":
    unittest.main()

