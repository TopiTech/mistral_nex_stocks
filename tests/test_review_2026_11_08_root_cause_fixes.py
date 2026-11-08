"""Regression tests for the full-repository review (goal-20261108-full-review-v4).

Each test pins one root-cause fix and fails against the pre-fix code.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

import config_store
from utils.disk_cache import StockDiskCache

# ---------------------------------------------------------------------------
# ISS-03 — delete_prefix must mirror _entry_path's 120-char truncation
# ---------------------------------------------------------------------------


def test_delete_prefix_matches_keys_longer_than_120_chars(tmp_path: Path) -> None:
    cache = StockDiskCache(cache_dir=tmp_path, default_ttl=60, enable_cleanup=False)
    long_key = "hist_" + ("A" * 200)
    cache.set(long_key, {"history": [1, 2, 3]})
    assert cache.get(long_key) is not None

    removed = cache.delete_prefix(long_key[:150])

    assert removed == 1
    assert cache.get(long_key) is None


def test_delete_prefix_does_not_match_unrelated_long_key(tmp_path: Path) -> None:
    cache = StockDiskCache(cache_dir=tmp_path, default_ttl=60, enable_cleanup=False)
    cache.set("hist_" + ("A" * 200), {"v": 1})
    cache.set("hist_" + ("B" * 200), {"v": 2})

    removed = cache.delete_prefix("hist_" + ("A" * 200))

    assert removed == 1
    assert cache.get("hist_" + ("B" * 200)) == {"v": 2}


# ---------------------------------------------------------------------------
# ISS-02 — a failed mtime read must not turn a durable save into a failure
# ---------------------------------------------------------------------------


def test_save_user_stocks_succeeds_when_stat_raises(tmp_path: Path) -> None:
    from utils import storage

    target = tmp_path / "user_stocks.json"
    real_stat = os.stat

    def _stat(path, *args, **kwargs):
        if str(path) == str(target):
            raise OSError("sharing violation")
        return real_stat(path, *args, **kwargs)

    with (
        patch.object(storage, "USER_STOCKS_FILE", str(target)),
        patch.object(storage.os, "stat", side_effect=_stat),
    ):
        # Must not raise UserStocksPersistError: the atomic write succeeded.
        storage.save_user_stocks()

    assert target.exists()


# ---------------------------------------------------------------------------
# U-01 — any load failure must arm the destructive-save guard
# ---------------------------------------------------------------------------


def test_load_user_stocks_marks_failure_when_master_key_raises(tmp_path: Path) -> None:
    from app_state import app_state
    from utils import storage

    target = tmp_path / "user_stocks.json"
    target.write_text(
        json.dumps({"scheme": "fernet", "value": "not-a-real-token"}),
        encoding="utf-8",
    )
    app_state.market.user_stocks_load_error = False

    with (
        patch.object(storage, "USER_STOCKS_FILE", str(target)),
        patch.object(
            config_store,
            "get_or_create_master_key",
            side_effect=RuntimeError("mns_master_key could not be decoded"),
        ),
    ):
        storage.load_user_stocks()

    assert app_state.market.user_stocks_load_error is True


# ---------------------------------------------------------------------------
# CFG-02 — an unauthenticated request must not provision the extension token
# ---------------------------------------------------------------------------


def test_require_extension_token_rejects_without_resolving_token() -> None:
    import credential_manager

    request_obj = MagicMock()
    request_obj.headers = {}

    with patch.object(
        credential_manager, "get_or_create_extension_api_token"
    ) as mock_token:
        ok, reason = credential_manager.require_extension_token(request_obj)

    assert ok is False
    assert reason == "invalid or missing extension token"
    mock_token.assert_not_called()


def test_require_extension_token_still_accepts_valid_bearer() -> None:
    import credential_manager

    request_obj = MagicMock()
    request_obj.headers = {"Authorization": "Bearer " + "a" * 40}

    with patch.object(
        credential_manager, "get_or_create_extension_api_token", return_value="a" * 40
    ):
        ok, reason = credential_manager.require_extension_token(request_obj)

    assert ok is True
    assert reason == ""


# ---------------------------------------------------------------------------
# CFG-01 — reading an existing master key must not take the update lock
# ---------------------------------------------------------------------------


def test_get_or_create_master_key_read_path_skips_update_lock() -> None:
    from config.master_key import get_or_create_master_key

    with (
        patch.object(
            config_store, "load_config", return_value={"mns_master_key": {"x": 1}}
        ),
        patch.object(config_store, "_decode_secret", return_value="k" * 44),
        patch.object(config_store, "_master_key_update_lock") as mock_update_lock,
    ):
        assert get_or_create_master_key() == "k" * 44

    mock_update_lock.assert_not_called()


# ---------------------------------------------------------------------------
# CFG-08 — BACKEND_LOG_LEVEL must not resolve non-level module attributes
# ---------------------------------------------------------------------------


def test_log_level_lookup_rejects_non_level_attributes() -> None:
    names = logging.getLevelNamesMapping()
    assert names.get("BASICCONFIG") is None
    assert names.get("INFO") == logging.INFO

    import logging_config

    assert isinstance(logging_config.LOG_LEVEL, int)


# ---------------------------------------------------------------------------
# AI-04 — an explicit max_tokens budget must reach the OpenAI provider
# ---------------------------------------------------------------------------


def test_explicit_max_tokens_is_forwarded_to_openai_provider() -> None:
    from services.ai_service import call_mistral_chat

    with (
        patch("services.ai_service.is_openai_provider", return_value=True),
        patch(
            "services.openai_service.call_openai_chat", return_value={"choices": []}
        ) as mock_call,
    ):
        call_mistral_chat(
            "sk-test",
            [{"role": "user", "content": "hi"}],
            max_tokens=600,
            use_cache=False,
        )

    assert mock_call.call_args.kwargs["max_tokens"] == 600


def test_default_max_tokens_defers_to_provider_default() -> None:
    from services.ai_service import call_mistral_chat

    with (
        patch("services.ai_service.is_openai_provider", return_value=True),
        patch(
            "services.openai_service.call_openai_chat", return_value={"choices": []}
        ) as mock_call,
    ):
        call_mistral_chat("sk-test", [{"role": "user", "content": "hi"}], use_cache=False)

    assert mock_call.call_args.kwargs["max_tokens"] is None


# ---------------------------------------------------------------------------
# AI-05 — the coroutine timeout must not be defeated by executor shutdown
# ---------------------------------------------------------------------------


def test_run_coro_sync_does_not_block_on_stuck_worker() -> None:
    """The executor branch must not wait for a coroutine stuck in sync I/O."""
    import asyncio

    from services.ai_service import _run_coro_sync

    release = threading.Event()
    outcome: dict[str, object] = {}

    async def _stuck() -> None:
        # A synchronous block that ``wait_for`` cannot cancel.
        release.wait(30)

    def _caller() -> None:
        async def _main() -> None:
            started = time.monotonic()
            try:
                with pytest.raises(httpx.TimeoutException):
                    _run_coro_sync(_stuck(), timeout_sec=0.2)
                outcome["elapsed"] = time.monotonic() - started
            except BaseException as exc:  # pragma: no cover - diagnostic
                outcome["error"] = exc

        asyncio.run(_main())

    worker = threading.Thread(target=_caller, daemon=True)
    worker.start()
    worker.join(timeout=10)
    try:
        assert not worker.is_alive(), "_run_coro_sync blocked past its timeout"
        assert "elapsed" in outcome, outcome
        assert outcome["elapsed"] < 5.0
    finally:
        release.set()


# ---------------------------------------------------------------------------
# ROUTES-02 — caller-supplied base URLs must not reach link-local / metadata
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://[fe80::1]/v1",
        "http://[fd00:ec2::254]/v1",
        "ftp://example.com/v1",
        "not-a-url",
        "",
    ],
)
def test_validate_outbound_base_url_rejects(url: str) -> None:
    from utils.networking import validate_outbound_base_url

    ok, reason = validate_outbound_base_url(url)
    assert ok is False
    assert reason


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:11434/v1",
        "http://localhost:1234/v1",
        "http://192.168.1.10:8000/v1",
        "https://api.openai.com/v1",
    ],
)
def test_validate_outbound_base_url_allows_supported_targets(url: str) -> None:
    from utils.networking import validate_outbound_base_url

    ok, reason = validate_outbound_base_url(url)
    assert ok is True
    assert reason == ""


def test_verify_openai_connection_refuses_metadata_target() -> None:
    from services.openai_service import verify_openai_connection

    with patch("httpx.Client.get") as mock_get:
        result = verify_openai_connection(
            base_url="http://169.254.169.254/latest/meta-data", api_key="x"
        )

    assert result["ok"] is False
    mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# SVC-DATA-01 — a transient "no price data" must not delete a valid symbol
# ---------------------------------------------------------------------------


def test_prices_missing_error_is_not_an_invalid_symbol() -> None:
    from services.stock.errors import YFPricesMissingError, _is_yfinance_invalid_symbol_error

    exc = YFPricesMissingError(
        "No price data found, symbol may be delisted (period=1d)", {"period": "1d"}
    )
    assert _is_yfinance_invalid_symbol_error(exc) is False


def test_ticker_missing_error_is_still_an_invalid_symbol() -> None:
    from services.stock.errors import YFTickerMissingError, _is_yfinance_invalid_symbol_error

    exc = YFTickerMissingError("ZZZZZZ", "Symbol does not exist")
    assert _is_yfinance_invalid_symbol_error(exc) is True


def test_requests_404_text_alone_is_not_an_invalid_symbol() -> None:
    """``requests`` formats every 404 as "Not Found"; the text alone must not count."""
    from services.stock.errors import _is_yfinance_invalid_symbol_error

    assert _is_yfinance_invalid_symbol_error(RuntimeError("404 Client Error: Not Found")) is False


# ---------------------------------------------------------------------------
# ROUTES-06 — the legacy SSE alias must be covered by the cross-site block
# ---------------------------------------------------------------------------


def test_cross_site_costly_get_paths_cover_stream_alias() -> None:
    from app_factory.hooks import _CROSS_SITE_COSTLY_GET_PATHS

    assert "/api/stocks/stream" in _CROSS_SITE_COSTLY_GET_PATHS
    assert "/stocks/stream" in _CROSS_SITE_COSTLY_GET_PATHS
