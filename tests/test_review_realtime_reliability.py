"""Regression tests for realtime reconnect reliability and AI-portfolio symbol normalization.

Covers fixes reviewed against HEAD:
- TradingView WS reconnect exponential backoff was dead code (reset to 1.0 on
  every loop iteration), causing a constant 1s reconnect storm.
- The reconnect backoff ``time.sleep`` was not interruptible by ``stop()``.
- A stopped TradingView worker could rejoin after restart because all
  generations shared one ``running`` flag.
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
        client.start()
        deadline = time.time() + 0.5
        while len(recorded) < 4 and time.time() < deadline:
            time.sleep(0.01)
        client.stop()

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
    """A completed worker reference is cleared during stop()."""
    client = rt.TradingViewWSClient()
    # Simulate a worker thread reference that is still alive at stop() time.
    dummy = threading.Thread(target=lambda: None, daemon=True)
    dummy.start()
    dummy.join()
    client.thread = dummy
    client.running = True

    client.stop()

    assert client.running is False
    assert client.thread is None


def test_tv_restart_waits_for_lingering_generation_before_replacement():
    """A delayed run_forever cannot overlap or reconnect after restart."""
    first_started = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()
    release_second = threading.Event()
    state_lock = threading.Lock()
    state = {"created": 0, "active": 0, "max_active": 0}

    class HangingWSApp:
        def __init__(self, *args, **kwargs):
            del args
            self.on_open = kwargs.get("on_open")
            self.index = state["created"]
            state["created"] += 1

        def close(self):
            # Simulate a transport that does not return from run_forever
            # immediately when close() is requested.
            return None

        def run_forever(self, **kwargs):
            del kwargs
            with state_lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            try:
                if self.index == 0:
                    first_started.set()
                    assert release_first.wait(timeout=2.0)
                else:
                    second_started.set()
                    assert release_second.wait(timeout=2.0)
            finally:
                with state_lock:
                    state["active"] -= 1

    client = rt.TradingViewWSClient()
    client.STOP_JOIN_TIMEOUT_SEC = 0.01
    fake_ws = types.SimpleNamespace(WebSocketApp=HangingWSApp)
    with patch.object(rt, "websocket", fake_ws):
        client.start()
        assert first_started.wait(timeout=1.0)
        first_thread = client.thread

        client.stop()
        assert first_thread is not None and first_thread.is_alive()
        assert client.thread is first_thread

        client.start()
        time.sleep(0.05)
        assert state["created"] == 1
        assert state["max_active"] == 1

        release_first.set()
        assert second_started.wait(timeout=1.0)
        assert state["created"] == 2
        assert state["max_active"] == 1

        client.stop()
        release_second.set()
        deadline = time.time() + 1.0
        while client.thread is not None and time.time() < deadline:
            time.sleep(0.01)

    assert client.thread is None
    assert client.running is False


def test_tv_stop_cannot_join_worker_before_start_publishes_it():
    """Concurrent stop waits until the published worker has actually started."""
    client = rt.TradingViewWSClient()
    start_entered = threading.Event()
    release_start = threading.Event()
    stop_returned = threading.Event()
    failures = []

    class ControlledThread:
        def __init__(self, **kwargs):
            self.name = kwargs.get("name", "controlled")
            self.started = False
            self.joined = False

        def start(self):
            start_entered.set()
            assert release_start.wait(timeout=2.0)
            self.started = True

        def is_alive(self):
            return self.started and not self.joined

        def join(self, timeout=None):
            del timeout
            if not self.started:
                raise RuntimeError("cannot join thread before it is started")
            self.joined = True

    def run_start():
        try:
            client.start()
        except Exception as exc:  # pragma: no cover - assertion reports details
            failures.append(exc)

    def run_stop():
        try:
            client.stop()
        except Exception as exc:  # pragma: no cover - assertion reports details
            failures.append(exc)
        finally:
            stop_returned.set()

    start_controller = threading.Thread(target=run_start)
    stop_controller = threading.Thread(target=run_stop)
    with patch.object(rt.threading, "Thread", ControlledThread):
        start_controller.start()
        assert start_entered.wait(timeout=1.0)
        stop_controller.start()
        time.sleep(0.05)
        assert not stop_returned.is_set()
        release_start.set()
        start_controller.join(timeout=1.0)
        stop_controller.join(timeout=1.0)

    assert not failures
    assert stop_returned.is_set()
    assert client.thread is None
    assert client.running is False


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
