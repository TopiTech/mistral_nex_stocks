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

