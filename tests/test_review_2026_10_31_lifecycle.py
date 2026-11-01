"""Regression tests for the lifecycle/CI review layer (LIFECYCLE-1..15).

Scratch report: scratch/review/lifecycle.md; resolutions:
scratch/review/lifecycle_resolution.md.

Every FIXED issue gets trigger + edge/abnormal + retained-behavior coverage;
NO ACTION issues are documented with concrete evidence (measured numbers or
the pinning test that owns the behavior). No test here sends a real signal to
a live process, touches the network, or skips.
"""

from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# LIFECYCLE-1 (FIXED) — Windows liveness probe must never TerminateProcess
# ---------------------------------------------------------------------------


class TestPidIsAliveNeverSignalsForeignPids:
    """bg/leader_election._pid_is_alive: os.kill fallback was Windows-unsafe."""

    def test_windows_without_psutil_never_calls_os_kill(self, monkeypatch):
        """On Windows without psutil, os.kill must NOT be probed (it would
        TerminateProcess the target); the result is a conservative False."""
        import bg.leader_election as le

        kill_calls: list[tuple[int, int]] = []

        def _record_kill(pid, sig):
            kill_calls.append((pid, sig))

        monkeypatch.setattr(le.os, "name", "nt")
        monkeypatch.setattr(le.os, "kill", _record_kill)
        monkeypatch.setitem(sys.modules, "psutil", None)  # import -> ImportError

        assert le._pid_is_alive(os.getpid()) is False
        assert kill_calls == [], "os.kill must never be invoked on Windows"

    def test_windows_without_psutil_logs_actionable_warning(self, monkeypatch, caplog):
        import logging

        import bg.leader_election as le

        monkeypatch.setattr(le.os, "name", "nt")
        monkeypatch.setitem(sys.modules, "psutil", None)
        with caplog.at_level(logging.WARNING, logger="bg.leader_election"):
            assert le._pid_is_alive(4242) is False
        assert any("psutil" in rec.getMessage() for rec in caplog.records)

    def test_posix_kill0_probe_semantics_preserved(self, monkeypatch):
        """POSIX keeps the documented non-destructive kill(pid, 0) probe."""
        import bg.leader_election as le

        calls: list[tuple[int, int]] = []

        monkeypatch.setattr(le.os, "name", "posix")
        monkeypatch.setitem(sys.modules, "psutil", None)

        def _alive_kill(pid, sig):
            calls.append((pid, sig))

        monkeypatch.setattr(le.os, "kill", _alive_kill)
        assert le._pid_is_alive(1234) is True
        assert calls == [(1234, 0)]

        def _dead_kill(pid, sig):
            raise ProcessLookupError(pid)

        monkeypatch.setattr(le.os, "kill", _dead_kill)
        assert le._pid_is_alive(4321) is False

    def test_posix_eperm_counts_as_alive(self, monkeypatch):
        """EPERM means the process exists but belongs to another user."""
        import bg.leader_election as le

        monkeypatch.setattr(le.os, "name", "posix")
        monkeypatch.setitem(sys.modules, "psutil", None)

        def _eperm_kill(pid, sig):
            raise PermissionError(pid)

        monkeypatch.setattr(le.os, "kill", _eperm_kill)
        assert le._pid_is_alive(777) is True

    def test_psutil_path_unaffected_and_nonpositive_pid_is_dead(self):
        import bg.leader_election as le

        # psutil is a hard runtime dependency; the fast path stays intact.
        assert le._pid_is_alive(os.getpid()) is True
        assert le._pid_is_alive(-1) is False
        assert le._pid_is_alive(0) is False


# ---------------------------------------------------------------------------
# LIFECYCLE-2 (FIXED) — app-level signal handlers must not override WSGI servers
# ---------------------------------------------------------------------------


