"""Regression guard for the standalone successful-bootstrap CI smoke."""

from pathlib import Path


def test_ci_runs_bootstrap_smoke_without_pytest_conftest():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "python tests/startup_smoke_runner.py" in workflow
    assert 'MNS_SKIP_BOOTSTRAP: "0"' in workflow


def test_start_background_worker_respects_app_bg_facade_loop_patches():
    """R1: start_background_worker must resolve loop callables from app_bg facade."""
    import threading
    from unittest.mock import MagicMock, patch

    import app_bg
    from app_state import app_state
    from bg.sync_worker import start_background_worker
    from services.realtime_engine import realtime_market_engine

    mock_yahoo = MagicMock()
    mock_leader = MagicMock()
    mock_interp = MagicMock()
    mock_load = MagicMock()
    mock_threads = []

    def fake_thread_factory(target=None, args=(), daemon=True, name=None):
        mock_t = MagicMock()
        mock_t.target = target
        mock_t.args = args
        mock_threads.append(mock_t)
        return mock_t

    mock_bg_threads = []
    with (
        patch.object(app_bg, "bg_yahoo_fetch_loop", mock_yahoo),
        patch.object(app_bg, "bg_leader_election_loop", mock_leader),
        patch.object(app_bg, "bg_interpolate_loop", mock_interp),
        patch.object(app_bg, "load_user_stocks", mock_load),
        patch.object(threading, "Thread", side_effect=fake_thread_factory),
        patch.object(realtime_market_engine, "register_symbols"),
        patch.object(realtime_market_engine, "start"),
        patch.object(app_state.execution, "background_threads", mock_bg_threads),
    ):
        start_background_worker()

    mock_load.assert_called_once_with(force=True)
    # Check that the thread target wrappers received the mocked functions as their target arg
    loop_targets = [t.args[0] for t in mock_threads if t.args]
    assert mock_yahoo in loop_targets
    assert mock_leader in loop_targets
    assert mock_interp in loop_targets


def test_bg_loops_dispatch_to_app_bg_overrides():
    """R1: direct loop calls must delegate to app_bg facade overrides."""
    from unittest.mock import MagicMock, patch

    import app_bg
    from bg.leader_election import bg_leader_election_loop
    from bg.sse_interpolator import bg_interpolate_loop
    from bg.sync_worker import _watchdog_restart_dead_realtime_engine, bg_yahoo_fetch_loop

    mock_leader = MagicMock(return_value="leader_delegated")
    mock_yahoo = MagicMock(return_value="yahoo_delegated")
    mock_interp = MagicMock(return_value="interp_delegated")
    mock_watchdog = MagicMock(return_value=["mocked_thread"])

    with (
        patch.object(app_bg, "bg_leader_election_loop", mock_leader),
        patch.object(app_bg, "bg_yahoo_fetch_loop", mock_yahoo),
        patch.object(app_bg, "bg_interpolate_loop", mock_interp),
        patch.object(app_bg, "_watchdog_restart_dead_realtime_engine", mock_watchdog),
    ):
        res_leader = bg_leader_election_loop()
        res_yahoo = bg_yahoo_fetch_loop()
        res_interp = bg_interpolate_loop()
        res_watchdog = _watchdog_restart_dead_realtime_engine()

    assert res_leader == "leader_delegated"
    assert res_yahoo == "yahoo_delegated"
    assert res_interp == "interp_delegated"
    assert res_watchdog == ["mocked_thread"]


def test_app_factory_modules_import_cleanly_without_circular_dependency():
    """Ensure app_factory modules can be imported directly in fresh processes."""
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    modules_to_test = [
        "from app_factory.boot import bootstrap",
        "from app_factory.factory import create_app",
        "from app_factory.warmup import schedule_news_warmup",
        "from app_factory.early import _early_excepthook",
    ]
    for import_stmt in modules_to_test:
        proc = subprocess.run(
            [sys.executable, "-c", import_stmt],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert proc.returncode == 0, (
            f"Import failed: {import_stmt}\nstderr: {proc.stderr}\nstdout: {proc.stdout}"
        )


def test_app_entrypoint_submodule_circular_import_prevention():
    """Ensure running app.py as __main__ does not trigger circular import or duplicate load."""
    import os
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["MNS_SKIP_BOOTSTRAP"] = "1"
    # Execute a snippet that tests running app.py with runpy (simulating python app.py)
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "root = Path(r'" + str(root).replace('\\', '\\\\') + "')\n"
        "if str(root) not in sys.path:\n"
        "    sys.path.insert(0, str(root))\n"
        "from unittest.mock import patch\n"
        "from flask import Flask\n"
        "import runpy\n"
        "with patch.object(Flask, 'run'):\n"
        "    runpy.run_path(str(root / 'app.py'), run_name='__main__')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Execution failed:\nstderr: {proc.stderr}\nstdout: {proc.stdout}"
    )
