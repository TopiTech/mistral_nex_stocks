"""Regression coverage for process, SSE, JP-symbol, and FX-state contracts."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app_state import app_state
from messaging import SseListenerLimiter
from tests import reset_app_state_internals
from utils import storage
from utils.worker_validation import (
    MultiWorkerConfigurationError,
    _counts_from_command_tokens,
    enforce_single_worker,
)

ROOT = Path(__file__).resolve().parent.parent


def _load_gunicorn_config():
    spec = importlib.util.spec_from_file_location("test_gunicorn_config", ROOT / "gunicorn.conf.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_worker_parser_treats_short_p_as_uwsgi_only():
    """Gunicorn's ``-p`` pidfile option must never be parsed as workers."""
    assert _counts_from_command_tokens(["-p", "4", "-w", "1"], server="gunicorn") == [1]
    assert _counts_from_command_tokens(["-p", "4"], server="uwsgi") == [4]

    assert (
        enforce_single_worker(
            environ={"GUNICORN_CMD_ARGS": "-p 4 --workers 1"},
            argv=["gunicorn", "-p", "4", "wsgi:app"],
            uwsgi_module=object(),
        )
        == 1
    )


def test_worker_guard_rejects_uwsgi_ini_processes_and_cheaper_mode():
    """uWSGI native options cannot bypass the one-process deployment rule."""
    with pytest.raises(MultiWorkerConfigurationError, match="workers=2"):
        enforce_single_worker(
            environ={},
            argv=["uwsgi", "--ini", "server.ini"],
            uwsgi_module=SimpleNamespace(opt={b"processes": b"2"}),
        )

    with pytest.raises(MultiWorkerConfigurationError, match="cheaper mode"):
        enforce_single_worker(
            environ={"UWSGI_CMD_ARGS": "--cheaper 1 --cheaper-initial 1"},
            argv=["uwsgi"],
            uwsgi_module=SimpleNamespace(opt={}),
        )


def test_gunicorn_threads_follow_sse_limit_and_reject_unsafe_override(monkeypatch):
    monkeypatch.setenv("MNS_MAX_SSE_LISTENERS", "7")
    config = _load_gunicorn_config()

    assert config.sse_listener_limit == 7
    assert config.required_threads == 13
    assert config.threads == 13

    with pytest.raises(SystemExit):
        config.on_starting(SimpleNamespace(num_workers=1, cfg=SimpleNamespace(threads=12)))


def test_sse_limiter_is_global_and_reservations_are_idempotent():
    with patch("messaging.MAX_SSE_LISTENERS", 2):
        limiter = SseListenerLimiter()
        first = limiter.reserve()
        second = limiter.reserve()
        assert first is not None and second is not None
        assert limiter.reserve() is None
        assert limiter.listener_count() == 2

        first.release()
        first.release()
        assert limiter.listener_count() == 1
        second.release()
        assert limiter.listener_count() == 0


def test_sse_endpoint_reservation_is_global_and_releases_on_close():
    """Both uniterated and active streams must release one global admission slot."""
    from app import app

    # Do not use the context-preserving fixture here: an uniterated SSE
    # response intentionally owns its request context until close, while this
    # test must issue a second independent connection before that close.
    first_client = app.test_client()
    second_client = app.test_client()
    third_client = app.test_client()
    app_state.sse_listener_limiter.reset_for_testing()
    with patch("messaging.MAX_SSE_LISTENERS", 1):
        mode1_response = first_client.get(
            "/api/stocks/stream?mode=1",
            headers={"Origin": "http://localhost:5000"},
            buffered=False,
        )
        assert mode1_response.status_code == 200
        assert app_state.sse_listener_limiter.listener_count() == 1

        # The other mode shares the reservation instead of having an
        # independent MAX_SSE_LISTENERS budget.
        denied = second_client.get(
            "/api/stocks/stream?mode=2",
            headers={"Origin": "http://localhost:5000"},
            buffered=False,
        )
        assert denied.status_code == 429

        # Closing before WSGI has iterated the generator still invokes the
        # response close callback and returns the reservation.
        mode1_response.close()
        assert app_state.sse_listener_limiter.listener_count() == 0

        mode2_response = third_client.get(
            "/api/stocks/stream?mode=2",
            headers={"Origin": "http://localhost:5000"},
            buffered=False,
        )
        assert mode2_response.status_code == 200
        assert next(iter(mode2_response.response))
        assert app_state.sse_listener_limiter.listener_count() == 1
        mode2_response.close()
        assert app_state.sse_listener_limiter.listener_count() == 0


