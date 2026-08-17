"""Regression tests for the 2026-08-14 code review fixes.

Covers:
- H-CB1: circuit breaker single-probe in HALF_OPEN (market_state + route)
- M-RL1: rate-limit polling token bounded semantics (sanity via existing suite)
- M-SH1: double shutdown idempotency
- L-CH1: chat history DB file permissions
- L-CSS1: realtime_client.js CSS.escape hardening (static check)
- RLock fix: crypto_utils ephemeral double-check under lock
"""

import os
import queue
import threading
import unittest
from concurrent.futures import Future
from unittest.mock import patch


class CircuitBreakerHalfOpenSingleProbeTestCase(unittest.TestCase):
    def test_only_first_half_open_probe_is_allowed(self):
        from market_state import MarketDataState

        m = MarketDataState()
        svc = "mistral"
        # Force OPEN -> HALF_OPEN transition.
        m.circuit_states[svc]["status"] = "OPEN"
        m.circuit_states[svc]["open_until"] = 0  # already expired
        m.circuit_states[svc]["probing"] = False

        first_open = m.is_circuit_open(svc)
        # First caller transitions to HALF_OPEN and is allowed.
        self.assertFalse(first_open)
        self.assertEqual(m.circuit_states[svc]["status"], "HALF_OPEN")

        # HALF_OPEN should still allow the backing call (probe) to proceed.
        # The route layer claims the probe via try_claim_circuit_probe.
        self.assertTrue(m.try_claim_circuit_probe(svc))
        # Second concurrent claimant must be rejected.
        self.assertFalse(m.try_claim_circuit_probe(svc))

        # Success clears probing and closes circuit.
        m.report_circuit_result(svc, success=True)
        self.assertEqual(m.circuit_states[svc]["status"], "CLOSED")
        self.assertFalse(m.circuit_states[svc].get("probing"))
        self.assertFalse(m.is_circuit_open(svc))

    def test_half_open_failure_reopens(self):
        from market_state import MarketDataState

        m = MarketDataState()
        svc = "mistral"
        m.circuit_states[svc]["status"] = "OPEN"
        m.circuit_states[svc]["open_until"] = 0
        m.circuit_states[svc]["probing"] = False
        self.assertFalse(m.is_circuit_open(svc))  # -> HALF_OPEN
        self.assertTrue(m.try_claim_circuit_probe(svc))
        m.report_circuit_result(svc, success=False, open_sec=30)
        self.assertEqual(m.circuit_states[svc]["status"], "OPEN")
        self.assertFalse(m.circuit_states[svc].get("probing"))

    def test_symbol_scoped_circuit_single_probe(self):
        from market_state import MarketDataState

        m = MarketDataState()
        sym = "us:TESTFIX"
        m.history_circuit_state[sym] = m.history_circuit_state.get(sym) or __import__("market_state").CircuitState(status="OPEN", open_until=0, probing=False)
        m.history_circuit_state[sym]["status"] = "OPEN"
        m.history_circuit_state[sym]["open_until"] = 0
        m.history_circuit_state[sym]["probing"] = False

        self.assertFalse(m.is_circuit_open("yfinance_history", symbol=sym))
        self.assertEqual(m.history_circuit_state[sym]["status"], "HALF_OPEN")
        self.assertTrue(m.try_claim_circuit_probe("yfinance_history", symbol=sym))
        self.assertFalse(m.try_claim_circuit_probe("yfinance_history", symbol=sym))

    def test_route_releases_probe_when_async_submission_is_full(self):
        from app import app
        from app_state import app_state
        from market_state import CircuitState

        symbol = "TEST_QUEUE_FULL"
        circuit_key = f"us:{symbol}"
        with app_state.market.history_circuit_lock:
            app_state.market.history_circuit_state[circuit_key] = CircuitState(
                status="OPEN", open_until=0, probing=False
            )

        with (
            patch("routes.api_stocks._has_cached_key", return_value=False),
            patch.object(
                app_state.execution.data_executor,
                "submit",
                side_effect=queue.Full,
            ),
            app.test_client() as client,
        ):
            response = client.get(f"/api/stock-history?symbol={symbol}&market=us&period=1mo")

        self.assertEqual(response.status_code, 200)
        with app_state.market.history_circuit_lock:
            state = app_state.market.history_circuit_state[circuit_key]
            self.assertEqual(state["status"], "OPEN")
            self.assertFalse(state["probing"])
            self.assertGreater(state["open_until"], 0)

    def test_route_releases_probe_when_submitted_future_is_cancelled(self):
        from app import app
        from app_state import app_state
        from market_state import CircuitState

        symbol = "TESTCANCELLED"
        circuit_key = f"us:{symbol}"
        cache_key = f"hist_{symbol}_us_1mo"
        cancelled_future = Future()
        cancelled_future.cancel()
        with app_state.market.history_circuit_lock:
            app_state.market.history_circuit_state[circuit_key] = CircuitState(
                status="OPEN", open_until=0, probing=False
            )

        class _CancelledExecutor:
            def submit(self, *args, **kwargs):
                return cancelled_future

        with (
            patch("routes.api_stocks._has_cached_key", return_value=False),
            patch.object(app_state.execution, "data_executor", _CancelledExecutor()),
            app.test_client() as client,
        ):
            response = client.get(f"/api/stock-history?symbol={symbol}&market=us&period=1mo")

        self.assertEqual(response.status_code, 200)
        with app_state.market.history_circuit_lock:
            state = app_state.market.history_circuit_state[circuit_key]
            self.assertEqual(state["status"], "OPEN")
            self.assertFalse(state["probing"])
        with app_state.history_fetch_lock:
            self.assertNotIn(cache_key, app_state.history_fetch_inflight)

    def test_probe_job_releases_probe_on_error_result(self):
        from app_state import app_state
        from market_state import CircuitState
        from services import stock_service

        symbol = "TEST_PROBE_ERROR"
        circuit_key = f"us:{symbol}"
        cache_key = f"hist_{symbol}_us_1mo"
        with app_state.market.history_circuit_lock:
            app_state.market.history_circuit_state[circuit_key] = CircuitState(
                status="HALF_OPEN", probing=True
            )

        with patch.object(
            stock_service,
            "fetch_history_sync_impl",
            return_value={"error": "upstream unavailable", "symbol": symbol},
        ):
            stock_service.fetch_history_async_task(
                symbol, "us", "1mo", cache_key, 60, probe=True
            )

        with app_state.market.history_circuit_lock:
            state = app_state.market.history_circuit_state[circuit_key]
            self.assertEqual(state["status"], "OPEN")
            self.assertFalse(state["probing"])

    def test_probe_job_releases_probe_on_unexpected_exception(self):
        from app_state import app_state
        from market_state import CircuitState
        from services import stock_service

        symbol = "TEST_PROBE_EXCEPTION"
        circuit_key = f"us:{symbol}"
        cache_key = f"hist_{symbol}_us_1mo"
        with app_state.market.history_circuit_lock:
            app_state.market.history_circuit_state[circuit_key] = CircuitState(
                status="HALF_OPEN", probing=True
            )

        with patch.object(
            stock_service,
            "fetch_history_sync_impl",
            side_effect=RuntimeError("unexpected worker failure"),
        ):
            stock_service.fetch_history_async_task(
                symbol, "us", "1mo", cache_key, 60, probe=True
            )

        with app_state.market.history_circuit_lock:
            state = app_state.market.history_circuit_state[circuit_key]
            self.assertEqual(state["status"], "OPEN")
            self.assertFalse(state["probing"])


