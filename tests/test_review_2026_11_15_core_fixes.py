"""Regression tests for the 2026-11-15 core/config/service review fixes."""

from __future__ import annotations

import builtins
import gc
import json
import logging
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet

import config_store
from services import ai_service
from services.ai_portfolio_service import sanitize_ai_portfolio
from services.search import tavily
from services.stock import retry as retry_mod
from services.stock.errors import YFRateLimitError
from utils.threading import DaemonThreadPoolExecutor


def _reset_config_state() -> None:
    config_store._CONFIG_CACHE["data"] = None
    config_store._CONFIG_CACHE["key"] = None
    config_store._CONFIG_CORRUPTED = False
    config_store._LEGACY_MERGE_DONE = True


def test_transient_config_read_error_is_not_corruption(tmp_path, monkeypatch):
    runtime = tmp_path / "config.json"
    runtime.write_text(json.dumps({"mns_master_key": "existing"}), encoding="utf-8")
    monkeypatch.setattr(config_store, "CONFIG_FILE", runtime)
    monkeypatch.setattr(config_store, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config_store, "LEGACY_CONFIG_FILE", tmp_path / "legacy.json")
    _reset_config_state()

    real_open = builtins.open

    def flaky_open(file, *args, **kwargs):
        if Path(file) == runtime:
            raise OSError("transient AV lock")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", flaky_open)
    cfg = config_store.load_config()

    assert cfg["mistral_model"] == config_store.DEFAULT_CONFIG["mistral_model"]
    assert config_store._CONFIG_CORRUPTED is False
    assert config_store._CONFIG_CACHE["data"] is None
    assert config_store._CONFIG_CACHE["key"] is None


def test_save_config_preserves_protected_secrets_when_caller_has_defaults(tmp_path, monkeypatch):
    runtime = tmp_path / "config.json"
    runtime.write_text(
        json.dumps(
            {
                "mns_master_key": {"scheme": "keyring", "value": ""},
                "flask_secret_key": {"scheme": "keyring", "value": ""},
                "extension_api_token": {"scheme": "keyring", "value": ""},
                "mistral_model": "old-model",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_store, "CONFIG_FILE", runtime)
    monkeypatch.setattr(config_store, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config_store, "LEGACY_CONFIG_FILE", tmp_path / "legacy.json")
    _reset_config_state()

    config_store.save_config({"mistral_model": "new-model"}, create_backup=False)

    saved = json.loads(runtime.read_text(encoding="utf-8"))
    assert saved["mistral_model"] == "new-model"
    assert saved["mns_master_key"] == {"scheme": "keyring", "value": ""}
    assert saved["flask_secret_key"] == {"scheme": "keyring", "value": ""}
    assert saved["extension_api_token"] == {"scheme": "keyring", "value": ""}


def test_invalid_env_master_key_fails_closed(monkeypatch):
    monkeypatch.setenv("MNS_MASTER_KEY", "not-a-valid-fernet-key")
    with pytest.raises(RuntimeError, match="valid Fernet"):
        config_store.get_or_create_master_key()


def test_invalid_stored_master_key_fails_closed():
    with (
        patch.object(
            config_store, "load_config", return_value={"mns_master_key": {"x": 1}}
        ),
        patch.object(config_store, "_decode_secret", return_value="not-a-fernet-key"),
        patch("utils.env_helpers._is_production_env", return_value=False),
    ):
        with pytest.raises(RuntimeError, match="valid Fernet"):
            config_store.get_or_create_master_key()


def test_save_config_refuses_defaults_before_legacy_migration(tmp_path, monkeypatch):
    runtime = tmp_path / "config.json"
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"mns_master_key": "secret"}), encoding="utf-8")
    monkeypatch.setattr(config_store, "CONFIG_FILE", runtime)
    monkeypatch.setattr(config_store, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config_store, "LEGACY_CONFIG_FILE", legacy)
    _reset_config_state()

    with pytest.raises(RuntimeError, match="legacy config migration"):
        config_store.save_config({"mistral_model": "x"}, create_backup=False)
    assert not runtime.exists()


def test_tavily_half_open_state_is_detected():
    class _State:
        def get(self, key, default=None):
            return "HALF_OPEN" if key == "status" else default

    with patch(
        "app_state.app_state.market.get_circuit_state",
        return_value=_State(),
    ):
        assert tavily._is_tavily_circuit_half_open() is True


def test_ai_portfolio_sanitizer_drops_overflowing_numbers():
    portfolio = {
        "title": "t",
        "items": [
            {"symbol": "AAPL", "market": "us", "weight_pct": 10**400},
            {
                "symbol": "MSFT",
                "market": "us",
                "weight_pct": 50,
                "target_price": 10**400,
            },
        ],
    }
    cleaned = sanitize_ai_portfolio(portfolio)
    assert cleaned["items"] == []


def test_yfinance_retry_records_one_rate_limit_per_logical_call(monkeypatch):
    app_state_ref = retry_mod._get_app_state_cached()
    assert app_state_ref is not None
    monkeypatch.setattr(app_state_ref.market, "is_yf_rate_limited", lambda: False)
    monkeypatch.setattr(app_state_ref.market, "yfinance_lock", __import__("threading").Lock())
    with patch("services.stock_provider._handle_yf_rate_limit") as mark:
        @retry_mod.with_yfinance_retry(max_retries=2, base_delay=0.0)
        def always_limited():
            raise YFRateLimitError()

        with pytest.raises(YFRateLimitError):
            always_limited()

    assert mark.call_count == 1


