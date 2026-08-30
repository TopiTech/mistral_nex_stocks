"""Regression tests for the 2026-08-30 autonomous code review fixes.

Covers:
- CB-RELEASE: ``release_circuit_probe`` must always reset ``probing`` even
  when a concurrent ``report_circuit_result`` has already moved the status
  away from HALF_OPEN. Previously the flag could be stuck at ``True`` and
  ``try_claim_circuit_probe`` would refuse future HALF_OPEN probes forever.
- CHAT-SSE-CTX: ``_stream_chat_response`` SSE Response must wrap its
  generator in ``stream_with_context`` so the generator can still access
  ``current_app``/``request``/``session`` after the WSGI server starts
  streaming.
- SHUTDOWN-IDEMPOTENT: ``AppState.shutdown_executors`` must be idempotent so
  concurrent signal handler + ``atexit`` invocations do not double-close
  SSE announcers, Mistral clients, or chat history connections.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch


class CircuitProbeReleaseResetsFlagAfterStatusChangeTestCase(unittest.TestCase):
    """``release_circuit_probe`` must clear the ``probing`` flag whenever a
    probe was claimed, even if a concurrent ``report_circuit_result`` has
    moved the status to OPEN (or to CLOSED) in the meantime."""

    def test_release_resets_probing_after_status_moved_to_open(self):
        from market_state import MarketDataState

        market = MarketDataState()
        service = "langsearch"
        market.circuit_states[service]["status"] = "HALF_OPEN"
        market.circuit_states[service]["probing"] = False
        market.circuit_states[service]["timeout_streak"] = 0

        # First claimant wins the probe slot.
        self.assertTrue(market.try_claim_circuit_probe(service))
        self.assertTrue(market.circuit_states[service]["probing"])

        # Concurrent failure path moves the circuit back to OPEN.
        market.report_circuit_result(service, success=False, open_sec=30)
        self.assertEqual(market.circuit_states[service]["status"], "OPEN")
        self.assertFalse(market.circuit_states[service]["probing"])

        # Now a defensive release runs (as LangSearch does on 429 paths).
        market.release_circuit_probe(service)

        # The flag must remain False so future HALF_OPEN probes are accepted.
        self.assertFalse(market.circuit_states[service]["probing"])

        # Future HALF_OPEN transitions must allow a fresh probe.
        market.circuit_states[service]["status"] = "OPEN"
        market.circuit_states[service]["open_until"] = 0.0
        self.assertFalse(market.is_circuit_open(service))
        self.assertEqual(market.circuit_states[service]["status"], "HALF_OPEN")
        self.assertTrue(market.try_claim_circuit_probe(service))

    def test_release_is_noop_when_no_probe_was_claimed(self):
        from market_state import MarketDataState

        market = MarketDataState()
        service = "langsearch"
        market.circuit_states[service]["status"] = "OPEN"
        market.circuit_states[service]["probing"] = False

        # No claim happened. The defensive release must not flip probing=True.
        market.release_circuit_probe(service)
        self.assertFalse(market.circuit_states[service]["probing"])

    def test_release_resets_symbol_scoped_probing_after_status_change(self):
        from market_state import MarketDataState

        market = MarketDataState()
        symbol = "us:TESTFIX"
        market.history_circuit_state[symbol] = {
            "status": "HALF_OPEN",
            "open_until": 0.0,
            "probing": False,
            "timeout_streak": 0,
        }

        self.assertTrue(
            market.try_claim_circuit_probe("yfinance_history", symbol=symbol)
        )
        self.assertTrue(market.history_circuit_state[symbol]["probing"])

        market.report_circuit_result(
            "yfinance_history", success=False, symbol=symbol, open_sec=30
        )
        self.assertEqual(market.history_circuit_state[symbol]["status"], "OPEN")

        market.release_circuit_probe("yfinance_history", symbol=symbol)
        self.assertFalse(market.history_circuit_state[symbol]["probing"])


class ChatSseResponseStreamWithContextTestCase(unittest.TestCase):
    """The chat SSE Response must wrap its generator in
    ``stream_with_context`` so the generator can still touch Flask context
    (``current_app``, ``request``, ``session``, ``g``) while streaming."""

    def test_stream_with_context_is_applied_to_chat_response(self):
        import inspect

        from routes import api_analysis

        source = inspect.getsource(api_analysis._stream_chat_response)
        self.assertIn(
            "stream_with_context(generate())",
            source,
            "Chat SSE generator must be wrapped in stream_with_context so "
            "request-context-bound Flask globals remain accessible.",
        )


class ShutdownExecutorsIdempotencyTestCase(unittest.TestCase):
    """``AppState.shutdown_executors`` must be safe to call more than once.

    The signal handler, ``atexit`` hook, and ``/api/shutdown`` route can all
    invoke cleanup concurrently. Repeat invocations must short-circuit
    instead of double-closing SSE announcers, Mistral clients, and chat
    history connections."""

    def test_second_invocation_short_circuits_cleanup(self):
        from app_state import AppState

        fresh = AppState()
        try:
            yf_close = MagicMock()
            rt_stop = MagicMock()
            fresh.fallback_provider.close = yf_close
            with patch(
                "services.realtime_engine.realtime_market_engine.stop",
                rt_stop,
                create=True,
            ):
                fresh.shutdown_executors()
                fresh.shutdown_executors()

            # The second call must not re-run cleanup hooks.
            self.assertEqual(yf_close.call_count, 1)
        finally:
            # Reset idempotency flag so the teardown in test fixtures still
            # runs the cleanup the first time if it gets called.
            fresh._shutdown_executors_done = False
            fresh.shutdown_executors()

    def test_idempotency_flag_is_set(self):
        from app_state import AppState

        fresh = AppState()
        try:
            self.assertFalse(getattr(fresh, "_shutdown_executors_done", False))
            fresh.shutdown_executors()
            self.assertTrue(getattr(fresh, "_shutdown_executors_done", False))
        finally:
            fresh._shutdown_executors_done = False
            fresh.shutdown_executors()


if __name__ == "__main__":
    unittest.main()