def test_screener_normalizes_bare_jp_query_before_enrichment(client):
    captured: dict[str, object] = {}

    def _enrich(items, q_symbol, **_kwargs):
        captured["items"] = items
        captured["q_symbol"] = q_symbol
        return {
            "7203.T": {
                "symbol": "7203.T",
                "name": "Toyota",
                "market": "jp",
                "sector": "Other",
                "price": 100.0,
                "change_percent": 0.0,
                "market_cap": 1.0,
                "volume": 1,
            }
        }

    def _immediate_cache(_key, factory, **_kwargs):
        return factory()

    with (
        patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, "")),
        patch("routes.api_stocks._resolve_stocks_for_response", return_value={"us": [], "jp": []}),
        patch("routes.api_stocks.build_popular_symbol_items", return_value=[]),
        patch("routes.api_stocks.get_cached", side_effect=_immediate_cache),
        patch("routes.api_stocks.build_screener_enrichment", side_effect=_enrich),
    ):
        response = client.get("/api/screener?market=all&q=7203")

    assert response.status_code == 200
    assert captured["q_symbol"] == "7203.T"
    assert ("7203.T", "7203.T", "jp") in captured["items"]
    assert [row["symbol"] for row in response.get_json()["stocks"]] == ["7203.T"]


def test_ai_copy_normalizes_bare_jp_symbol_and_avoids_legacy_duplicate(client):
    with (
        patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, "")),
        patch("routes.api_stocks.save_user_stocks"),
        patch("routes.api_stocks._sync_realtime_symbol"),
        patch("routes.api_stocks._announce_watchlist_state"),
        patch("routes.api_stocks.schedule_sync_all_stocks_now"),
    ):
        response = client.post(
            "/api/ai-portfolio/copy-to-my",
            json={
                "items": [
                    {"symbol": "7203", "market": "jp", "weight_pct": 10, "target_price": 2000}
                ]
            },
        )

    assert response.status_code == 200
    assert response.get_json()["added_count"] == 1
    assert "7203.T" in app_state.market.user_jp
    assert "7203" not in app_state.market.user_jp

    with patch("routes.api_stocks.save_user_stocks"):
        # Simulate an un-restarted legacy process: canonical add must not create
        # a second entry beside the old bare numeric spelling.
        app_state.market.user_jp = {"7203": "Toyota"}
        response = client.post(
            "/api/stocks/add",
            json={"symbol": "7203", "market": "jp", "name": "Toyota"},
            headers={"Origin": "http://localhost:5000"},
        )
    assert response.status_code == 400
    assert app_state.market.user_jp == {"7203": "Toyota"}


@pytest.mark.parametrize("request_symbol", ["7203", "7203.T"])
def test_jp_delete_removes_both_legacy_aliases_from_persisted_state(
    client, tmp_path, monkeypatch, request_symbol
):
    """Deleting either spelling is the explicit reconciliation for a conflict."""
    target = tmp_path / "user_stocks.json"
    monkeypatch.setattr(storage, "USER_STOCKS_FILE", str(target))
    monkeypatch.setattr(storage.config_store, "get_or_create_master_key", lambda: "test-key")
    monkeypatch.setattr(
        storage,
        "protect_data",
        lambda value, **_kwargs: {"scheme": "test", "value": value},
    )
    monkeypatch.setattr(
        storage,
        "_write_user_stocks_with_lock",
        lambda data, _tmp, output, _lock: output.write_text(data, encoding="utf-8"),
    )

    app_state.market.user_jp = {
        "7203": {"name": "legacy", "shares": 1, "avg_price": 1000},
        "7203.T": {"name": "canonical", "shares": 2, "avg_price": 2000},
    }
    with (
        patch("routes.api_stocks._sync_realtime_symbol"),
        patch("routes.api_stocks._announce_watchlist_state"),
        patch("routes.api_stocks.schedule_sync_all_stocks_now"),
    ):
        response = client.post(
            "/api/stocks/delete",
            json={"symbol": request_symbol, "market": "jp"},
            headers={"Origin": "http://localhost:5000"},
        )

    assert response.status_code == 200
    assert app_state.market.user_jp == {}
    persisted = json.loads(json.loads(target.read_text(encoding="utf-8"))["value"])
    assert persisted["jp"] == {}


def test_jp_alias_delete_restores_every_key_when_persistence_fails(client):
    app_state.market.user_jp = {"7203": "legacy", "7203.T": "canonical"}

    with patch(
        "routes.api_stocks.save_user_stocks",
        side_effect=storage.UserStocksPersistError("disk full"),
    ):
        response = client.post(
            "/api/stocks/delete",
            json={"symbol": "7203", "market": "jp"},
            headers={"Origin": "http://localhost:5000"},
        )

    assert response.status_code == 503
    assert app_state.market.user_jp == {"7203": "legacy", "7203.T": "canonical"}


def test_test_reset_clears_usdjpy_rate_and_freshness_timestamp():
    app_state.market.last_usdjpy_rate = 173.42
    app_state.market.last_usdjpy_rate_ts = 123_456_789.0

    reset_app_state_internals()

    assert app_state.market.last_usdjpy_rate == pytest.approx(150.00)
    assert app_state.market.last_usdjpy_rate_ts == 0.0