class TestSignalHandlerWsgiServerOwnership:
    def test_handlers_not_registered_under_gunicorn(self):
        """With gunicorn loaded (worker or --preload master), the app must not
        replace the server's SIGTERM/SIGINT handlers."""
        from app_factory import wiring

        orig_term = signal.getsignal(signal.SIGTERM)
        orig_int = signal.getsignal(signal.SIGINT)
        try:
            with (
                patch.object(wiring, "_is_testing", return_value=False),
                patch.dict(sys.modules, {"gunicorn": ModuleType("gunicorn")}),
            ):
                wiring._register_signal_handlers(MagicMock())
            assert signal.getsignal(signal.SIGTERM) is orig_term
            assert signal.getsignal(signal.SIGINT) is orig_int
        finally:
            signal.signal(signal.SIGTERM, orig_term)
            signal.signal(signal.SIGINT, orig_int)

    def test_handlers_not_registered_under_uwsgi(self):
        from app_factory import wiring

        orig_term = signal.getsignal(signal.SIGTERM)
        try:
            with (
                patch.object(wiring, "_is_testing", return_value=False),
                patch.dict(sys.modules, {"uwsgi": ModuleType("uwsgi")}),
            ):
                wiring._register_signal_handlers(MagicMock())
            assert signal.getsignal(signal.SIGTERM) is orig_term
        finally:
            signal.signal(signal.SIGTERM, orig_term)

    def test_dev_server_still_registers_handlers(self):
        """Retained behavior: without an external WSGI server the custom
        handler is installed (Werkzeug dev server path)."""
        from app_factory import wiring

        saved = {
            name: sys.modules.pop(name) for name in ("gunicorn", "uwsgi") if name in sys.modules
        }
        orig_term = signal.getsignal(signal.SIGTERM)
        try:
            with patch.object(wiring, "_is_testing", return_value=False):
                wiring._register_signal_handlers(MagicMock())
            installed = signal.getsignal(signal.SIGTERM)
            assert installed is not orig_term
            assert callable(installed)
        finally:
            signal.signal(signal.SIGTERM, orig_term)
            sys.modules.update(saved)

    def test_testing_mode_still_skips_registration(self):
        """Retained behavior, pinned by
        tests/test_pytest_detection_unification_u3_2026_10_23.py:110-114:
        under pytest no signal is touched at all."""
        from app_factory import wiring

        with (
            patch.object(wiring, "_is_testing", return_value=True),
            patch("signal.signal", side_effect=AssertionError("must not arm")),
        ):
            assert wiring._register_signal_handlers(MagicMock()) is None


# ---------------------------------------------------------------------------
# LIFECYCLE-3 (FIXED) — preload disabled; bootstrap state keyed to the PID
# ---------------------------------------------------------------------------


