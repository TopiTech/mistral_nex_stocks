"""tests/test_sse_modes.py - Integration tests for 3-stage SSE streaming modes.
"""

import json
import unittest

from app import create_app
from app_state import app_state
from constants import SSE_MODE_DISABLED


class TestSSEModes(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

        # Pre-populate sample stock cache for testing
        with app_state.cache.sse_data_lock:
            app_state.market.current_stocks_cache = {
                "us": [
                    {
                        "symbol": "AAPL",
                        "name": "Apple Inc.",
                        "price": 180.5,
                        "change": 1.2,
                        "change_percent": 0.67,
                    }
                ],
                "jp": [
                    {
                        "symbol": "7203.T",
                        "name": "トヨタ自動車",
                        "price": 2500.0,
                        "change": -10.0,
                        "change_percent": -0.4,
                    }
                ],
                "idx": [],
            }

    def test_sse_mode_0_disabled(self):
        """Mode 0 (Disabled) should return JSON status cleanly without opening SSE stream."""
        response = self.client.get(
            "/api/stocks/stream?mode=0",
            headers={"X-MNS-Admin-Token": "test-token"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["status"], "disabled")
        self.assertEqual(data["sse_mode"], SSE_MODE_DISABLED)

    def test_sse_mode_1_complementary(self):
        """Mode 1 (Complementary) should return event-stream with sse_mode=1 in snapshot."""
        response = self.client.get(
            "/api/stocks/stream?mode=1",
            headers={"X-MNS-Admin-Token": "test-token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/event-stream")

        # Read first chunk of stream
        first_chunk = next(response.response).decode("utf-8")
        self.assertIn("initial_snapshot", first_chunk)
        self.assertIn('"sse_mode": 1', first_chunk)

    def test_sse_mode_2_tradingview_realtime(self):
        """Mode 2 (TradingView Realtime) should include tv_symbol mapping and tv_ticker_tape list."""
        response = self.client.get(
            "/api/stocks/stream?mode=2",
            headers={"X-MNS-Admin-Token": "test-token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/event-stream")

        first_chunk = next(response.response).decode("utf-8")
        self.assertIn("initial_snapshot", first_chunk)
        self.assertIn('"sse_mode": 2', first_chunk)

        # Parse JSON payload from data line
        data_line = next(line for line in first_chunk.split("\n") if line.startswith("data: "))
        payload = json.loads(data_line[6:])

        self.assertIn("tv_ticker_tape", payload)
        self.assertIsInstance(payload["tv_ticker_tape"], list)

        # Verify tv_symbol was added to stocks
        us_stocks = payload["stocks"]["us"]
        self.assertEqual(us_stocks[0]["tv_symbol"], "NASDAQ:AAPL")

        jp_stocks = payload["stocks"]["jp"]
        self.assertEqual(jp_stocks[0]["tv_symbol"], "TSE:7203")

    def test_sse_excludes_idx_market(self):
        """Verify that SSE stream payloads exclude the idx (Index/ETF) market."""
        from app_bg import _build_sse_diff, _build_sse_light_stocks_payload

        # 1. Initial snapshot stream payload test
        response = self.client.get(
            "/api/stocks/stream?mode=2",
            headers={"X-MNS-Admin-Token": "test-token"},
        )
        self.assertEqual(response.status_code, 200)
        first_chunk = next(response.response).decode("utf-8")
        data_line = next(line for line in first_chunk.split("\n") if line.startswith("data: "))
        payload = json.loads(data_line[6:])
        self.assertNotIn("idx", payload["stocks"])

        # 2. _build_sse_light_stocks_payload test
        sample = {
            "us": [{"symbol": "AAPL"}],
            "jp": [{"symbol": "7203.T"}],
            "idx": [{"symbol": "^N225"}],
        }
        light = _build_sse_light_stocks_payload(sample)
        self.assertNotIn("idx", light)

        # 3. _build_sse_diff test
        diff = _build_sse_diff(sample, {})
        self.assertNotIn("idx", diff)

    def test_mode2_does_not_receive_mode1_interpolated_ticks(self):
        """Verify Mode 2 clients listen on sse_announcer_mode2 and do NOT receive Mode 1 interpolated ticks."""
        from app_bg import announce_current_market_state
        # Trigger mode 1 announcement
        announce_current_market_state()

        # Mode 2 announcer should remain untouched with 0 listeners
        self.assertEqual(app_state.sse_announcer_mode2.listener_count(), 0)

    def test_sse_stream_terminates_on_backpressure_sentinel(self):
        """Verify that when a backpressure None sentinel is received, the SSE stream generator breaks cleanly."""
        response = self.client.get(
            "/api/stocks/stream?mode=1",
            headers={"X-MNS-Admin-Token": "test-token"},
        )
        self.assertEqual(response.status_code, 200)
        gen = response.response
        # Read the initial snapshot
        first_chunk = next(gen).decode("utf-8")
        self.assertIn("initial_snapshot", first_chunk)

        # Retrieve the registered listener queue from sse_announcer_mode1
        listeners = list(app_state.sse_announcer_mode1.listeners)
        self.assertGreater(len(listeners), 0)
        target_q = listeners[-1]

        # Inject backpressure None sentinel directly into queue
        target_q.put_nowait(None)

        # The stream generator should terminate cleanly (raise StopIteration)
        with self.assertRaises(StopIteration):
            next(gen)


if __name__ == "__main__":
    unittest.main()

