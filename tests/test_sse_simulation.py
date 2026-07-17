"""Unit tests for the SSE real-time stock price interpolation and simulation logic."""

import random
import unittest
from unittest.mock import patch

from app_bg import _interpolate_and_fluctuate_market, _fluctuate_indices


class SseSimulationTests(unittest.TestCase):
    def setUp(self):
        # Fix random seed for reproducibility in tests that check random choices
        random.seed(42)

    def test_interpolate_empty_targets(self):
        res = _interpolate_and_fluctuate_market([], [], is_open=True, market="us")
        self.assertEqual(res, [])

    def test_interpolate_closed_market(self):
        target_list = [
            {
                "symbol": "AAPL",
                "price": 100.0,
                "change": 2.0,
                "change_percent": 2.04,
                "currency": "USD",
                "name": "Apple Inc."
            }
        ]
        current_list = [
            {
                "symbol": "AAPL",
                "price": 99.2,
                "change": -2.0,
                "change_percent": -2.04,
                "currency": "USD",
                "name": "Apple Inc."
            }
        ]

        # In a closed market, it should just step towards target without noise.
        # step = (100 - 99.2) * 0.25 = 0.2 -> new_price = 99.4
        # previous_close = 100 - 2 = 98.0
        # new_change = 99.4 - 98.0 = 1.4
        # new_change_percent = 1.4 / 98.0 * 100 = 1.43%
        res = _interpolate_and_fluctuate_market(target_list, current_list, is_open=False, market="us")
        self.assertEqual(len(res), 1)
        stock = res[0]
        self.assertEqual(stock["symbol"], "AAPL")
        self.assertEqual(stock["price"], 99.4)
        self.assertEqual(stock["change"], 1.4)
        self.assertEqual(stock["change_percent"], 1.43)
        self.assertEqual(stock["market_state"], "CLOSED")
        self.assertIsNotNone(stock.get("snapshot_ts_ms"))

    def test_interpolate_open_market_fluctuations_and_clamping(self):
        target_list = [
            {
                "symbol": "AAPL",
                "price": 100.0,
                "change": 2.0,
                "change_percent": 2.04,
                "currency": "USD",
                "name": "Apple Inc."
            }
        ]
        current_list = [
            {
                "symbol": "AAPL",
                "price": 100.0,
                "change": 2.0,
                "change_percent": 2.04,
                "currency": "USD",
                "name": "Apple Inc."
            }
        ]

        # Force random.random to return 0.1 so fluctuation is triggered,
        # and random.uniform to return 0.0002 (positive fluctuation)
        with patch("random.random", return_value=0.1), \
             patch("random.uniform", return_value=0.0002):
            res = _interpolate_and_fluctuate_market(target_list, current_list, is_open=True, market="us")

        self.assertEqual(len(res), 1)
        stock = res[0]
        self.assertEqual(stock["price"], 100.02)
        self.assertEqual(stock["market_state"], "REGULAR")

        # Test clamping: if price exceeds target +/- 1.0% (i.e. > 101.0 or < 99.0)
        current_list_large = [{"symbol": "AAPL", "price": 102.0, "currency": "USD"}]
        res_clamped = _interpolate_and_fluctuate_market(target_list, current_list_large, is_open=True, market="us")
        self.assertLessEqual(res_clamped[0]["price"], 101.0)

    def test_decimal_rounding_rules(self):
        target_us = [{"symbol": "AAPL", "price": 100.12345, "change": 1.0, "currency": "USD"}]
        target_jp = [{"symbol": "7203.T", "price": 2500.12345, "change": 10.0, "currency": "JPY"}]

        res_us = _interpolate_and_fluctuate_market(target_us, [], is_open=False, market="us")
        res_jp = _interpolate_and_fluctuate_market(target_jp, [], is_open=False, market="jp")

        self.assertEqual(res_us[0]["price"], 100.1235)
        self.assertEqual(res_jp[0]["price"], 2500.12)

    def test_fluctuate_indices(self):
        indices = {
            "SP500": {"price": 5000.0, "change": 50.0, "percent": 1.0},
            "N225": {"price": 38000.0, "change": 380.0, "percent": 1.01},
            "USDJPY": {"price": 150.0, "change": 1.5, "percent": 1.0}
        }

        # Closed markets - no changes
        indices_closed = indices.copy()
        _fluctuate_indices(indices_closed, us_open=False, jp_open=False)
        self.assertEqual(indices_closed, indices)

        # Open markets - fluctuate SP500 and USDJPY
        indices_open = {
            "SP500": {"price": 5000.0, "change": 50.0, "percent": 1.0},
            "N225": {"price": 38000.0, "change": 380.0, "percent": 1.01},
            "USDJPY": {"price": 150.0, "change": 1.5, "percent": 1.0}
        }
        with patch("random.random", return_value=0.1), \
             patch("random.uniform", return_value=0.0001):
            _fluctuate_indices(indices_open, us_open=True, jp_open=False)

        self.assertEqual(indices_open["N225"]["price"], 38000.0)
        self.assertNotEqual(indices_open["SP500"]["price"], 5000.0)
        self.assertNotEqual(indices_open["USDJPY"]["price"], 150.0)

        self.assertEqual(indices_open["SP500"]["price"], 5000.5)
        self.assertEqual(indices_open["USDJPY"]["price"], 150.015)

    def test_announce_current_market_state_payload(self):
        import json
        import app_bg
        from app_state import app_state

        with patch.object(app_state.sse_announcer, "announce") as mock_announce, \
             patch("app_bg.is_market_open", side_effect=lambda m: True if m == "us" else False):
            app_bg._invalidate_sse_payload_cache()
            app_bg._sse_full_snapshot_counter = 5
            app_bg._original_announce_current_market_state()

            self.assertTrue(mock_announce.called)
            announcement = mock_announce.call_args[0][0]
            self.assertTrue(announcement.startswith("data: "))
            
            json_str = announcement[len("data: "):-2]
            payload = json.loads(json_str)
            
            self.assertIn("is_us_market_open", payload)
            self.assertIn("is_jp_market_open", payload)
            self.assertTrue(payload["is_us_market_open"])
            self.assertFalse(payload["is_jp_market_open"])


if __name__ == "__main__":
    unittest.main()