class TestBootstrapForkSafety:
    def test_gunicorn_config_disables_preload(self):
        spec = importlib.util.spec_from_file_location(
            "gunicorn_conf_lifecycle_test", ROOT / "gunicorn.conf.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.preload_app is False
        # Retained contract: single worker only.
        assert module.workers == 1

    def test_fresh_process_defaults_are_follower_and_unbootstrapped(self, tmp_path):
        """Fork-inherited state must be detectable: the import-time bootstrap
        flag is False and the leader default is 'follower until elected'."""
        env = dict(os.environ)
        env.update(
            {
                "MNS_DATA_DIR": str(tmp_path),
                "MNS_APP_DATA_DIR": str(tmp_path),
                "MNS_SKIP_BOOTSTRAP": "1",
                "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
            }
        )
        code = (
            "import app;"
            "assert app._app_bootstrap_done is False;"
            "assert app._app_bootstrap_pid is None;"
            "import bg.leader_election as le;"
            "assert le._is_sync_leader is False, 'default must be follower';"
            "import app_bg;"
            "assert app_bg._is_sync_leader is False"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr

    def test_bootstrap_noop_when_flag_and_pid_match(self, monkeypatch):
        """Retained idempotence: same-PID (or legacy None-PID) done flag is a
        no-op — no threads are started again."""
        import app as app_module
        from app import bootstrap

        monkeypatch.delenv("MNS_SKIP_BOOTSTRAP", raising=False)
        with app_module._app_bootstrap_lock:
            was_done = app_module._app_bootstrap_done
            was_pid = app_module._app_bootstrap_pid
            app_module._app_bootstrap_done = True
            app_module._app_bootstrap_pid = os.getpid()
        try:
            with patch("app._start_background_threads") as start_mock:
                bootstrap(app_module.app)
            start_mock.assert_not_called()
            # Legacy state (done=True, pid=None) also stays a no-op.
            with app_module._app_bootstrap_lock:
                app_module._app_bootstrap_pid = None
            with patch("app._start_background_threads") as start_mock2:
                bootstrap(app_module.app)
            start_mock2.assert_not_called()
        finally:
            with app_module._app_bootstrap_lock:
                app_module._app_bootstrap_done = was_done
                app_module._app_bootstrap_pid = was_pid

    def test_bootstrap_reruns_in_forked_child(self, monkeypatch):
        """A done flag recorded by another PID (fork/preload) must trigger a
        full re-run in this process, and re-record this PID."""
        import app as app_module
        from app import app as flask_app
        from app import bootstrap

        monkeypatch.delenv("MNS_SKIP_BOOTSTRAP", raising=False)
        with app_module._app_bootstrap_lock:
            was_done = app_module._app_bootstrap_done
            was_pid = app_module._app_bootstrap_pid
            app_module._app_bootstrap_done = True
            app_module._app_bootstrap_pid = os.getpid() + 10_000_000
        try:
            with patch.dict(os.environ, {"MNS_SKIP_BOOTSTRAP": ""}, clear=False):
                with (
                    patch("utils.worker_validation.enforce_single_worker"),
                    patch.object(
                        app_module.app_state, "get_or_create_shutdown_token", return_value="tok"
                    ),
                    patch.object(app_module.app_state, "initialize_yfinance_cache"),
                    patch("utils.storage.load_user_stocks"),
                    patch("app._start_background_threads") as start_mock,
                    patch.object(
                        app_module.app_state.execution, "sync_refresh_executor", MagicMock()
                    ),
                    patch.object(app_module.app_state.execution, "news_executor", MagicMock()),
                ):
                    bootstrap(flask_app)
                    start_mock.assert_called_once()
            assert app_module._app_bootstrap_done is True
            assert app_module._app_bootstrap_pid == os.getpid()
        finally:
            with app_module._app_bootstrap_lock:
                app_module._app_bootstrap_done = was_done
                app_module._app_bootstrap_pid = was_pid
            app_module.app_state.bootstrap_ready.clear()
            os.environ["MNS_SKIP_BOOTSTRAP"] = "1"

    def test_first_request_guard_reruns_after_fork(self, monkeypatch):
        """The before_request safety net re-bootstraps when the done flag was
        recorded by a different PID, and stays quiet for the current PID."""
        import app as app_module

        monkeypatch.delenv("MNS_SKIP_BOOTSTRAP", raising=False)
        bootstrap_mock = MagicMock()
        with app_module._app_bootstrap_lock:
            was_done = app_module._app_bootstrap_done
            was_pid = app_module._app_bootstrap_pid
            app_module._app_bootstrap_done = True
            app_module._app_bootstrap_pid = os.getpid() + 10_000_000
        try:
            with (
                patch.object(app_module, "bootstrap", bootstrap_mock),
                app_module.app.test_request_context("/"),
            ):
                app_module._ensure_bootstrap_called()
                bootstrap_mock.assert_called_once()

                # Same PID: guard is a no-op.
                with app_module._app_bootstrap_lock:
                    app_module._app_bootstrap_pid = os.getpid()
                app_module._ensure_bootstrap_called()
                bootstrap_mock.assert_called_once()
        finally:
            with app_module._app_bootstrap_lock:
                app_module._app_bootstrap_done = was_done
                app_module._app_bootstrap_pid = was_pid


# ---------------------------------------------------------------------------
# LIFECYCLE-4 (NO ACTION) — coverage gate not inflated by .venv (measured)
# ---------------------------------------------------------------------------


class TestCoverageGateEvidence:
    def test_cov_gate_and_scope_unchanged(self):
        """LIFECYCLE-4 NO ACTION evidence: measured with the pinned coverage
        (7.16.2, full suite via ``--cov=.``) the coverage.xml contained ZERO
        ``.venv``/``site-packages`` files: all=24909 lines / 20055 covered =
        80.51%, project-only identical (>= the 75% gate). Modern coverage.py
        auto-excludes the active venv even when its directory sits under the
        source root, so the report's inflation scenario is not reproducible
        with the pinned toolchain. The gate and scope must stay as they are —
        this test pins them against accidental weakening or drift."""
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "--cov=." in ci
        assert "--cov-fail-under=75" in ci
        import tomllib

        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        omit = data["tool"]["coverage"]["run"]["omit"]
        assert "tests/*" in omit
        assert ".mimocode/*" in omit


# ---------------------------------------------------------------------------
# LIFECYCLE-5 (FIXED) — watchdog loop exception isolation
# ---------------------------------------------------------------------------


class TestWatchdogExceptionIsolation:
    def _capture_watchdog(self):
        """Run start_background_worker with all side effects mocked and
        return the watchdog loop target captured from the fake threads."""
        import app_bg
        from app_state import app_state
        from bg.sync_worker import start_background_worker
        from services.realtime_engine import realtime_market_engine

        captured: list[object] = []

        def fake_thread_factory(target=None, args=(), daemon=True, name=None, **kwargs):
            mock_t = MagicMock()
            mock_t.target = target
            mock_t.args = args
            mock_t.name = name
            captured.append(mock_t)
            return mock_t

        registered: list[object] = []
        with (
            patch.object(app_bg, "bg_yahoo_fetch_loop", MagicMock()),
            patch.object(app_bg, "bg_leader_election_loop", MagicMock()),
            patch.object(app_bg, "bg_interpolate_loop", MagicMock()),
            patch.object(app_bg, "load_user_stocks", MagicMock()),
            patch.object(threading, "Thread", side_effect=fake_thread_factory),
            patch.object(realtime_market_engine, "register_symbols"),
            patch.object(realtime_market_engine, "start"),
            patch.object(app_state.execution, "background_threads", registered),
        ):
            start_background_worker()

        watchdog_threads = [t for t in captured if t.name == "Watchdog"]
        assert len(watchdog_threads) == 1
        return watchdog_threads[0], registered

    def test_watchdog_survives_exception_and_keeps_supervising(self):
        """A raising realtime-engine watchdog call must not kill the
        supervision loop: the next tick still runs."""
        import app_bg
        from app_state import app_state
        from bg.sync.state import _BG_LOOP_MARKER_ATTR

        watchdog_thread, _registered = self._capture_watchdog()
        assert getattr(watchdog_thread, _BG_LOOP_MARKER_ATTR, None) == "Watchdog"
        watchdog_target = watchdog_thread.target
        assert callable(watchdog_target)

        watchdog_calls = {"n": 0}

        def _raising_then_ok(engine=None):
            watchdog_calls["n"] += 1
            if watchdog_calls["n"] == 1:
                raise RuntimeError("simulated engine-restart failure")
            return []

        class _SequencedShutdown:
            """Allows exactly two loop iterations, then reports shutdown.

            Two ``is_set()`` calls happen per iteration (while-check + break-check).
            """

            def __init__(self, iterations: int = 2):
                self._checks_left = iterations * 2

            def is_set(self) -> bool:
                if self._checks_left > 0:
                    self._checks_left -= 1
                    return False
                return True

            def wait(self, timeout=None):
                return None

        fake_event = _SequencedShutdown(iterations=2)
        with (
            patch.object(app_state.execution, "shutdown_event", fake_event),
            patch.object(
                app_bg, "_watchdog_restart_dead_realtime_engine", side_effect=_raising_then_ok
            ),
        ):
            # Before the fix this raised out of the first tick and killed the
            # supervision thread; now both ticks complete.
            watchdog_target()

        assert watchdog_calls["n"] == 2, "second tick must run after the first raised"

    def test_watchdog_thread_is_registered_for_lifecycle_tracking(self):
        """Retained behavior: the watchdog thread is marker-tagged and
        registered like the other background loops."""
        from bg.sync.state import _BG_LOOP_MARKER_ATTR

        watchdog_thread, registered = self._capture_watchdog()
        assert getattr(watchdog_thread, _BG_LOOP_MARKER_ATTR, None) == "Watchdog"
        assert watchdog_thread in registered


# ---------------------------------------------------------------------------
# LIFECYCLE-6 (NO ACTION) — announce is strictly non-blocking (falsified)
# ---------------------------------------------------------------------------


class TestAnnouncerNonBlockingEvidence:
    def test_full_queue_evicts_listener_without_blocking(self):
        """LIFECYCLE-6 NO ACTION evidence: ``MessageAnnouncer.announce`` uses
        ``put_nowait`` exclusively (messaging.py:119-175) and evicts overloaded
        listeners, so it cannot stall while ``_sse_payload_lock`` is held; the
        in-lock announce is additionally required for ordering
        (bg/common.py:237-242). A full listener queue must not block."""
        from messaging import MessageAnnouncer

        announcer = MessageAnnouncer()
        queue = announcer.listen(maxsize=1)
        queue.put_nowait("existing-frame")

        finished = threading.Event()

        def _announce():
            announcer.announce(("seq-1", "data: {}\n\n"))
            finished.set()

        worker = threading.Thread(target=_announce, daemon=True)
        worker.start()
        worker.join(timeout=2.0)
        assert finished.is_set(), "announce must not block on a full listener queue"
        assert announcer.dropped_count == 1
        assert announcer.listeners == []
        # The evicted listener receives the None reconnect sentinel.
        assert queue.get_nowait() is None


# ---------------------------------------------------------------------------
# LIFECYCLE-7 (FIXED) — follower until elected
# ---------------------------------------------------------------------------


class TestFollowerUntilElected:
    def test_follower_path_taken_before_election_completes(self):
        """While the election loop has not promoted this process, the sync
        entry point must take the safe disk-warm follower path (no upstream
        fetches), and the election loop must promote once the lock is won."""
        import app_bg
        import bg.leader_election as le
        from app_state import app_state

        warm_mock = MagicMock()
        inval_mock = MagicMock()
        ann_mock = MagicMock()
        fetch_mock = MagicMock()

        class _FakeLock:
            def acquire(self, blocking=True, timeout=-1):
                return True

            def release(self):
                pass

        le._set_is_sync_leader(False)  # pre-election state (the fixed default)
        with (
            patch.object(app_bg, "_is_sync_leader", False),
            patch.object(app_bg, "_sync_execution_lock", _FakeLock()),
            patch.object(app_bg, "_warm_payload_cache_from_disk", warm_mock),
            patch.object(app_bg, "_invalidate_sse_payload_cache", inval_mock),
            patch.object(app_bg, "announce_current_market_state", ann_mock),
            patch("app_bg.fetch_stocks_batch", fetch_mock),
        ):
            app_bg.sync_all_stocks_now()

        warm_mock.assert_called_once()
        fetch_mock.assert_not_called()

        # Election loop promotes as soon as the leader lock is acquired.
        shutdown = app_state.execution.shutdown_event
        already_set = shutdown.is_set()
        try:
            shutdown.set()  # keep the loop from entering its wait cycle
            with patch.object(le, "_try_acquire_leader_lock", return_value=True):
                le.bg_leader_election_loop()
            assert le.is_leader() is True
        finally:
            if not already_set:
                shutdown.clear()
            le._set_is_sync_leader(False)
            app_bg._is_sync_leader = False


# ---------------------------------------------------------------------------
# LIFECYCLE-8 (NO ACTION) — duplicated mirror implementations are pinned
# ---------------------------------------------------------------------------


class TestSyncMirrorEquivalenceEvidence:
    def test_stale_recovery_entrypoints_are_equivalent(self):
        """LIFECYCLE-8 NO ACTION evidence: ``app_bg`` keeps full
        implementations for ``patch("app_bg.X")`` compatibility — deliberately
        pinned by tests/test_sync_stale_recovery.py:193-228, which loads a
        FRESH app_bg module copy and requires it to honor its own
        ``_sync_start_time``/``_run_scheduled_sync_job`` globals (delegating to
        bg.sync.* would read the canonical mirror instead and break the
        contract). Both entry points share the mirror and must produce the same
        observable effects."""
        import app_bg
        from app_state import app_state
        from bg.sync import state as sync_state

        def _set_stale() -> None:
            with app_state.market.is_syncing_lock:
                app_state.market.is_syncing = True
            app_bg._sync_start_time = time.time() - (app_bg.SYNC_STALE_TIMEOUT_SEC + 10)

        # Entry point 1: the app_bg copy.
        _set_stale()
        assert app_bg._recover_stale_sync_state_if_needed() is True
        with app_state.market.is_syncing_lock:
            assert app_state.market.is_syncing is False
        assert app_bg._sync_start_time == 0.0
        assert sync_state._sync_start_time == 0.0  # setter wrote through both sides

        # Entry point 2: the bg.sync.state original (delegating through the
        # mirror) — identical observable effects.
        _set_stale()
        assert sync_state._recover_stale_sync_state_if_needed() is True
        with app_state.market.is_syncing_lock:
            assert app_state.market.is_syncing is False
        assert app_bg._sync_start_time == 0.0


# ---------------------------------------------------------------------------
# LIFECYCLE-9 (FIXED) — run_app.bat exit-code propagation
# ---------------------------------------------------------------------------


class TestRunAppBatExitCode:
    def test_uv_failure_exit_code_propagates(self, tmp_path):
        """A ``uv`` that exits 3 must surface as the batch file's exit code.
        Before the fix, ``exit /b %errorlevel%`` inside the parenthesized
        block was expanded at parse time (to the ``where uv`` result, 0), so
        the launcher always reported success."""
        bat = ROOT / "run_app.bat"
        content = bat.read_text(encoding="utf-8")

        if os.name != "nt":
            # cmd.exe is unavailable on this platform: pin the restructured
            # (top-level, parse-time-safe) form instead of executing it.
            assert "if not errorlevel 1 (" not in content
            lines = content.splitlines()
            run_idx = next(i for i, line in enumerate(lines) if line.startswith("uv run "))
            assert lines[run_idx + 1] == "exit /b %errorlevel%"
            assert lines[-1] == "exit /b %errorlevel%"
            return

        shim_dir = tmp_path / "shim"
        shim_dir.mkdir()
        (shim_dir / "uv.cmd").write_text("@echo off\r\nexit /b 3\r\n", encoding="utf-8")

        env = dict(os.environ)
        env["PATH"] = str(shim_dir) + os.pathsep + env["PATH"]
        env.pop("MNS_ALLOW_UNLOCKED", None)

        result = subprocess.run(
            ["cmd", "/c", str(bat)],
            cwd=str(tmp_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 3, (result.returncode, result.stdout, result.stderr)

    def test_missing_uv_without_optout_still_fails_closed(self, tmp_path):
        """Retained behavior: no uv and no MNS_ALLOW_UNLOCKED -> exit 1."""
        bat = ROOT / "run_app.bat"
        if os.name != "nt":
            content = bat.read_text(encoding="utf-8")
            assert "Refusing to start without lockfile" in content
            assert "exit /b 1" in content
            return

        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        env = dict(os.environ)
        # Hide uv by using an empty PATH (cmd built-ins still work).
        env["PATH"] = str(empty_dir)
        env.pop("MNS_ALLOW_UNLOCKED", None)
        result = subprocess.run(
            ["cmd", "/c", str(bat)],
            cwd=str(tmp_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 1


# ---------------------------------------------------------------------------
# LIFECYCLE-10 (FIXED) — CI pylint must cover the lifecycle packages
# ---------------------------------------------------------------------------


class TestCiPylintTargets:
    def test_pylint_step_covers_lifecycle_packages(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        pylint_line = next(line for line in ci.splitlines() if "pylint --errors-only" in line)
        for target in (
            "app_factory/",
            "bg/",
            "app_bg.py",
            "wsgi.py",
            "gunicorn.conf.py",
            "shutdown_manager.py",
        ):
            assert target in pylint_line, f"pylint CI target missing {target}"
        # The original targets must remain covered as well.
        for target in ("app.py", "routes/", "services/", "utils/"):
            assert target in pylint_line


# ---------------------------------------------------------------------------
# LIFECYCLE-11 (FIXED) — native host per-connection ancestry cache
# ---------------------------------------------------------------------------


class TestNativeHostAncestryCache:
    VALID_ID = "abcdefghijklmnopqrstuvwxyz123456"

    def _call(self, nh):
        return nh._require_valid_extension_id({"extensionId": self.VALID_ID, "action": "ping"})

    def test_authorized_verdict_enumerated_once(self):
        from native_host import native_host as nh

        calls = {"n": 0}

        def _counting_ancestors(max_depth=5):
            calls["n"] += 1
            return ["cmd.exe", "chrome.exe"]

        sent: list[dict] = []
        with (
            patch.object(nh, "_get_ancestor_process_names", side_effect=_counting_ancestors),
            patch.object(nh, "_load_allowed_manifest_origins", return_value={self.VALID_ID}),
            patch.object(nh, "send_message", side_effect=sent.append),
            patch("sys.argv", ["native_host.py", f"chrome-extension://{self.VALID_ID}/"]),
        ):
            first = self._call(nh)
            second = self._call(nh)

        assert first == self.VALID_ID
        assert second == self.VALID_ID
        assert calls["n"] == 1, "ancestry must be enumerated only once after success"
        assert sent == []

    def test_failed_verification_is_not_cached(self):
        """Fail-closed: a rejected ancestry is re-evaluated on every message;
        a later authorized chain is accepted without a stale negative."""
        from native_host import native_host as nh

        ancestry = {"values": [["powershell.exe"], ["cmd.exe", "chrome.exe"]], "n": 0}

        def _switching_ancestors(max_depth=5):
            value = ancestry["values"][min(ancestry["n"], len(ancestry["values"]) - 1)]
            ancestry["n"] += 1
            return list(value)

        sent: list[dict] = []
        with (
            patch.object(nh, "_get_ancestor_process_names", side_effect=_switching_ancestors),
            patch.object(nh, "_load_allowed_manifest_origins", return_value={self.VALID_ID}),
            patch.object(nh, "send_message", side_effect=sent.append),
            patch("sys.argv", ["native_host.py", f"chrome-extension://{self.VALID_ID}/"]),
        ):
            rejected = self._call(nh)
            accepted = self._call(nh)

        assert rejected is None
        assert sent == [{"ok": False, "error": "Unauthorized parent process"}]
        assert accepted == self.VALID_ID
        assert ancestry["n"] == 2, "failures must not be memoized"

    def test_reset_helper_clears_cached_verdict(self):
        from native_host import native_host as nh

        calls = {"n": 0}

        def _counting_ancestors(max_depth=5):
            calls["n"] += 1
            return ["cmd.exe", "chrome.exe"]

        with (
            patch.object(nh, "_get_ancestor_process_names", side_effect=_counting_ancestors),
            patch.object(nh, "_load_allowed_manifest_origins", return_value={self.VALID_ID}),
            patch.object(nh, "send_message"),
            patch("sys.argv", ["native_host.py", f"chrome-extension://{self.VALID_ID}/"]),
        ):
            assert self._call(nh) == self.VALID_ID
            nh._reset_caller_ancestry_cache()
            assert self._call(nh) == self.VALID_ID
        assert calls["n"] == 2

    def test_invalid_extension_id_rejected_before_any_ancestry_work(self):
        """Retained order: the cheap format check still runs first."""
        from native_host import native_host as nh

        sent: list[dict] = []
        with (
            patch.object(nh, "_get_ancestor_process_names", side_effect=AssertionError),
            patch.object(nh, "send_message", side_effect=sent.append),
        ):
            result = nh._require_valid_extension_id({"extensionId": "short", "action": "ping"})
        assert result is None
        assert sent == [{"ok": False, "error": "Invalid extension ID"}]


# ---------------------------------------------------------------------------
# LIFECYCLE-12 (FIXED) — conftest manifest fixture atomicity / xdist safety
# ---------------------------------------------------------------------------


class TestManifestFixtureSafety:
    def test_session_manifest_is_parseable(self):
        """The session-created manifest must never be readable in a torn state;
        an atomic os.replace write makes partial JSON impossible."""
        import json

        manifest = ROOT / "native_host" / "com.mistral_nex_stocks.host.json"
        assert manifest.exists(), "session fixture (or installer) must provide the manifest"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        assert data["allowed_origins"], "manifest must carry at least one origin"

    def test_no_temp_leftovers_next_to_manifest(self):
        manifest_tmp = ROOT / "native_host" / "com.mistral_nex_stocks.host.json.tmp"
        assert not manifest_tmp.exists(), "atomic-write temp file must not be left behind"

    def test_fixture_uses_atomic_replace_and_xdist_guard(self):
        """Pin the two root-cause fixes in the fixture itself: atomic replace
        (no torn reads) and no unlink under xdist (no early-removal race)."""
        conftest_src = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
        fixture_src = conftest_src.split("def ensure_manifest_exists")[1].split("@pytest.fixture")[0]
        assert "os.replace" in fixture_src
        assert "PYTEST_XDIST_WORKER" in fixture_src


# ---------------------------------------------------------------------------
# LIFECYCLE-13 (NO ACTION) — launcher fallback no longer generated
# ---------------------------------------------------------------------------


class TestNativeHostLauncherTemplateEvidence:
    def test_tracked_launcher_template_has_no_bare_pythonw_fallback(self):
        """LIFECYCLE-13 NO ACTION evidence: the reported fallback lives only in
        the gitignored, machine-generated ``native_host/native_host.cmd`` (a
        stale artifact of an older installer). The tracked generator —
        ``native_host/host_launcher.cmd.template`` — pins an absolute
        ``__PYTHON_EXE__`` path with no fallback branch, and
        ``install_host_windows.ps1`` hard-fails when no interpreter resolves
        ('Python executable not found'). Reinstalling regenerates the launcher
        from the template."""
        template = (ROOT / "native_host" / "host_launcher.cmd.template").read_text(
            encoding="utf-8"
        )
        assert "pythonw" not in template, "template must not fall back to bare pythonw"
        assert "__PYTHON_EXE__" in template  # pinned also by test_extension_review_fixes.py:67
        assert template.count("%*") == 1  # pinned by test_native_host_security.py:718-726
        installer = (ROOT / "native_host" / "install_host_windows.ps1").read_text(encoding="utf-8")
        assert "Python executable not found" in installer


# ---------------------------------------------------------------------------
# LIFECYCLE-14 (FIXED) — startup excepthook prefers APP_DATA_DIR over tree root
# ---------------------------------------------------------------------------


class TestEarlyExcepthookLogLocation:
    def _snapshot_root_logs(self) -> dict[str, tuple[int, int]]:
        snapshot = {}
        for name in ("backend.log", "error.log"):
            path = ROOT / name
            if path.exists():
                stat = path.stat()
                snapshot[name] = (stat.st_mtime_ns, stat.st_size)
        return snapshot

    def test_no_override_writes_to_app_data_dir_not_repo_root(self, monkeypatch, tmp_path):
        from app_factory import early

        for var in ("MNS_LOG_DIR", "MNS_DATA_DIR", "MNS_APP_DATA_DIR"):
            monkeypatch.delenv(var, raising=False)
        for var in ("PYTEST_CURRENT_TEST", "MNS_TESTING", "TESTING"):
            monkeypatch.delenv(var, raising=False)
        # Keep the platform branches hermetic: no writes to the real home or
        # LOCALAPPDATA during this test.
        xdg_dir = tmp_path / "xdg"
        appdata_dir = tmp_path / "appdata"
        monkeypatch.setenv("XDG_DATA_HOME", str(xdg_dir))

        before = self._snapshot_root_logs()
        mock_hook = MagicMock()
        with (
            patch.object(sys, "__excepthook__", mock_hook),
            patch.object(early, "_resolve_is_windows_runtime", return_value=False),
            patch("config_store.APP_DATA_DIR", appdata_dir),
        ):
            try:
                raise RuntimeError("simulated startup crash")
            except RuntimeError:
                exc_type, exc_value, tb = sys.exc_info()
                early._early_excepthook(exc_type, exc_value, tb)

        mock_hook.assert_called_once()
        assert (appdata_dir / "backend.log").exists()
        assert (appdata_dir / "error.log").exists()
        entry = (appdata_dir / "backend.log").read_text(encoding="utf-8")
        assert "simulated startup crash" in entry
        assert self._snapshot_root_logs() == before, "repo root must not receive crash logs"

    def test_last_resort_falls_back_to_package_root(self, monkeypatch):
        """Edge case: when config_store cannot be imported during a broken
        startup, the legacy package-root fallback keeps the traceback
        reachable (helper only — no files are written here)."""
        from app_factory import early

        monkeypatch.setitem(sys.modules, "config_store", None)  # import -> ImportError
        assert early._resolve_app_data_log_dir() == ROOT

    def test_data_dir_override_still_wins(self, monkeypatch, tmp_path):
        """Retained behavior: an explicit MNS_LOG_DIR override keeps writing
        only to the override directory (pinned additionally by
        tests/test_early_excepthook.py and
        tests/test_code_review_goal_audit_2026_10_v2.py:33-47)."""
        from app_factory import early

        override = tmp_path / "override-logs"
        monkeypatch.setenv("MNS_LOG_DIR", str(override))
        mock_hook = MagicMock()
        with patch.object(sys, "__excepthook__", mock_hook):
            try:
                raise ValueError("override crash")
            except ValueError:
                exc_type, exc_value, tb = sys.exc_info()
                early._early_excepthook(exc_type, exc_value, tb)
        assert (override / "backend.log").exists()
        assert not (tmp_path / "appdata").exists()


# ---------------------------------------------------------------------------
# LIFECYCLE-15 (NO ACTION) — mypy suppression is pre-existing and large
# ---------------------------------------------------------------------------


class TestMypySuppressionEvidence:
    def test_disable_error_code_list_is_pinned_preexisting_config(self):
        """LIFECYCLE-15 NO ACTION evidence: re-enabling ``no-any-return`` and
        ``unused-ignore`` with the pinned mypy produces 80 errors (40 + 39,
        plus 1 pre-existing union-attr), the majority in files outside this
        layer's editable scope (routes/, services/, utils/, config/, ...).
        Removing the suppression is out of scope; no *new* weakening may be
        added. This test pins the list so it cannot silently grow."""
        import tomllib

        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        disabled = data["tool"]["mypy"]["disable_error_code"]
        assert sorted(disabled) == ["no-any-return", "unused-ignore"]


# ---------------------------------------------------------------------------
# Cross-cutting audit finding A-1 (twin of DATA-3): the shutdown token sites
# call secrets.compare_digest on caller-supplied strings — a non-ASCII token
# must be rejected (fail-closed False), never crash the endpoint with 500.
# ---------------------------------------------------------------------------


class TestShutdownTokenNonAsciiRejected:
    """A-1: shutdown_manager consume/validate are the DATA-3 twin sites."""

    @staticmethod
    def _make_manager(tmp_path):
        import logging

        from shutdown_manager import ShutdownTokenManager

        mgr = ShutdownTokenManager(logging.getLogger("test_a1_shutdown"))
        mgr.token_file = tmp_path / ".mns_shutdown_token"
        mgr.used_marker = tmp_path / ".mns_shutdown_token.used"
        mgr._legacy_token_file = tmp_path / ".legacy_token"
        mgr._legacy_used_marker = tmp_path / ".legacy_used"
        mgr.runtime_state_dir = tmp_path
        return mgr

    def test_consume_non_ascii_token_rejected_without_typeerror(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        token = mgr.get_or_create_shutdown_token()
        assert token and token.isascii()

        for bad in ("秘密トークン", "toké-😀", "\u200b", "\ufeff"):
            # No TypeError may escape (compare_digest is ASCII-only for str).
            assert mgr.consume_shutdown_token(bad) is False

        assert mgr.shutdown_token_used is False
        assert not mgr.used_marker.exists()
        # Retained: the genuine ASCII token still consumes exactly once.
        assert mgr.consume_shutdown_token(token) is True
        assert mgr.shutdown_token_used is True

    def test_validate_non_ascii_token_rejected_without_typeerror(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        token = mgr.get_or_create_shutdown_token()
        assert token

        assert mgr.validate_shutdown_token("非ASCII") is False
        assert mgr.validate_shutdown_token("\u200b") is False
        assert mgr.shutdown_token_used is False
        # Retained: the genuine ASCII token still validates before commit.
        assert mgr.validate_shutdown_token(token) is True
