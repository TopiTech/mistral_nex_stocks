"""Tests for the review-driven improvements:

- P1-1: dirty-symbol delta scanning in RealtimeMarketEngine (only changed
  symbols are revisited after the first full-snapshot scan).
- P1-2: MNS_SIMULATE_FLUCTUATION gate on the mode-1 interpolator's random
  price noise (pure linear interpolation when disabled).
- P1-3: Yahoo JP __NEXT_DATA__ JSON fallback parsing.
- P2-3: canonical history short-cache key helpers.
"""

import time
import unittest
from unittest.mock import patch

from services.realtime_engine import (
    RealtimeMarketEngine,
    _extract_next_data_quotes,
)
from utils.caching import history_short_cache_key, history_short_payload_cache_key


def _payload(symbol, price, change=0.0):
    return {
        "symbol": symbol,
        "price": price,
        "change": change,
        "change_percent": 0.0,
        "volume": 0,
        "source": "tradingview",
        "updated_at": time.time(),
    }


class DirtyDeltaScanTests(unittest.TestCase):
    """P1-1: delta scanning must only revisit dirty symbols after the first pass."""

    def test_first_scan_returns_full_snapshot_then_only_changes(self):
        engine = RealtimeMarketEngine()
        engine._handle_producer_update(_payload("AAPL", 220.0, 1.0))
        engine._handle_producer_update(_payload("MSFT", 410.0, 2.0))

        # First scan: full snapshot for a fresh cursor.
        d1 = engine.get_market_deltas()
        self.assertEqual(set(d1.keys()), {"AAPL", "MSFT"})
        # Second scan with no changes: nothing.
        self.assertEqual(engine.get_market_deltas(), {})
        # Only AAPL changes: only AAPL is returned, and it is pruned once
        # every cursor has consumed the new value.
        engine._handle_producer_update(_payload("AAPL", 222.0, 3.0))
        d3 = engine.get_market_deltas()
        self.assertEqual(set(d3.keys()), {"AAPL"})
        self.assertEqual(d3["AAPL"]["price"], 222.0)
        self.assertEqual(engine.get_market_deltas(), {})

    def test_dirty_set_stays_until_all_clients_consume(self):
        engine = RealtimeMarketEngine()
        engine._handle_producer_update(_payload("AAPL", 220.0, 1.0))
        c1 = engine.register_client()
        c2 = engine.register_client()

        # Both clients consume the initial snapshot.
        engine.get_market_deltas(c1)
        engine.get_market_deltas(c2)

        engine._handle_producer_update(_payload("AAPL", 221.0, 2.0))
        # c1 consumes first; AAPL must STILL be pending for c2 (each cursor
        # owns its own pending set and drains it only when it polls).
        self.assertIn("AAPL", engine.get_market_deltas(c1))
        self.assertIn("AAPL", engine._client_pending[c2])
        # c2 consumes; its pending set is now drained.
        self.assertIn("AAPL", engine.get_market_deltas(c2))
        self.assertNotIn("AAPL", engine._client_pending[c2])
        # The default cursor's pending set (_dirty_symbols) is independent of
        # per-client cursors; it is drained when the default cursor scans.
        self.assertIn("AAPL", engine._dirty_symbols)
        engine.get_market_deltas()  # default cursor full scan
        self.assertNotIn("AAPL", engine._dirty_symbols)

    def test_pending_sets_fan_out_on_every_update(self):
        # A producer update must reach the default cursor AND every registered
        # client's pending set (previously a shared dirty set required a
        # cross-cursor prune pass that could never fire in production because
        # the default cursor is never polled by the SSE path).
        engine = RealtimeMarketEngine()
        c1 = engine.register_client()
        engine._handle_producer_update(_payload("AAPL", 220.0, 1.0))
        self.assertIn("AAPL", engine._dirty_symbols)
        self.assertIn("AAPL", engine._client_pending[c1])
        # Unregistered clients hold no pending state.
        self.assertIn("AAPL", engine.get_market_deltas(c1))
        self.assertNotIn("AAPL", engine._client_pending[c1])

    def test_unregister_drops_dirty_entry(self):
        engine = RealtimeMarketEngine()
        engine._handle_producer_update(_payload("7203.T", 3500.0, 50.0))
        engine._handle_producer_update(_payload("AAPL", 220.0, 1.0))
        self.assertIn("7203.T", engine._dirty_symbols)

        engine.unregister_symbol("7203.T", "jp")
        self.assertNotIn("7203.T", engine._dirty_symbols)
        self.assertNotIn("7203.T", engine.get_market_deltas())

    def test_pts_dirty_scan(self):
        engine = RealtimeMarketEngine()
        pts = {
            "symbol": "7203.T",
            "price": 2973.9,
            "change": -9.6,
            "change_percent": -0.32,
            "volume": 600,
            "source": "yahoojp_pts",
            "pts": True,
            "pts_trading": True,
            "pts_time": "17:03",
            "updated_at": time.time(),
        }
        engine._handle_pts_update(pts)
        self.assertIn("7203.T", engine.get_pts_deltas())
        self.assertEqual(engine.get_pts_deltas(), {})

        pts2 = dict(pts)
        pts2["price"] = 2975.0
        engine._handle_pts_update(pts2)
        d = engine.get_pts_deltas()
        self.assertEqual(d["7203.T"]["price"], 2975.0)
        self.assertEqual(engine.get_pts_deltas(), {})


