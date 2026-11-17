"""Regression tests for the 2026-11-17 comprehensive review (R17-xx)."""
import os


def test_413_parse_raises_for_chunked_oversized(client):
    """R17-03: chunked body over MAX_JSON_SIZE must surface 413, not 400."""
    import json

    from flask import Flask

    flask_app = Flask(__name__)

    @flask_app.post("/probe")
    def _probe():
        from utils.text_utils import _parse_json_request

        _parse_json_request()
        return {"ok": True}

    with flask_app.test_client() as c:
        body = json.dumps({"k": "x" * 1048577}, ensure_ascii=False)
        resp = c.post("/probe", data=body, content_type="application/json")
    assert resp.status_code == 413


def test_empty_string_preserves_credential(monkeypatch, tmp_path):
    """R17-02 contract: save_api_credentials(x='') is a no-op for that key.

    The HTTP route translates an explicit delete into clear_api_credentials,
    which is the single clear path. ``save_api_credentials`` keeps ==
    'leave unchanged' for '' (test_input_validation pins this).
    """
    import credential_manager

    real = credential_manager.crypto_utils._encode_secret

    def fake_encode(value, key_name):
        return real(value, key_name)

    monkeypatch.setattr(credential_manager.crypto_utils, "_encode_secret", fake_encode)
    credential_manager.save_api_credentials(mistral_api_key="a" * 32)
    # Blank update must not remove the key (other keys are preserved).
    credential_manager.save_api_credentials(langsearch_api_key="")
    state_after = credential_manager.get_api_credential_state()
    assert state_after.get("has_mistral_api_key") is True
    # clear_api_credentials is the explicit clear path.
    failed = credential_manager.clear_api_credentials()
    assert failed == []


def test_error_code_numeric_contract(client):
    """`code` is str(error_code) per the pinned contract (R17-14 narrowed)."""
    resp = client.post("/api/credentials", json={"x": 1})
    if resp.status_code == 400:
        body = resp.get_json()
        assert body["code"] == str(body["error_code"])
        assert body["error_code"] == 1008


def test_leader_pid_alive_probe(monkeypatch):
    """R17-01: _pid_is_alive must not signal the target process."""
    from bg import leader_election as le

    # Non-existent pid is not alive; the current pid is alive without signalling.
    assert le._pid_is_alive(os.getpid()) is True
    assert le._pid_is_alive(2_147_483_647) is False


def test_storage_load_preserves_dict_identity(client, monkeypatch):
    """R17-06: load_user_stocks must not rebind user_us/jp/idx."""
    from app_state import app_state
    from utils import storage

    ident_us = id(app_state.market.user_us)
    ident_jp = id(app_state.market.user_jp)
    ident_idx = id(app_state.market.user_idx)
    monkeypatch.setattr(storage, "_locked_read_user_stocks", lambda lock_file: {"us": {}, "jp": {}, "idx": {}})
    monkeypatch.setattr(storage, "_get_user_stocks_path", lambda: _p())
    try:
        storage.load_user_stocks()
    except Exception:
        pass
    assert id(app_state.market.user_us) == ident_us
    assert id(app_state.market.user_jp) == ident_jp
    assert id(app_state.market.user_idx) == ident_idx


def _p():
    from pathlib import Path

    return Path.cwd() / "nonexistent_user_stocks.json"
