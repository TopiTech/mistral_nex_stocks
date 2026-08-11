"""Regression tests for realtime reconnect reliability and AI-portfolio symbol normalization.

Covers fixes reviewed against HEAD:
- TradingView WS reconnect exponential backoff was dead code (reset to 1.0 on
  every loop iteration), causing a constant 1s reconnect storm.
- The reconnect backoff ``time.sleep`` was not interruptible by ``stop()``.
- ``TradingViewWSClient.stop()`` did not detach its worker thread, so a
  crash-recovery ``restart()`` (start()'s is_alive() guard) could permanently
  strand US realtime after the old thread lingered.
- AI-portfolio sanitizer did not normalize JP digit symbols to ``.T``.
"""

import threading
import time
import types
from unittest.mock import patch

from services import realtime_engine as rt
from services.ai_portfolio_service import sanitize_ai_portfolio

# ---------------------------------------------------------------------------
# TradingView WS reconnect reliability
# ---------------------------------------------------------------------------


def test_tv_reconnect_backoff_grows():
    """Backoff must grow across consecutive failures (no longer stuck at 1.0)."""
    recorded = []

    def fake_interrupt(should_continue, seconds, step=0.5):
        recorded.append(seconds)

    class FailWSApp:
        def __init__(self, *a, **kw):
            self.on_open = kw.get("on_open")

        def send(self, *a, **kw):
            pass

        def run_forever(self, **kw):
            # Simulate an immediate connection failure -> except/backoff path.
            raise RuntimeError("simulated connect failure")

    fake_ws = types.SimpleNamespace(WebSocketApp=FailWSApp)
    fake_state = types.SimpleNamespace(
        execution=types.SimpleNamespace(shutdown_event=threading.Event())
    )

    client = rt.TradingViewWSClient(symbols=["NASDAQ:AAPL"])
    with (
        patch.object(rt, "websocket", fake_ws),
        patch("app_state.app_state", fake_state),
        patch("services.realtime_engine._interruptible_sleep", fake_interrupt),
    ):
        client.running = True
        t = threading.Thread(target=client._run_ws, daemon=True)
        t.start()
        deadline = time.time() + 0.5
        while len(recorded) < 4 and time.time() < deadline:
            time.sleep(0.01)
        client.running = False
        t.join(timeout=2.0)

    assert len(recorded) >= 3, f"expected backoff recorded across failures, got {recorded}"
    # First reconnect uses the base 1.0s, then it must grow.
    assert recorded[0] == 1.0
    assert recorded[1] > recorded[0]
    assert recorded[2] > recorded[1]
    # Growth is capped.
    assert max(recorded) <= 10.0


def test_interruptible_sleep_exits_when_condition_false():
    calls = []

    def fake_sleep(s):
        calls.append(s)

    with patch("services.realtime_engine.time.sleep", fake_sleep):
        rt._interruptible_sleep(lambda: False, 5.0)
    # Should not sleep at all when the continue-condition is immediately False.
    assert calls == []


def test_interruptible_sleep_sleeps_while_condition_true():
    # Use the real sleep here to confirm the helper actually waits (not a no-op).
    start = time.monotonic()
    rt._interruptible_sleep(lambda: True, 0.3, step=0.3)
    elapsed = time.monotonic() - start
    assert elapsed >= 0.25


def test_tv_stop_detaches_worker_thread():
    """stop() must detach the worker thread so a later start() can respawn."""
    client = rt.TradingViewWSClient()
    # Simulate a worker thread reference that is still alive at stop() time.
    dummy = threading.Thread(target=lambda: None, daemon=True)
    dummy.start()
    dummy.join()
    client.thread = dummy
    client.running = True

    client.stop()

    assert client.running is False
    # The fix: stop() clears the thread reference. Without this, start()'s
    # ``if self.thread is not None and self.thread.is_alive()`` guard would
    # short-circuit and never respawn the worker after a restart.
    assert client.thread is None


# ---------------------------------------------------------------------------
# AI-portfolio symbol normalization
# ---------------------------------------------------------------------------


def test_sanitize_ai_portfolio_normalizes_jp_digit_symbol():
    """A model-emitted '7203' on market 'jp' must become the '.T' ticker."""
    portfolio = {
        "theme": "value",
        "title": "JP Value",
        "items": [
            {"symbol": "7203", "market": "jp", "weight_pct": 50.0},
            {"symbol": "AAPL", "market": "us", "weight_pct": 50.0},
        ],
    }
    clean = sanitize_ai_portfolio(portfolio)
    symbols = [it["symbol"] for it in clean["items"]]
    assert "7203.T" in symbols
    assert "AAPL" in symbols


def test_sanitize_ai_portfolio_keeps_existing_suffix():
    portfolio = {
        "items": [{"symbol": "7203.T", "market": "jp", "weight_pct": 100.0}],
    }
    clean = sanitize_ai_portfolio(portfolio)
    assert clean["items"][0]["symbol"] == "7203.T"
