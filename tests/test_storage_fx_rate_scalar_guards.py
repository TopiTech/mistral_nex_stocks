"""The persisted USD/JPY rate must be a finite positive number.

``float(True)`` is ``1.0``, so a corrupted store containing
``"last_usdjpy_rate": true`` used to be accepted and valued every USD holding
at 1 JPY/USD. The timestamp had the same hole (``true`` became 1970-01-01).
Both load and save now share one coercion rule that rejects booleans and
non-finite values.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from app_state import app_state
from constants import DEFAULT_USDJPY_RATE
from tests import reset_app_state_internals
from utils import storage


def test_load_rejects_boolean_fx_metadata(monkeypatch) -> None:
    reset_app_state_internals()
    try:
        raw = {
            "us": {},
            "jp": {},
            "idx": {},
            "last_usdjpy_rate": True,
            "last_usdjpy_rate_ts": True,
        }
        with (
            patch("utils.storage._locked_read_user_stocks", return_value=raw),
            patch("utils.storage.os.path.exists", return_value=True),
            patch(
                "services.realtime_engine.realtime_market_engine.register_symbols",
                return_value=None,
            ),
        ):
            storage.load_user_stocks(force=True)

        assert app_state.market.last_usdjpy_rate == DEFAULT_USDJPY_RATE
        assert app_state.market.last_usdjpy_rate_ts == 0.0
    finally:
        reset_app_state_internals()


def test_load_rejects_non_finite_fx_metadata(monkeypatch) -> None:
    reset_app_state_internals()
    try:
        raw = {
            "us": {},
            "jp": {},
            "idx": {},
            "last_usdjpy_rate": float("inf"),
            "last_usdjpy_rate_ts": float("nan"),
        }
        with (
            patch("utils.storage._locked_read_user_stocks", return_value=raw),
            patch("utils.storage.os.path.exists", return_value=True),
            patch(
                "services.realtime_engine.realtime_market_engine.register_symbols",
                return_value=None,
            ),
        ):
            storage.load_user_stocks(force=True)

        assert app_state.market.last_usdjpy_rate == DEFAULT_USDJPY_RATE
        assert app_state.market.last_usdjpy_rate_ts == 0.0
    finally:
        reset_app_state_internals()


def test_save_coerces_boolean_fx_metadata_before_writing() -> None:
    reset_app_state_internals()
    try:
        app_state.market.last_usdjpy_rate = True
        app_state.market.last_usdjpy_rate_ts = True
        captured: dict[str, object] = {}

        def _capture(encoded: str, **kwargs: object) -> dict[str, str]:
            captured.update(json.loads(encoded))
            return {"scheme": "fernet", "value": "ciphertext"}

        def _write(encoded_data: str, tmp_file: Path, target_file: Path, lock_file: Path) -> None:
            target_file.write_text("{}", encoding="utf-8")

        with (
            patch("utils.storage.protect_data", side_effect=_capture),
            patch("utils.storage._write_user_stocks_with_lock", side_effect=_write),
            patch("utils.storage.config_store.get_or_create_master_key", return_value="k"),
            patch(
                "services.realtime_engine.realtime_market_engine.register_symbols",
                return_value=None,
            ),
        ):
            storage.save_user_stocks()

        assert captured["last_usdjpy_rate"] == DEFAULT_USDJPY_RATE
        assert captured["last_usdjpy_rate_ts"] == 0.0
    finally:
        reset_app_state_internals()
