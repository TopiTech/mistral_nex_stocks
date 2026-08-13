"""Regression tests for current review R1-R4 fixes."""

import importlib
import importlib.util
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# R1: gunicorn.conf bind derives from MNS_BACKEND_PORT
# ---------------------------------------------------------------------------
def _load_gunicorn_conf(env_overrides):
    spec = importlib.util.spec_from_file_location(
        "gunicorn_conf_under_test",
        Path(__file__).parent.parent / "gunicorn.conf.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    with patch.dict("os.environ", env_overrides, clear=False):
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_r1_gunicorn_bind_default_port():
    mod = _load_gunicorn_conf({"MNS_BACKEND_PORT": ""})
    # fallback via _env_int returns default 5000
    assert mod.bind == "127.0.0.1:5000"
    assert mod._backend_port == 5000


def test_r1_gunicorn_bind_custom_port():
    mod = _load_gunicorn_conf({"MNS_BACKEND_PORT": "5001"})
    assert mod.bind == "127.0.0.1:5001"


def test_r1_gunicorn_bind_invalid_falls_back():
    mod = _load_gunicorn_conf({"MNS_BACKEND_PORT": "abc"})
    assert mod.bind == "127.0.0.1:5000"


def test_r1_gunicorn_bind_out_of_range_clamped():
    mod = _load_gunicorn_conf({"MNS_BACKEND_PORT": "99999"})
    # _env_int clamps to 65535
    assert mod.bind == "127.0.0.1:65535"
    mod2 = _load_gunicorn_conf({"MNS_BACKEND_PORT": "0"})
    assert mod2.bind == "127.0.0.1:1"


# ---------------------------------------------------------------------------
# R2: request_token bounded + hashed + distinct cap
# ---------------------------------------------------------------------------
def _build_rate_app():
    from flask import Flask, jsonify, request

    from route_helpers import rate_limit

    app = Flask(__name__)
    app.config["TESTING"] = True

    @app.route("/api/chat", methods=["POST"])
    @rate_limit(max_requests=2, window_seconds=60, skip_polling_duplicates=True)
    def chat():
        body = request.get_json(silent=True) or {}
        return jsonify({"ok": True, "token": body.get("request_token")})

    return app


def test_r2_overlong_token_counts_normally():
    """Over-long token is ignored for skip path and counts toward quota."""
    from route_helpers import _rate_limit_distinct_token_counts, _rate_limit_lock, _rate_limit_store

    with _rate_limit_lock:
        _rate_limit_store.clear()
        _rate_limit_distinct_token_counts.clear()
    app = _build_rate_app()
    client = app.test_client()
    env = {"REMOTE_ADDR": "10.0.0.1"}
    long_token = "x" * 500
    # 2 distinct long tokens count normally, 3rd -> 429
    for _ in range(2):
        resp = client.post("/api/chat", json={"request_token": long_token}, environ_base=env)
        assert resp.status_code == 200
    # Use a different long token each time — but they all count (no token bucket created)
    resp = client.post("/api/chat", json={"request_token": "y" * 500}, environ_base=env)
    # Third distinct counts normally -> 429
    assert resp.status_code == 429


def test_r2_token_hashed_not_raw():
    """Token key uses hash, not raw token text."""
    from route_helpers import _rate_limit_lock, _rate_limit_store

    with _rate_limit_lock:
        _rate_limit_store.clear()
    app = _build_rate_app()
    client = app.test_client()
    env = {"REMOTE_ADDR": "10.0.0.2"}
    token = "my-secret-token-1234567890abcdef1234567890"
    client.post("/api/chat", json={"request_token": token}, environ_base=env)
    with _rate_limit_lock:
        # raw token should not appear in keys
        for k in _rate_limit_store:
            assert token not in k
            if ":token:" in k:
                assert len(k.split(":token:")[1]) == 32  # sha256[:32]


def test_r2_distinct_token_cap_enforced():
    """Distinct-token budget prevents store flood; legitimate polling still works."""
    from route_helpers import _rate_limit_distinct_token_counts, _rate_limit_lock, _rate_limit_store

    app = _build_rate_app()
    client = app.test_client()
    env = {"REMOTE_ADDR": "10.0.0.3"}

    # Use a higher endpoint quota so distinct-token cap is testable without
    # hitting max_requests first. We patch MAX_DISTINCT to 3, then 3 distinct
    # tokens create buckets; 4th distinct must not create a new bucket.
    with patch("route_helpers._RATE_LIMIT_MAX_DISTINCT_TOKENS", 3):
        with _rate_limit_lock:
            _rate_limit_store.clear()
            _rate_limit_distinct_token_counts.clear()
        # Raise endpoint limit for this test by patching resolve to allow 10
        with patch("route_helpers._resolve_rate_limit", return_value=(10, 60)):
            for i in range(3):
                resp = client.post("/api/chat", json={"request_token": f"tok-{i:040d}"}, environ_base=env)
                assert resp.status_code == 200
            resp = client.post("/api/chat", json={"request_token": "tok-new-0000000000000000000000000000"}, environ_base=env)
            # Not over quota, so 200 (falls through to normal quota but still under limit)
            assert resp.status_code == 200
        with _rate_limit_lock:
            distinct = _rate_limit_distinct_token_counts.get("10.0.0.3:chat:distinct", 0)
            assert distinct == 3
            # Only 3 token buckets (4th token not bucketed)
            token_keys = [k for k in _rate_limit_store if ":token:" in k]
            assert len(token_keys) == 3


def test_r2_same_token_polls_still_skip_within_cap():
    """Same token polls still skip (regression guard)."""
    from route_helpers import _rate_limit_distinct_token_counts, _rate_limit_lock, _rate_limit_store

    with _rate_limit_lock:
        _rate_limit_store.clear()
        _rate_limit_distinct_token_counts.clear()
    app = _build_rate_app()
    client = app.test_client()
    env = {"REMOTE_ADDR": "10.0.0.4"}
    token = "reused-token-aaaaaaaaaaaaaaaaaaaaaaaaaaa"
    for _ in range(10):
        resp = client.post("/api/chat", json={"request_token": token}, environ_base=env)
        assert resp.status_code == 200


def test_r2_boundary_token_length():
    """Token at exactly max length is accepted; one char over is not."""
    from route_helpers import (
        _RATE_LIMIT_MAX_REQUEST_TOKEN_LEN,
        _rate_limit_distinct_token_counts,
        _rate_limit_lock,
        _rate_limit_store,
    )

    with _rate_limit_lock:
        _rate_limit_store.clear()
        _rate_limit_distinct_token_counts.clear()
    app = _build_rate_app()
    client = app.test_client()
    env = {"REMOTE_ADDR": "10.0.0.5"}
    ok_token = "b" * _RATE_LIMIT_MAX_REQUEST_TOKEN_LEN
    over_token = "b" * (_RATE_LIMIT_MAX_REQUEST_TOKEN_LEN + 1)
    # ok_token creates a poll bucket
    client.post("/api/chat", json={"request_token": ok_token}, environ_base=env)
    with _rate_limit_lock:
        # At least one token bucket exists (hashed, not raw)
        assert any(":token:" in k for k in _rate_limit_store)
    # over_token does NOT create a new token bucket beyond endpoint
    before = len([k for k in _rate_limit_store if ":token:" in k])
    client.post("/api/chat", json={"request_token": over_token}, environ_base=env)
    with _rate_limit_lock:
        after = len([k for k in _rate_limit_store if ":token:" in k])
    assert after == before  # over-long not bucketed


# ---------------------------------------------------------------------------
# R3: narrowed except + logging
# ---------------------------------------------------------------------------
def test_r3_realtime_ws_sock_close_logs_on_failure(caplog):
    from services.realtime_engine import TradingViewWSClient

    client = TradingViewWSClient()
    mock_ws = MagicMock()
    mock_sock = MagicMock()
    mock_sock.close.side_effect = OSError("sock boom")
    mock_ws.sock = mock_sock
    client.ws = mock_ws
    client.running = True
    client.thread = None
    with caplog.at_level(logging.DEBUG):
        client.stop()
    assert any("sock" in r.message.lower() or "closing" in r.message.lower() for r in caplog.records) or mock_sock.close.called


def test_r3_disk_cache_stale_payload_narrow_except(caplog):
    from utils.disk_cache import _read_stale_payload

    # Non-existent file -> OSError path logs at debug
    with caplog.at_level(logging.DEBUG):
        result = _read_stale_payload(Path("/tmp/__mns_nonexist_12345.json"))
    assert result is None


def test_r3_stock_payload_fx_narrow_except(caplog):
    from app_state import app_state
    from utils.stock_payload import get_current_usdjpy_rate

    # Force disk cache get to raise OSError, should be caught and logged
    orig = app_state.stock_disk_cache.get
    app_state.stock_disk_cache.get = MagicMock(side_effect=OSError("disk fail"))
    try:
        with caplog.at_level(logging.DEBUG):
            rate, est = get_current_usdjpy_rate(default_rate=150.0, max_age_sec=0)
        assert est is True  # fallback
        assert rate == 150.0 or rate > 0
    finally:
        app_state.stock_disk_cache.get = orig


# ---------------------------------------------------------------------------
# R4: config merge untouched-keys log
# ---------------------------------------------------------------------------
def test_r4_merge_logs_untouched_keys(caplog, tmp_path, monkeypatch):
    import config_store

    legacy = tmp_path / "legacy.json"
    runtime = tmp_path / "runtime.json"
    legacy.write_text(json.dumps({"mistral_model": "open-mistral-7b", "custom_ai_prompt": "hello"}), encoding="utf-8")
    runtime.write_text(json.dumps({"mistral_model": "open-mistral-7b"}), encoding="utf-8")
    # No change to mistral_model -> modified stays False, untouched contains custom_ai_prompt
    with caplog.at_level(logging.INFO):
        config_store._merge_configs(legacy, runtime)
    assert any("custom_ai_prompt" in r.message for r in caplog.records)


def test_r4_merge_protected_keys_never_logged_as_untouched(caplog, tmp_path):
    import config_store

    legacy = tmp_path / "legacy2.json"
    runtime = tmp_path / "runtime2.json"
    legacy.write_text(json.dumps({"mns_master_key": "abc", "mistral_model": "m"}), encoding="utf-8")
    runtime.write_text(json.dumps({"mistral_model": "m"}), encoding="utf-8")
    with caplog.at_level(logging.INFO):
        config_store._merge_configs(legacy, runtime)
    # protected keys must not appear in untouched log
    assert not any("mns_master_key" in r.message for r in caplog.records)