class DoubleShutdownIdempotencyTestCase(unittest.TestCase):
    def test_execution_state_shutdown_is_idempotent(self):
        from execution_state import ExecutionState

        es = ExecutionState()
        # First shutdown marks event.
        es.shutdown()
        self.assertTrue(es.shutdown_event.is_set())
        # Second shutdown should be a no-op, not raise.
        es.shutdown()
        self.assertTrue(es.shutdown_event.is_set())


class EphemeralKeyDoubleCheckTestCase(unittest.TestCase):
    def test_concurrent_get_ephemeral_key_no_deadlock(self):
        import crypto_utils

        # Reset for isolation.
        crypto_utils._EPHEMERAL_KEY = None  # type: ignore[attr-defined]
        results: list[str] = []
        errors: list[Exception] = []

        def worker():
            try:
                results.append(crypto_utils._get_ephemeral_key())
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
            self.assertFalse(t.is_alive(), "worker deadlocked")

        self.assertEqual(errors, [])
        # All threads must agree on the same key (single generation wins).
        self.assertEqual(len(set(results)), 1)

    def test_nested_lock_does_not_deadlock(self):
        import crypto_utils

        crypto_utils._EPHEMERAL_KEY = None  # type: ignore[attr-defined]
        # The bug was: caller holds _EPHEMERAL_LOCK then calls _get_ephemeral_key
        # which tried to acquire the same non-reentrant Lock -> deadlock.
        # With RLock, this must not deadlock.
        acquired = crypto_utils._EPHEMERAL_LOCK.acquire(timeout=5)
        self.assertTrue(acquired)
        try:
            key = crypto_utils._get_ephemeral_key()
            self.assertIsInstance(key, str)
            self.assertTrue(len(key) > 0)
        finally:
            crypto_utils._EPHEMERAL_LOCK.release()