def test_tier_restriction_does_not_match_incidental_403_substring():
    assert (
        ai_service._is_mistral_tier_restriction_error(
            Exception("read timed out after 1403 ms"), None, 0
        )
        is False
    )
    assert (
        ai_service._is_mistral_tier_restriction_error(
            Exception("403 Forbidden: model not allowed"), None, 0
        )
        is True
    )


def test_mistral_cache_key_discriminates_execution_mode():
    chat = ai_service._build_mistral_cache_key(
        "mistral-small", [{"role": "user", "content": "hi"}], 100, None
    )
    agents = ai_service._build_mistral_cache_key(
        "mistral-small",
        [{"role": "user", "content": "hi"}],
        100,
        None,
        execution_mode="agents_sdk",
    )
    assert chat != agents


def test_request_fingerprint_binds_symbol_and_message():
    from routes.api_analysis import _request_fingerprint

    aapl = _request_fingerprint(
        {"symbol": "AAPL", "market": "us", "message": "hello"}
    )
    msft = _request_fingerprint(
        {"symbol": "MSFT", "market": "us", "message": "hello"}
    )
    changed_message = _request_fingerprint(
        {"symbol": "AAPL", "market": "us", "message": "different"}
    )
    assert aapl != msft
    assert aapl != changed_message
    assert aapl == _request_fingerprint(
        {"symbol": "AAPL", "market": "us", "message": "hello"}
    )


def test_provider_error_details_allowlist_drops_prompt_text():
    from routes.api_analysis import _safe_provider_error_details

    safe = _safe_provider_error_details(
        {
            "prompt": "SECRET PROMPT",
            "provider_error": {
                "status_code": 429,
                "type": "rate_limit",
                "message": "rate limited",
            },
        }
    )
    assert "prompt" not in safe
    assert safe["provider_error"]["status_code"] == 429
    assert safe["provider_error"]["type"] == "rate_limit"


def test_zero_wait_still_observes_shutdown():
    from app_state import app_state

    with patch.object(app_state.execution.shutdown_event, "is_set", return_value=True):
        assert ai_service._wait_for_rate_limit_slot(0.0) == "shutdown"


def test_partial_user_stocks_target_is_retried(tmp_path, monkeypatch):
    from utils import storage

    legacy = tmp_path / "legacy_user_stocks.json"
    target = tmp_path / "user_stocks.json"
    legacy.write_text(json.dumps({"us": {"AAPL": "Apple"}}), encoding="utf-8")
    target.write_text("", encoding="utf-8")
    monkeypatch.setattr(storage, "LEGACY_USER_STOCKS_FILE", str(legacy))
    monkeypatch.setattr(storage, "USER_STOCKS_FILE", str(target))
    monkeypatch.setattr(
        storage.config_store,
        "get_or_create_master_key",
        lambda: Fernet.generate_key().decode("ascii"),
    )

    assert storage._migrate_legacy_user_stocks() is True
    assert not legacy.exists()
    assert target.stat().st_size > 0
    assert json.loads(target.read_text(encoding="utf-8"))["scheme"] == "fernet"


def test_daemon_executor_recovers_from_inherited_dead_workers():
    executor = DaemonThreadPoolExecutor(max_workers=1)
    try:
        # Simulate a forked child: the parent's worker set is inherited but
        # none of those threads exist here, and an idle token is present.
        executor._threads.clear()
        executor._idle_semaphore.release()
        future = executor.submit(lambda: 42)
        assert future.result(timeout=5) == 42
    finally:
        executor.shutdown(wait=True)


def test_chat_history_closes_worker_connection_on_thread_exit(tmp_path, monkeypatch):
    from utils import chat_history

    monkeypatch.setattr(chat_history, "DB_PATH", tmp_path / "chat.db")
    chat_history._reset_db_state()
    store = chat_history.SQLiteChatHistoryStore()

    def worker():
        store.add_message("thread-session", {"role": "user", "content": "hello"})

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    gc.collect()

    assert store._active_conns == set()
    store.close_all()


def test_content_length_over_json_limit_raises_413():
    from flask import Flask
    from werkzeug.exceptions import RequestEntityTooLarge

    from utils.text_utils import _parse_json_request

    app = Flask(__name__)
    with app.test_request_context(
        "/api/chat",
        method="POST",
        data=b"x" * 100,
        content_type="application/json",
    ):
        with pytest.raises(RequestEntityTooLarge):
            _parse_json_request(max_size=10)


def test_gunicorn_rejects_preload_app():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "gunicorn_conf_preload_test", Path(__file__).resolve().parent.parent / "gunicorn.conf.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = MagicMock()
    server.num_workers = 1
    server.cfg.preload_app = True
    server.cfg.threads = module.required_threads
    with pytest.raises(SystemExit):
        module.on_starting(server)


def test_logging_falls_back_when_log_dir_is_a_file(tmp_path, monkeypatch):
    from logging_config import init_logging

    blocked = tmp_path / "not-a-directory"
    blocked.write_text("x", encoding="utf-8")
    monkeypatch.setenv("MNS_LOG_DIR", str(blocked))

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    root.handlers.clear()
    try:
        mock_app = MagicMock()
        mock_app.logger = logging.getLogger("test_logging_fallback")
        mock_app.logger.handlers.clear()
        init_logging(mock_app)
        assert root.handlers
    finally:
        root.handlers[:] = saved_handlers
