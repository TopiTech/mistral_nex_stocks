"""Regression tests for Unit 1 core foundations (config, crypto, security, logging, networking, utils)."""

from __future__ import annotations

import ipaddress
import json
import logging
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest
from flask import Flask


def test_master_key_refuses_ephemeral_fallback_even_when_keyring_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D1B-01: Headless keyring failure with MNS_EPHEMERAL_FALLBACK=1 must fail closed."""
    import config_store
    import crypto_utils
    from config import master_key as mk_mod

    cfg_file = tmp_path / "config.json"
    legacy_file = tmp_path / "legacy_config.json"
    cfg_file.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(config_store, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config_store, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(config_store, "LEGACY_CONFIG_FILE", legacy_file)
    monkeypatch.setattr(config_store, "_CONFIG_CACHE", {"data": None, "key": None})
    monkeypatch.setattr(config_store, "_CONFIG_CORRUPTED", False)
    monkeypatch.setattr(config_store, "_LEGACY_MERGE_DONE", True)

    monkeypatch.delenv("MNS_MASTER_KEY", raising=False)
    monkeypatch.delenv("MNS_PROD", raising=False)
    monkeypatch.delenv("MNS_ALLOW_EPHEMERAL_MASTER_KEY", raising=False)
    monkeypatch.setenv("MNS_EPHEMERAL_FALLBACK", "1")
    monkeypatch.setattr(crypto_utils, "KEYRING_AVAILABLE", True)
    monkeypatch.setattr(crypto_utils, "_is_windows", lambda: False)

    class _FailingKeyring:
        @staticmethod
        def get_password(_svc: str, _name: str):
            return None

        @staticmethod
        def set_password(_svc: str, _name: str, _val: str) -> None:
            raise RuntimeError("No D-Bus SecretService")

    monkeypatch.setattr(crypto_utils, "keyring", _FailingKeyring)
    monkeypatch.setattr(crypto_utils, "_EPHEMERAL_CREDENTIALS", {})

    with pytest.raises(RuntimeError, match="ephemeral"):
        mk_mod.get_or_create_master_key()

    assert "mns_master_key" not in crypto_utils._EPHEMERAL_CREDENTIALS
    assert json.loads(cfg_file.read_text(encoding="utf-8")) == {}


def test_master_key_post_save_verification_rejects_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D1B-10: Post-save verification must fail and roll back if decoded key mismatches."""
    import config_store
    import crypto_utils
    from config import master_key as mk_mod

    cfg_file = tmp_path / "config.json"
    cfg_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config_store, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config_store, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(config_store, "LEGACY_CONFIG_FILE", tmp_path / "legacy.json")
    monkeypatch.setattr(config_store, "_CONFIG_CACHE", {"data": None, "key": None})
    monkeypatch.setattr(config_store, "_CONFIG_CORRUPTED", False)
    monkeypatch.setattr(config_store, "_LEGACY_MERGE_DONE", True)
    monkeypatch.delenv("MNS_MASTER_KEY", raising=False)
    monkeypatch.delenv("MNS_PROD", raising=False)
    monkeypatch.setenv("MNS_ALLOW_EPHEMERAL_MASTER_KEY", "1")
    monkeypatch.setattr(crypto_utils, "KEYRING_AVAILABLE", False)
    monkeypatch.setattr(crypto_utils, "_is_windows", lambda: False)

    monkeypatch.setattr(
        config_store,
        "_encode_secret",
        lambda _v, _k="default": {"scheme": "keyring", "value": ""},
    )
    monkeypatch.setattr(
        config_store,
        "_decode_secret",
        lambda _e, _k="default": "stale-wrong-key",
    )

    with pytest.raises(RuntimeError, match="verify persisted master key"):
        mk_mod.get_or_create_master_key()


def test_init_security_preserves_samesite_strict() -> None:
    """D1B-02: Flask-Talisman must not downgrade SESSION_COOKIE_SAMESITE from Strict to Lax."""
    from security_config import init_security

    app = Flask(__name__)
    app.secret_key = "test-secret-key-for-security-config-tests-only"
    init_security(app)
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Strict"


