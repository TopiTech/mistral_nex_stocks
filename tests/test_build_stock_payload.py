import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

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

    def test_fetch_stock_writes_market_only_payload_to_disk(self):
        import app_bg

        sensitive_payload = {
            "symbol": "AAPL",
            "market": "us",
            "price": 200.0,
            "shares": 12.5,
            "avg_price": 187.25,
            "avg_fx_rate": 149.8,
            "portfolio_value": 375000.0,
            "portfolio_pl": 25000.0,
        }
        with (
            patch("app_bg.acquire_yfinance_slot", return_value=True),
            patch("utils.market_utils.is_market_open", return_value=False),
            patch.object(app_state.stock_provider, "get_history", return_value=self._sample_hist()),
            patch("app_bg.build_stock_payload", return_value=sensitive_payload),
            patch.object(app_state.payload_disk_cache, "set") as cache_set,
        ):
            result = app_bg.fetch_stock("AAPL", {"name": "Apple"}, "us")

        self.assertIs(result, sensitive_payload)
        cached = cache_set.call_args.args[1]
        self.assertEqual(cached["symbol"], "AAPL")
        for key in ("shares", "avg_price", "avg_fx_rate", "portfolio_value", "portfolio_pl"):
            self.assertNotIn(key, cached)

    @patch.object(app_state.stock_provider, "get_calendar", return_value={})
    @patch("utils.stock_payload.is_market_open", return_value=True)
    @patch("utils.stock_payload.get_stock_info_cached", return_value={})
    def test_portfolio_pl_is_computed_when_avg_price_zero(
        self, _mock_info, _mock_market, _mock_cal
    ):
        payload = build_stock_payload(
            "TEST",
            {"name": "Test Inc", "shares": 10, "avg_price": 0},
            "jp",
            self._sample_hist(),
            snapshot_ts_ms=1234567890,
        )
        self.assertIsNotNone(payload)
        self.assertEqual(payload["portfolio_pl"], 1100.0)

    @patch.object(app_state.stock_provider, "get_calendar", return_value={})
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

    @patch.object(app_state.stock_provider, "get_calendar", return_value={})
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

    @patch.object(app_state.stock_provider, "get_calendar", return_value={})
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

    @patch.object(app_state.stock_provider, "get_calendar", return_value={})
    @patch("utils.stock_payload.is_market_open", return_value=True)
    @patch(
        "utils.stock_payload.get_stock_info_cached",
        return_value={
            "dividendYield": float("nan"),
            "marketCap": float("nan"),
            "sharesOutstanding": float("nan"),
            "floatShares": float("nan"),
            "freeCashflow": float("nan"),
            "operatingCashflow": float("nan"),
            # Inf must also be rejected (_fmt hardens this path too).
            "trailingPE": float("inf"),
        },
    )
    def test_build_payload_normalizes_nan_fundamentals(
        self, _mock_info, _mock_market, _mock_cal
    ):
        """R2: yfinance NaN must never reach the SSE JSON stream.

        The SSE stream serializes with ``json.dumps(..., allow_nan=False)`` which
        raises ``ValueError`` on NaN. All fundamental fields must be normalized
        to None so the payload remains JSON-serializable.
        """
        payload = build_stock_payload(
            "TEST",
            {"name": "Test Inc"},
            "us",
            self._sample_hist(),
            snapshot_ts_ms=1234567890,
        )
        self.assertIsNotNone(payload)
        for field in (
            "dividend_yield",
            "market_cap",
            "shares_outstanding",
            "float_shares",
            "free_cashflow",
            "operating_cashflow",
            "pe_ratio",
        ):
            self.assertIsNone(payload[field], field)
        # allow_nan=False must succeed end-to-end (SSE serialization path).
        serialized = json.dumps(payload, allow_nan=False)
        self.assertIn("TEST", serialized)

    @patch.object(app_state.stock_provider, "get_calendar", return_value={})
    @patch("utils.stock_payload.is_market_open", return_value=True)
    @patch(
        "utils.stock_payload.get_stock_info_cached",
        return_value={
            "dividendYield": 0.04567,
            "marketCap": 1_000_000_000_000,
            "sharesOutstanding": 1_000_000_000,
            "floatShares": 900_000_000,
            "freeCashflow": -123456789.0,
            "operatingCashflow": 500000000.0,
        },
    )
    def test_build_payload_keeps_valid_fundamentals(
        self, _mock_info, _mock_market, _mock_cal
    ):
        """R2 regression: valid finite fundamentals must pass through unchanged."""
        payload = build_stock_payload(
            "TEST",
            {"name": "Test Inc"},
            "us",
            self._sample_hist(),
            snapshot_ts_ms=1234567890,
        )
        self.assertIsNotNone(payload)
        self.assertEqual(payload["dividend_yield"], round(0.04567, 4))
        self.assertEqual(payload["market_cap"], 1_000_000_000_000)
        self.assertEqual(payload["shares_outstanding"], 1_000_000_000)
        self.assertEqual(payload["float_shares"], 900_000_000)
        # Negative cash flow is meaningful and must be preserved.
        self.assertEqual(payload["free_cashflow"], -123456789.0)
        self.assertEqual(payload["operating_cashflow"], 500000000.0)

    @patch("utils.stock_payload._build_chart_ohlc_data")
    @patch("utils.stock_payload.is_market_open", return_value=True)
    @patch("utils.stock_payload.get_stock_info_cached", return_value={})
    def test_lightweight_payload_skips_chart_build(
        self, _mock_info, _mock_market, mock_chart_build
    ):
        """P4: lightweight payloads (heatmap/screener) must skip the expensive
        DataFrame -> dict conversion. This keeps the background sync loop
        CPU-cheap while the dashboard still receives full chart data."""
        payload = build_stock_payload(
            "TEST",
            {"name": "Test Inc"},
            "us",
            self._sample_hist(),
            snapshot_ts_ms=1234567890,
            lightweight=True,
        )
        self.assertIsNotNone(payload)
        mock_chart_build.assert_not_called()
        self.assertEqual(payload["chart_data"], [])
        self.assertEqual(payload["ohlc_data"], [])

    @patch("utils.stock_payload._build_chart_ohlc_data")
    @patch("utils.stock_payload.is_market_open", return_value=True)
    @patch("utils.stock_payload.get_stock_info_cached", return_value={})
    def test_full_payload_still_builds_chart(
        self, _mock_info, _mock_market, mock_chart_build
    ):
        """P4: non-lightweight payloads must keep building chart/OHLC series."""
        mock_chart_build.return_value = ([{"x": 1, "price": 100.0}], [{"x": 1, "c": 100.0}])
        payload = build_stock_payload(
            "TEST",
            {"name": "Test Inc"},
            "us",
            self._sample_hist(),
            snapshot_ts_ms=1234567890,
            lightweight=False,
        )
        self.assertIsNotNone(payload)
        mock_chart_build.assert_called_once()
        self.assertEqual(payload["chart_data"], [{"x": 1, "price": 100.0}])


if __name__ == "__main__":
    unittest.main()
