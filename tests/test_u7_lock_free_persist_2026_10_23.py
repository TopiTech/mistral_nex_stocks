"""U7 regression: C-03 lock-free persistence (2026-10-23).

``save_user_stocks()`` performs JSON serialization, encryption and fsync.
The ``user_stocks_lock`` must never be held across it, or slow disks block
SSE broadcasts and watchlist reads. Mutation happens under the lock, the
persist call happens outside, and failures roll back under the lock.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

from app_state import app_state
from bg import sync_worker
from tests import reset_app_state_internals

REPO_ROOT = Path(__file__).resolve().parent.parent
_SWEEP_FILES = (
    # E-01: save calls moved to bg/sync/process.py + bg/sync/control.py;
    # the bg/sync_worker.py facade holds no save logic.
    "bg/sync/process.py",
    "bg/sync/control.py",
    "routes/stocks/views.py",
    "routes/stocks/portfolio.py",
    "routes/stocks/ai_portfolio.py",
)
_SAVE_CALLS = {"save_user_stocks", "save_fn"}


def _saves_under_user_stocks_lock(path: Path) -> list[int]:
    """Return linenos of save calls nested inside a user_stocks_lock block."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[int] = []

    def _holds_user_stocks_lock(node: ast.With) -> bool:
        return any(
            "user_stocks_lock" in ast.dump(item.context_expr) for item in node.items
        )

    def _visit(node: ast.AST, in_guarded_with: bool = False) -> None:
        if isinstance(node, ast.With) and _holds_user_stocks_lock(node):
            for stmt in node.body:
                _visit(stmt, True)
            for handler in getattr(node, "handlers", []):
                _visit(handler, True)
            return
        if isinstance(node, ast.Call) and in_guarded_with:
            func = node.func
            if (
                isinstance(func, ast.Name)
                and func.id in _SAVE_CALLS
                or isinstance(func, ast.Attribute)
                and func.attr in _SAVE_CALLS
            ):
                offenders.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            _visit(child, in_guarded_with)

    _visit(tree)
    return offenders


def test_no_save_call_holds_user_stocks_lock():
    offenders = []
    for rel in _SWEEP_FILES:
        bad = _saves_under_user_stocks_lock(REPO_ROOT / rel)
        offenders.extend(f"{rel}:{lineno}" for lineno in bad)
    assert offenders == [], f"save under user_stocks_lock: {offenders}"


def _add_user_symbol(symbol: str, name: str = "TestCo") -> None:
    app_state.market.user_us[symbol] = {"name": name, "shares": 0, "avg_price": 0}


def _drive_removal_to_threshold(symbol: str) -> None:
    from bg.sync_worker import _auto_remove_invalid_symbols

    threshold = app_state.market.INVALID_SYMBOL_REMOVAL_THRESHOLD
    marker = (sync_worker._BATCH_INVALID_MARKER, symbol)
    for _ in range(threshold):
        _auto_remove_invalid_symbols([(symbol, "GoneCo", "us")], [marker])


def test_auto_remove_persists_without_holding_lock():
    reset_app_state_internals()
    try:
        _add_user_symbol("DELIST")
        held_during_save: list[bool] = []
        lock = app_state.market.user_stocks_lock

        def _recording_save() -> None:
            held_during_save.append(lock._is_owned())

        with patch("app_bg.save_user_stocks", side_effect=_recording_save):
            _drive_removal_to_threshold("DELIST")

        assert held_during_save, "save was never called"
        assert held_during_save == [False] * len(held_during_save)
        assert "DELIST" not in app_state.market.user_us
    finally:
        reset_app_state_internals()


def test_auto_remove_rollback_when_save_fails():
    from bg.sync_worker import _auto_remove_invalid_symbols

    reset_app_state_internals()
    try:
        _add_user_symbol("DELIST")
        threshold = app_state.market.INVALID_SYMBOL_REMOVAL_THRESHOLD
        app_state.market.invalid_symbol_streak["DELIST"] = threshold - 1

        with patch("app_bg.save_user_stocks", side_effect=OSError("disk unavailable")):
            _auto_remove_invalid_symbols(
                [("DELIST", "GoneCo", "us")],
                [(sync_worker._BATCH_INVALID_MARKER, "DELIST")],
            )

        assert "DELIST" in app_state.market.user_us
        assert app_state.market.invalid_symbol_streak["DELIST"] == threshold
    finally:
        reset_app_state_internals()


def _indices_payload() -> list[dict]:
    return [
        {"symbol": sym, "price": price, "market_state": "REGULAR"}
        for sym, price in (
            ("^N225", 40000.0),
            ("^DJI", 45000.0),
            ("USDJPY=X", 155.0),
            ("EURJPY=X", 170.0),
            ("^VIX", 15.0),
            ("^IXIC", 20000.0),
            ("^GSPC", 6000.0),
        )
    ]


def test_usdjpy_persist_runs_without_holding_lock():
    reset_app_state_internals()
    try:
        app_state.market.last_usdjpy_rate = None
        app_state.market.last_usdjpy_persisted_ts = 0.0
        held_during_save: list[bool] = []
        lock = app_state.market.user_stocks_lock

        def _recording_save() -> None:
            held_during_save.append(lock._is_owned())

        with patch("app_bg.save_user_stocks", side_effect=_recording_save):
            sync_worker._update_indices_data(_indices_payload(), [], [])

        assert held_during_save == [False]
        assert app_state.market.last_usdjpy_rate == 155.0
        assert app_state.market.last_usdjpy_persisted_ts > 0
    finally:
        reset_app_state_internals()


def test_usdjpy_persist_failure_keeps_rate_but_not_persisted_ts():
    reset_app_state_internals()
    try:
        app_state.market.last_usdjpy_rate = None
        app_state.market.last_usdjpy_persisted_ts = 0.0
        with patch("app_bg.save_user_stocks", side_effect=OSError("disk unavailable")):
            sync_worker._update_indices_data(_indices_payload(), [], [])

        assert app_state.market.last_usdjpy_rate == 155.0
        assert app_state.market.last_usdjpy_persisted_ts == 0.0
    finally:
        reset_app_state_internals()