class FluctuationGateTests(unittest.TestCase):
    """P1-2: random price noise must respect the SIMULATE_FLUCTUATION flag."""

    def test_fluctuation_disabled_is_pure_interpolation(self):
        # Patch the module-level flag used by _interpolate_and_fluctuate_market
        # (avoid importlib.reload, which would clobber app_state/app_bg module
        # state shared with the rest of the test suite).
        import app_bg

        target = [{"symbol": "AAPL", "price": 100.0, "change": 2.0, "currency": "USD"}]
        current = [{"symbol": "AAPL", "price": 100.0, "change": 2.0, "currency": "USD"}]

        with (
            patch.object(app_bg, "SIMULATE_FLUCTUATION", False),
            patch("random.random", return_value=0.1),
            patch("random.uniform", return_value=0.0002),
        ):
            # Flag off: even with random() primed to trigger noise, current==target
            # stays exactly 100.0 (pure interpolation, no artificial fluctuation).
            res = app_bg._interpolate_and_fluctuate_market(
                target, current, is_open=True, market="us"
            )
            self.assertEqual(res[0]["price"], 100.0)

    def test_fluctuation_enabled_still_fluctuates(self):
        # Sanity check: with the flag on (default), the historical behaviour is
        # preserved — random noise is applied while the market is open.
        import app_bg

        target = [{"symbol": "AAPL", "price": 100.0, "change": 2.0, "currency": "USD"}]
        current = [{"symbol": "AAPL", "price": 100.0, "change": 2.0, "currency": "USD"}]

        with (
            patch.object(app_bg, "SIMULATE_FLUCTUATION", True),
            patch("random.random", return_value=0.1),
            patch("random.uniform", return_value=0.0002),
        ):
            res = app_bg._interpolate_and_fluctuate_market(
                target, current, is_open=True, market="us"
            )
            self.assertEqual(res[0]["price"], 100.02)

    def test_flag_off_does_not_affect_indices(self):
        # _fluctuate_indices must also respect the gate.
        import app_bg

        indices = {
            "SP500": {"price": 5000.0, "change": 50.0, "percent": 1.0},
            "USDJPY": {"price": 150.0, "change": 1.5, "percent": 1.0},
        }
        with (
            patch.object(app_bg, "SIMULATE_FLUCTUATION", False),
            patch("random.random", return_value=0.1),
            patch("random.uniform", return_value=0.0001),
        ):
            app_bg._fluctuate_indices(indices, us_open=True, jp_open=False)
            # No artificial noise applied when the flag is off.
            self.assertEqual(indices["SP500"]["price"], 5000.0)
            self.assertEqual(indices["USDJPY"]["price"], 150.0)


class NextDataParsingTests(unittest.TestCase):
    """P1-3: __NEXT_DATA__ JSON fallback must extract quote fields."""

    def test_extracts_quote_from_next_data_blob(self):
        html = (
            '<html><head><script id="__NEXT_DATA__" type="application/json">'
            '{"props":{"pageProps":{"quote":{"price":{"value":"2,983.5"},'
            '"priceChange":{"value":"-9.6"},"priceChangeRate":{"value":"-0.32"},'
            '"volume":{"value":"1,234"}}}}}'
            "</script></head></html>"
        )
        quotes = _extract_next_data_quotes(html)
        self.assertIsNotNone(quotes)
        self.assertEqual(quotes["price"], "2,983.5")
        self.assertEqual(quotes["priceChange"], "-9.6")
        self.assertEqual(quotes["priceChangeRate"], "-0.32")

    def test_returns_none_without_blob(self):
        self.assertIsNone(_extract_next_data_quotes("<html><body>no data</body></html>"))

    def test_returns_none_on_malformed_json(self):
        html = (
            '<script id="__NEXT_DATA__" type="application/json">{not valid json}</script>'
        )
        self.assertIsNone(_extract_next_data_quotes(html))

    def test_handles_html_escaped_json(self):
        # Real Next.js pages HTML-escape the JSON (e.g. &amp; for &). Unescaping
        # must happen BEFORE json.loads so the parser sees valid JSON.
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            '{"a":{"price":{"value":"1,000&amp;5","fmt":"1,000.5"}}}'
            "</script>"
        )
        quotes = _extract_next_data_quotes(html)
        self.assertIsNotNone(quotes)
        # After unescape, the stored value contains a literal ampersand.
        self.assertEqual(quotes["price"], "1,000&5")


class CacheKeyHelpersTests(unittest.TestCase):
    """P2-3: canonical short-cache key helpers."""

    def test_history_short_cache_key_format(self):
        self.assertEqual(
            history_short_cache_key("AAPL", "1mo", "1d"), "history_short_AAPL_1mo_1d"
        )
        self.assertEqual(
            history_short_cache_key("7203.T", "3mo", "1d"), "history_short_7203.T_3mo_1d"
        )

    def test_history_short_payload_cache_key_format(self):
        self.assertEqual(
            history_short_payload_cache_key("AAPL", "1mo"),
            "history_short_payload_AAPL_1mo_auto",
        )
        self.assertEqual(
            history_short_payload_cache_key("AAPL", "1mo", "15m"),
            "history_short_payload_AAPL_1mo_15m",
        )


if __name__ == "__main__":
    unittest.main()