def test_sync_legacy_preferences_keeps_legacy_when_runtime_lacks_master_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D1B-03: Never delete newer legacy config if it has mns_master_key and runtime lacks it."""
    import config_store
    from config import io as config_io

    runtime_cfg = tmp_path / "runtime" / "config.json"
    legacy_cfg = tmp_path / "legacy" / "config.json"
    runtime_cfg.parent.mkdir(parents=True)
    legacy_cfg.parent.mkdir(parents=True)
    runtime_cfg.write_text(json.dumps({"mistral_model": "mistral-small-latest"}), encoding="utf-8")
    legacy_cfg.write_text(
        json.dumps({"mistral_model": "mistral-medium-3.5", "mns_master_key": {"scheme": "dpapi", "value": "x"}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(config_store, "APP_DATA_DIR", runtime_cfg.parent)
    monkeypatch.setattr(config_store, "CONFIG_FILE", runtime_cfg)
    monkeypatch.setattr(config_store, "LEGACY_CONFIG_FILE", legacy_cfg)
    monkeypatch.setattr(config_store, "_CONFIG_CACHE", {"data": None, "key": None})
    monkeypatch.setattr(config_store, "_CONFIG_CORRUPTED", False)

    with pytest.raises(RuntimeError, match="retained legacy mns_master_key"):
        config_io._sync_legacy_preferences_into_runtime("sync")
    assert legacy_cfg.exists()


def test_config_json_template_has_no_undecodable_master_key_stub() -> None:
    """D1B-06: Copying config.json.template must not hold a truthy empty keyring master key."""
    template_path = Path(__file__).resolve().parents[1] / "config.json.template"
    data = json.loads(template_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert not data.get("mns_master_key")
    assert not data.get("flask_secret_key")
    assert data.get("api_credentials") == {}


def test_save_api_credentials_refuses_unreadable_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """D1B-04: save_api_credentials must fail closed before encoding secrets when config is unreadable."""
    import config_store
    import credential_manager
    import crypto_utils
    from config import io as config_io

    monkeypatch.setattr(config_store, "load_config", lambda: config_io._TransientDefaultConfig({}))
    encode_calls: list[str] = []
    monkeypatch.setattr(
        crypto_utils,
        "_encode_secret",
        lambda _v, k="default": encode_calls.append(k) or {"scheme": "ephemeral", "value": ""},
    )

    with pytest.raises(RuntimeError, match="corrupted or unreadable"):
        credential_manager.save_api_credentials(mistral_api_key="sk-test-key-1234567890")
    assert encode_calls == []


def test_require_extension_token_rejects_empty_and_non_ascii_before_provisioning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D1B-09: Empty/non-ASCII Bearer tokens must not trigger get_or_create_extension_api_token."""
    import credential_manager

    called = False

    def _boom() -> str:
        nonlocal called
        called = True
        return "expected-secret"

    monkeypatch.setattr(credential_manager, "get_or_create_extension_api_token", _boom)

    class _Req:
        def __init__(self, auth: str) -> None:
            self.headers = {"Authorization": auth}

    ok_empty, _ = credential_manager.require_extension_token(_Req("Bearer   "))
    ok_non_ascii, _ = credential_manager.require_extension_token(_Req("Bearer あいう"))
    assert ok_empty is False
    assert ok_non_ascii is False
    assert called is False