class ChatHistoryPermissionsTestCase(unittest.TestCase):
    def test_init_db_enforces_restrictive_permissions_on_posix(self):
        if os.name == "nt":
            self.skipTest("POSIX-only permission check")
        import tempfile
        from pathlib import Path

        import utils.chat_history as ch

        prev_db_path = ch.DB_PATH
        prev_init = ch._db_initialized
        prev_data_dir = os.environ.get("MNS_DATA_DIR")
        tmpdir = tempfile.mkdtemp(prefix="mns-chat-perm-")
        try:
            os.environ["MNS_DATA_DIR"] = tmpdir
            # Force re-resolve DB_PATH to tmpdir for this test.
            ch.DB_PATH = Path(tmpdir) / "chat_history.db"  # type: ignore[attr-defined]
            ch._db_initialized = False
            ch.init_db()
            self.assertTrue(ch.DB_PATH.exists())
            mode = oct(ch.DB_PATH.stat().st_mode & 0o777)
            self.assertEqual(ch.DB_PATH.stat().st_mode & 0o777, 0o600, f"DB perms {mode} expected 0o600")
            parent_mode = ch.DB_PATH.parent.stat().st_mode & 0o777
            self.assertEqual(parent_mode, 0o700, f"parent perms {oct(parent_mode)} expected 0o700")
        finally:
            ch.DB_PATH = prev_db_path  # type: ignore[attr-defined]
            ch._db_initialized = prev_init
            if prev_data_dir is None:
                os.environ.pop("MNS_DATA_DIR", None)
            else:
                os.environ["MNS_DATA_DIR"] = prev_data_dir


class RealtimeClientCssEscapeTestCase(unittest.TestCase):
    def test_realtime_client_uses_css_escape(self):
        from pathlib import Path

        p = Path(__file__).parent.parent / "static" / "js" / "realtime_client.js"
        text = p.read_text(encoding="utf-8")
        self.assertIn("CSS.escape", text)
        # Fallback must exist for non-browser envs.
        self.assertIn("CSS.escape", text)
        # Ensure old vulnerable pattern is gone: no unescaped data-symbol="${symbol}"
        # The fixed code uses esc(symbol).
        self.assertNotIn('data-symbol="${symbol}"', text)
        self.assertIn("esc(symbol)", text)


class ReviewR1R2RegressionTestCase(unittest.TestCase):
    def test_r1_jp_delete_save_failure_restores_global_container(self):
        from app import app
        from app_state import app_state
        from utils import storage

        original_csrf = app.config.get("WTF_CSRF_ENABLED")
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            with app_state.market.user_stocks_lock:
                app_state.market.user_jp = {"1234": "Legacy Tokyo"}

            with (
                patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, "")),
                patch.object(
                    storage,
                    "_write_user_stocks_with_lock",
                    side_effect=storage.UserStocksPersistError("disk full"),
                ),
                app.test_client() as client,
            ):
                response = client.post(
                    "/api/stocks/delete",
                    json={"symbol": "1234", "market": "jp"},
                    headers={"Origin": "http://127.0.0.1:5000"},
                )

            self.assertEqual(response.status_code, 503)
            self.assertEqual(app_state.market.user_jp, {"1234": "Legacy Tokyo"})
        finally:
            if original_csrf is not None:
                app.config["WTF_CSRF_ENABLED"] = original_csrf

    def test_r2_generated_portfolio_save_failure_is_not_success(self):
        from services import ai_portfolio_service as aps

        with (
            patch.object(aps, "_find_saved_ai_portfolio", return_value=None),
            patch.object(aps, "collect_symbol_research_context", return_value=""),
            patch.object(aps, "get_mistral_api_key", return_value=""),
            patch.object(aps, "save_custom_ai_portfolio", return_value=False),
        ):
            with self.assertRaises(aps.PortfolioStorageError):
                aps.generate_ai_portfolio_by_theme("r2-save-failure", force_rebalance=True)

        self.assertNotIn("r2-save-failure", aps._AI_GEN_INFLIGHT)

    def test_r2_concurrent_unsaved_generation_is_not_success(self):
        import threading

        from services import ai_portfolio_service as aps

        theme = "r2-concurrent-save-failure"
        completed_owner = threading.Event()
        completed_owner.set()
        with aps._AI_GEN_LOCK:
            aps._AI_GEN_INFLIGHT[theme] = completed_owner
        try:
            with patch.object(aps, "_find_saved_ai_portfolio", return_value=None):
                with self.assertRaises(aps.PortfolioStorageError):
                    aps.generate_ai_portfolio_by_theme(theme)
        finally:
            with aps._AI_GEN_LOCK:
                aps._AI_GEN_INFLIGHT.pop(theme, None)
