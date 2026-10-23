"""U8 regression: A-08 safe default + E-04 shared initial snapshot (2026-10-23).

- The ``resolve_stocks_for_response`` wrapper must default to
  ``include_portfolio=False`` so a bare call can never leak holdings.
- The SSE initial snapshot is built once per data generation and shared by
  every client that connects within it; a generation bump forces a rebuild.
"""

from __future__ import annotations

import copy
import inspect
import json
from unittest.mock import patch

from app import create_app
from app_state import app_state
from bg.common import _invalidate_sse_payload_cache
from routes.stocks import stream as stream_mod
from routes.stocks.common import resolve_stocks_for_response


def _fake_stocks():
    return {
        "us": [{"symbol": "AAA", "name": "Alpha", "price": 10.0}],
        "jp": [{"symbol": "7203.T", "name": "Toyota", "price": 2500.0}],
        "idx": [],
    }


def _first_data_payload(response) -> dict:
    first_chunk = next(response.response).decode("utf-8")
    assert "initial_snapshot" in first_chunk
    data_line = next(
        line for line in first_chunk.split("\n") if line.startswith("data: ")
    )
    return json.loads(data_line[6:])


def test_wrapper_defaults_to_excluding_portfolio():
    sig = inspect.signature(resolve_stocks_for_response)
    assert sig.parameters["include_portfolio"].default is False

    with app_state.market.user_stocks_lock:
        app_state.market.user_us = {
            "AAA": {"name": "Alpha", "shares": 10.0, "avg_price": 9.0}
        }
        app_state.market.user_jp = {}
        app_state.market.user_idx = {}
    with app_state.cache.sse_data_lock:
        app_state.market.current_stocks_cache = copy.deepcopy(
            {
                "us": [
                    {
                        "symbol": "AAA",
                        "name": "Alpha",
                        "price": 10.0,
                        "shares": 10.0,
                        "avg_price": 9.0,
                    }
                ],
                "jp": [],
                "idx": [],
            }
        )
    try:
        resolved = resolve_stocks_for_response()
        assert "shares" not in resolved["us"][0]
        assert "avg_price" not in resolved["us"][0]
    finally:
        with app_state.market.user_stocks_lock:
            app_state.market.user_us = {}
        with app_state.cache.sse_data_lock:
            app_state.market.current_stocks_cache = {"us": [], "jp": [], "idx": []}


def _read_first_payload(client, mode) -> dict:
    # Sequential open -> read -> close: holding two suspended SSE generators
    # on one test client corrupts the Flask request context stack, so each
    # connection is fully closed before the next opens. The module-level
    # snapshot cache persists across connections, which is what we assert.
    response = client.get(
        f"/api/stocks/stream?mode={mode}",
        headers={"X-MNS-Admin-Token": "test-token"},
    )
    assert response.status_code == 200
    try:
        return _first_data_payload(response)
    finally:
        response.close()


def test_initial_snapshot_shared_within_generation():
    stream_mod._initial_snapshot_cache.clear()
    build_calls: list[int] = []

    def _counting_resolve(**kwargs):
        build_calls.append(1)
        return copy.deepcopy(_fake_stocks())

    app = create_app()
    app.config["TESTING"] = True
    try:
        with (
            patch.object(
                stream_mod, "resolve_stocks_for_response", side_effect=_counting_resolve
            ),
            patch.object(
                stream_mod,
                "resolve_indices_for_response",
                return_value={},
            ),
            app.test_client() as client,
        ):
            payload1 = _read_first_payload(client, 1)
            payload2 = _read_first_payload(client, 1)
        assert len(build_calls) == 1
        assert payload1 == payload2
    finally:
        stream_mod._initial_snapshot_cache.clear()


def test_generation_bump_forces_rebuild():
    stream_mod._initial_snapshot_cache.clear()
    builds: list[dict] = []

    def _versioned_resolve(**kwargs):
        payload = copy.deepcopy(_fake_stocks())
        payload["us"][0]["price"] = 10.0 + len(builds)
        builds.append(payload)
        return payload

    app = create_app()
    app.config["TESTING"] = True
    try:
        with (
            patch.object(
                stream_mod, "resolve_stocks_for_response", side_effect=_versioned_resolve
            ),
            patch.object(stream_mod, "resolve_indices_for_response", return_value={}),
            app.test_client() as client,
        ):
            payload1 = _read_first_payload(client, 1)
            assert payload1["stocks"]["us"][0]["price"] == 10.0
            _invalidate_sse_payload_cache()
            payload2 = _read_first_payload(client, 1)
            assert payload2["stocks"]["us"][0]["price"] == 11.0
        assert len(builds) == 2
    finally:
        stream_mod._initial_snapshot_cache.clear()


def test_modes_have_independent_shared_entries():
    stream_mod._initial_snapshot_cache.clear()
    build_modes: list[int] = []

    def _mode_resolve(**kwargs):
        build_modes.append(kwargs.get("real_data_only", False))
        return copy.deepcopy(_fake_stocks())

    app = create_app()
    app.config["TESTING"] = True
    try:
        with (
            patch.object(
                stream_mod, "resolve_stocks_for_response", side_effect=_mode_resolve
            ),
            patch.object(stream_mod, "resolve_indices_for_response", return_value={}),
            app.test_client() as client,
        ):
            _read_first_payload(client, 1)
            _read_first_payload(client, 2)
            _read_first_payload(client, 1)
            _read_first_payload(client, 2)
        # One build per mode; repeats within a mode are shared.
        assert sorted(map(int, build_modes)) == [0, 1]
    finally:
        stream_mod._initial_snapshot_cache.clear()