def test_dpapi_unprotect_sets_ui_forbidden_and_windows_ephemeral_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D1B-07 & D1B-08: _dpapi_unprotect passes UI_FORBIDDEN; Windows DPAPI failure honors ephemeral fallback."""
    import crypto_utils

    monkeypatch.setattr(crypto_utils, "KEYRING_AVAILABLE", False)
    monkeypatch.setattr(crypto_utils, "_is_windows", lambda: True)
    monkeypatch.setattr(crypto_utils, "_dpapi_protect", lambda _raw: (_ for _ in ()).throw(OSError("dpapi down")))
    monkeypatch.setenv("MNS_EPHEMERAL_FALLBACK", "1")
    monkeypatch.setattr(crypto_utils, "_EPHEMERAL_CREDENTIALS", {})

    encoded = crypto_utils._encode_secret("secret-value", "test_win_ephemeral")
    assert encoded == {"scheme": "ephemeral", "value": ""}


def test_url_masking_filter_handles_format_placeholders_and_exception_args() -> None:
    """D1A-04: Placeholder in query param must not crash formatting; Exception args must be masked."""
    from logging_config import URLMaskingFilter

    filt = URLMaskingFilter()

    rec1 = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="GET %s?sse_ticket=%s&other=1",
        args=("/api/stocks/stream", "TOPSECRET_TICKET"),
        exc_info=None,
    )
    assert filt.filter(rec1)
    rendered1 = rec1.getMessage()
    assert "TOPSECRET_TICKET" not in rendered1
    assert "sse_ticket=[REDACTED]" in rendered1
    assert "other=1" in rendered1

    rec2 = logging.LogRecord(
        name="t",
        level=logging.ERROR,
        pathname="",
        lineno=0,
        msg="Request failed: %s",
        args=(RuntimeError("failed on https://host/stream?sse_ticket=EXC_SECRET&x=1"),),
        exc_info=None,
    )
    assert filt.filter(rec2)
    rendered2 = rec2.getMessage()
    assert "EXC_SECRET" not in rendered2
    assert "sse_ticket=[REDACTED]" in rendered2


def test_warning_dedup_filter_rejects_nan_and_inf(monkeypatch: pytest.MonkeyPatch) -> None:
    """D1A-10: NaN/Inf MNS_LOG_WARNING_DEDUP_SEC must fall back to finite default 60.0."""
    from logging_config import WarningDeduplicationFilter

    for bad in ("inf", "-inf", "nan", "NaN"):
        monkeypatch.setenv("MNS_LOG_WARNING_DEDUP_SEC", bad)
        filt = WarningDeduplicationFilter()
        assert filt.dedup_window_sec == 60.0


def test_error_response_resolves_enum_name_and_unhandled_500_details() -> None:
    """D1A-05: ErrorCode string names resolve to numeric codes; 500 includes details.reason=None."""
    from error_codes import ErrorCode
    from error_handlers import AppError, register_error_handlers
    from utils.stock_payload import error_response

    app = Flask(__name__)
    app.config["PROPAGATE_EXCEPTIONS"] = False
    register_error_handlers(app)

    @app.route("/app-err")
    def _app_err():
        raise AppError("bad", status_code=400, error_code="INVALID_INPUT")

    @app.route("/crash")
    def _crash():
        raise RuntimeError("boom")

    with app.app_context():
        resp, status = error_response("INVALID_INPUT", 400)
        assert status == 400
        payload = resp.get_json()
        assert payload["code"] == "INVALID_INPUT"
        assert payload["error_code"] == int(ErrorCode.INVALID_INPUT)

    client = app.test_client()
    r1 = client.get("/app-err")
    assert r1.get_json()["error_code"] == int(ErrorCode.INVALID_INPUT)
    r2 = client.get("/crash")
    assert r2.status_code == 500
    assert r2.get_json()["details"] == {"reason": None}


def test_normalize_history_frame_accepts_lowercase_ohlcv() -> None:
    """D2-09: Lowercase OHLCV columns accepted by extract_batch_history must normalize cleanly."""
    from utils.normalization import normalize_history_frame

    df = pd.DataFrame(
        {"open": [10.0], "high": [12.0], "low": [9.0], "close": [11.5], "volume": [1000]},
        index=pd.to_datetime(["2026-11-27"]),
    )
    out = normalize_history_frame(df)
    assert not out.empty
    assert "Close" in out.columns
    assert float(out["Close"].iloc[0]) == 11.5


def test_get_current_usdjpy_rate_survives_non_dict_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """D2-10b: Non-dict indices cache or USDJPY entry must not raise AttributeError."""
    from app_state import app_state
    from utils.stock_payload import get_current_usdjpy_rate

    monkeypatch.setattr(app_state.market, "current_indices_cache", {"USDJPY": None})
    rate, is_est = get_current_usdjpy_rate()
    assert isinstance(rate, float) and rate > 0
    assert isinstance(is_est, bool)


def test_extract_chat_content_list_only_think_tags() -> None:
    """D2-10c: List chunks containing only <think>...</think> must return fallback sentinel."""
    from utils.validators import extract_chat_content

    res = extract_chat_content(
        {
            "choices": [
                {
                    "message": {
                        "content": [{"type": "text", "text": "<think>internal reasoning only</think>"}]
                    }
                }
            ]
        },
        preserve_for_history=False,
    )
    assert res == "(思考プロセスのみの応答でした)"


@pytest.mark.parametrize(
    "ip_text",
    [
        "169.254.169.254",
        "::ffff:169.254.169.254",
        "64:ff9b::169.254.169.254",
        "64:ff9b:1::a9fe:a9fe",
        "2002:a9fe:a9fe::1",
        "2001:0:a9fe:a9fe:0:0:0:1",
        "2001:0:0:0:0:0:5601:5601",
        "::ffff:0:169.254.169.254",
        "::169.254.169.254",
        "fe80::1",
        "fd00:ec2::254",
    ],
)
def test_is_blocked_ip_catches_ipv6_transition_imds_encodings(ip_text: str) -> None:
    """D2-03: NAT64, 6to4, Teredo, SIIT, and IPv4-compatible encodings of IMDS must be blocked."""
    from utils.networking import _is_blocked_ip

    assert _is_blocked_ip(ipaddress.ip_address(ip_text)) is True


def test_inspect_outbound_base_url_rejects_query_and_fragment() -> None:
    """D4-17: Base URLs containing ?query or #fragment must be rejected."""
    from utils.networking import validate_outbound_base_url

    with patch("utils.networking.is_blocked_outbound_host", return_value=False):
        ok_q, _ = validate_outbound_base_url("https://api.example.com/v1?api-version=1")
        ok_f, _ = validate_outbound_base_url("https://api.example.com/v1#frag")
    assert ok_q is False
    assert ok_f is False


def test_remote_auth_requires_proxy_fix_for_untrusted_origin_carveout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D2-08: Bare MNS_ALLOW_REMOTE_API=1 without MNS_PROXY_FIX=1 must not bypass origin checks."""
    from flask import request

    from app_factory.hooks import _is_remote_admin_authenticated
    from utils.networking import require_trusted_or_admin

    monkeypatch.setenv("MNS_ALLOW_REMOTE_API", "1")
    monkeypatch.setenv("MNS_PROXY_FIX", "0")
    monkeypatch.setenv("MNS_ADMIN_TOKEN", "a" * 32)

    app = Flask(__name__)
    with app.test_request_context(
        "/api/stocks",
        method="POST",
        headers={"X-MNS-Admin-Token": "wrong-token", "Origin": "https://evil.example"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    ):
        assert _is_remote_admin_authenticated(request) is False

    with app.test_request_context(
        "/api/stocks",
        method="POST",
        headers={"X-MNS-Admin-Token": "a" * 32, "Origin": "https://evil.example"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    ):
        assert _is_remote_admin_authenticated(request) is True
        ok, _reason = require_trusted_or_admin(request, require_origin=True)
        assert ok is False
