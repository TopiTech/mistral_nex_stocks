"""Regression, edge-case, and error-path tests for the 2026-11-20 review fixes."""

from __future__ import annotations

import json
import math
import shutil
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pandas as pd
import pytest

from app import app
from app_state import app_state
from bg.sse_interpolator import bg_interpolate_loop
from bg.sync.process import _warm_payload_cache_from_disk
from constants import (
    DEFAULT_USDJPY_RATE,
    HISTORY_CACHE_DURATION_CLOSED_LONG,
    NEGATIVE_CACHE_TTL,
)
from market_state import MarketDataState
from routes import api_analysis as ra
from routes.stocks import quotes as quotes_routes
from services import ai_service, openai_service
from services.fallback_provider import Nikkei225JPProvider
from services.nikkei225jp_parser import (
    NIKKEI225JP_INDEX_MAP,
    extract_direct_adr_parts,
    parse_adr_all_js,
    parse_ajax_index_js,
    parse_ndy_min_js,
)
from services.realtime.scrapers import Nikkei225JPScraper
from services.stock.provider import YFinanceProvider
from services.stock_service import fetch_history_async_task
from utils.caching import (
    _get_cached_value,
    _set_cached_value,
    clear_cache_key,
    clear_cache_key_in_bucket,
)
from utils.text_utils import json_safe_tool_value, sanitize_prompt_text
from utils.validators import sanitize_provider_error_info

ROOT = Path(__file__).resolve().parent.parent


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for frontend VM regression tests")
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT,
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout


# ---------------------------------------------------------------------------
# ISS-01: Negative history cache lookup & stale cleanup across TTL buckets
# ---------------------------------------------------------------------------


def test_history_negative_cache_read_and_cleared_on_subsequent_success():
    symbol = "ISS01TEST"
    market = "us"
    period = "1y"
    cache_key = f"hist_{symbol}_{market}_{period}"
    pos_duration = HISTORY_CACHE_DURATION_CLOSED_LONG
    neg_duration = min(pos_duration, NEGATIVE_CACHE_TTL)
    assert neg_duration < pos_duration

    clear_cache_key(cache_key)
    with app_state.market.circuit_lock:
        app_state.market.history_circuit_states.clear()
    orig_testing = app.config.get("TESTING")
    app.config["TESTING"] = True

    try:
        error_payload = {
            "error": "履歴データを取得できませんでした",
            "error_code": 1004,
            "symbol": symbol,
            "transient": False,
        }
        _set_cached_value(cache_key, error_payload, neg_duration)

        with (
            patch("routes.stocks.quotes.is_market_open", return_value=False),
            patch("routes.stocks.quotes.require_trusted_or_admin", return_value=(True, "ok")),
            patch("routes.stocks.quotes._submit_async_history_fetch") as mock_submit,
            app.test_client() as client,
        ):
            resp = client.get(
                f"/api/stock-history?symbol={symbol}&market={market}&period={period}",
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
            assert resp.status_code == 200
            body = json.loads(resp.data)
            assert body.get("fetching") is not True
            assert body.get("error_flag") is True
            mock_submit.assert_not_called()

        # Non-error/corrupt dict in negative bucket is discarded and refetched
        _set_cached_value(cache_key, {"unexpected": True}, neg_duration)
        with (
            patch("routes.stocks.quotes.is_market_open", return_value=False),
            patch("routes.stocks.quotes.require_trusted_or_admin", return_value=(True, "ok")),
            patch(
                "routes.stocks.quotes._submit_async_history_fetch", return_value=True
            ) as mock_submit,
            app.test_client() as client,
        ):
            resp = client.get(
                f"/api/stock-history?symbol={symbol}&market={market}&period={period}",
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
            assert resp.status_code == 200
            assert json.loads(resp.data).get("fetching") is True
            mock_submit.assert_called_once()
        assert _get_cached_value(cache_key, neg_duration) is None

        # When fetch_history_async_task subsequently succeeds, stale negative cache is evicted
        _set_cached_value(cache_key, error_payload, neg_duration)
        good_payload = {
            "symbol": symbol,
            "history": [
                {
                    "x": 1763424000000,
                    "o": 100.0,
                    "h": 102.0,
                    "l": 99.0,
                    "c": 101.0,
                    "v": 1000,
                }
            ],
            "interval_used": "1d",
            "period_requested": period,
        }
        with (
            patch("services.stock_service.fetch_history_sync_impl", return_value=good_payload),
            patch.object(app_state.stock_disk_cache, "set"),
        ):
            fetch_history_async_task(symbol, market, period, cache_key, pos_duration)

        assert _get_cached_value(cache_key, neg_duration) is None
        cached_pos = _get_cached_value(cache_key, pos_duration)
        assert isinstance(cached_pos, dict)
        assert len(cached_pos.get("history", [])) == 1
    finally:
        clear_cache_key_in_bucket(cache_key, neg_duration)
        clear_cache_key(cache_key)
        with app_state.market.circuit_lock:
            app_state.market.history_circuit_states.clear()
        with app_state.history_fetch_lock:
            app_state.history_fetch_inflight.discard(cache_key)
        if orig_testing is None:
            app.config.pop("TESTING", None)
        else:
            app.config["TESTING"] = orig_testing
        assert quotes_routes.clear_cache_key is clear_cache_key


# ---------------------------------------------------------------------------
# ISS-02: Disk warmup copy-on-write vs bg_interpolate_loop M25 race guard
# ---------------------------------------------------------------------------


def test_warm_payload_cache_uses_copy_on_write_and_prevents_interpolator_clobber():
    orig_target = app_state.market.target_stocks_cache
    orig_current = app_state.market.current_stocks_cache
    orig_indices = app_state.market.current_indices_cache
    orig_shutdown = app_state.execution.shutdown_event
    with app_state.market.user_stocks_lock:
        orig_us = dict(app_state.market.user_us)
        orig_jp = dict(app_state.market.user_jp)
        orig_idx = dict(app_state.market.user_idx)

    try:
        with app_state.market.user_stocks_lock:
            app_state.market.user_us = {"AAPL": {"name": "Apple"}}
            app_state.market.user_jp = {}
            app_state.market.user_idx = {}

        initial_target_us = [{"symbol": "AAPL", "price": 150.0, "change": 1.0}]
        initial_indices = {"^N225": {"price": 38000.0, "change": 100.0}}
        app_state.market.target_stocks_cache = {
            "us": initial_target_us,
            "jp": [],
            "idx": [],
        }
        app_state.market.current_stocks_cache = {
            "us": [{"symbol": "AAPL", "price": 149.0, "change": 0.5}],
            "jp": [],
            "idx": [],
        }
        app_state.market.current_indices_cache = initial_indices

        disk_map = {
            "indices_cache": {"^DJI": {"price": 42000.0, "change": 50.0}},
            "payload_AAPL_us": {"symbol": "AAPL", "price": 152.0, "change": 2.0},
        }
        with (
            patch("bg.sync.process._get_app_bg_attr", return_value=lambda force=False: None),
            patch("bg.sync.process._default_stock_names", return_value=[]),
            patch.object(
                app_state.payload_disk_cache,
                "get",
                side_effect=lambda k, ignore_ttl=True: disk_map.get(k),
            ),
        ):
            _warm_payload_cache_from_disk()

        # Copy-on-write replaced references rather than mutating old containers in place
        assert app_state.market.current_indices_cache is not initial_indices
        assert "^DJI" in app_state.market.current_indices_cache
        assert "^N225" in app_state.market.current_indices_cache
        assert app_state.market.target_stocks_cache["us"] is not initial_target_us
        assert len(initial_target_us) == 1
        assert initial_target_us[0]["price"] == 150.0

        # Paired with bg_interpolate_loop: disk warmup mid-tick aborts write-back
        stop_evt = threading.Event()
        app_state.execution.shutdown_event = stop_evt
        stable_marker = [{"symbol": "AAPL", "price": 148.0, "change": 0.0}]
        app_state.market.current_stocks_cache = {"us": stable_marker, "jp": [], "idx": []}
        app_state.market.target_stocks_cache = {
            "us": [{"symbol": "AAPL", "price": 150.0, "change": 1.0}],
            "jp": [],
            "idx": [],
        }
        app_state.market.current_indices_cache = {}

        def _warm_mid_tick(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            with app_state.market.user_stocks_lock:
                app_state.market.user_us = {"MSFT": {"name": "Microsoft"}}
            with (
                patch("bg.sync.process._get_app_bg_attr", return_value=lambda force=False: None),
                patch("bg.sync.process._default_stock_names", return_value=[]),
                patch.object(
                    app_state.payload_disk_cache,
                    "get",
                    side_effect=lambda k, ignore_ttl=True: (
                        {"symbol": "MSFT", "price": 400.0, "change": 5.0}
                        if k == "payload_MSFT_us"
                        else None
                    ),
                ),
            ):
                _warm_payload_cache_from_disk()
            stop_evt.set()
            return [{"symbol": "AAPL", "price": 149.5, "change": 0.75}]

        with (
            patch.object(app_state.sse_announcer_mode1, "listener_count", return_value=1),
            patch(
                "bg.sse_interpolator._interpolate_and_fluctuate_market",
                side_effect=_warm_mid_tick,
            ),
            patch.object(stop_evt, "wait", return_value=False),
        ):
            bg_interpolate_loop()

        assert app_state.market.current_stocks_cache["us"] is stable_marker
        assert any(s["symbol"] == "MSFT" for s in app_state.market.target_stocks_cache["us"])
    finally:
        with app_state.market.user_stocks_lock:
            app_state.market.user_us = orig_us
            app_state.market.user_jp = orig_jp
            app_state.market.user_idx = orig_idx
        app_state.market.target_stocks_cache = orig_target
        app_state.market.current_stocks_cache = orig_current
        app_state.market.current_indices_cache = orig_indices
        app_state.execution.shutdown_event = orig_shutdown


# ---------------------------------------------------------------------------
# ISS-03: MarketDataState.update_usdjpy_rate invalid input preservation
# ---------------------------------------------------------------------------


def test_update_usdjpy_rate_preserves_valid_rate_and_rejects_fresh_ts_on_invalid():
    state = MarketDataState()
    state.last_usdjpy_rate = 0.0
    state.last_usdjpy_rate_ts = 0.0

    # Empty state + invalid rate resets to DEFAULT_USDJPY_RATE with stale ts=0.0 (not supplied ts!)
    rate, ts = state.update_usdjpy_rate(0.0, ts=99999.0)
    assert rate == DEFAULT_USDJPY_RATE
    assert ts == 0.0

    # Valid update sets both rate and timestamp
    rate, ts = state.update_usdjpy_rate(153.25, ts=12345.0)
    assert rate == 153.25
    assert ts == 12345.0

    # Subsequent invalid values must NOT overwrite valid rate or advance timestamp
    for bad_val in (0, -150.0, float("nan"), float("inf"), float("-inf"), None, "invalid", True):
        got_rate, got_ts = state.update_usdjpy_rate(bad_val, ts=88888.0)
        assert got_rate == 153.25
        assert got_ts == 12345.0


# ---------------------------------------------------------------------------
# ISS-04 & ISS-05: OpenAI embeddings circuit abort + YFinance download_batch None
# ---------------------------------------------------------------------------


def _set_openai_circuit_state(state: dict[str, Any] | None) -> None:
    with app_state.market.circuit_lock:
        if state is None:
            app_state.market.circuit_states.pop("openai_compatible", None)
        else:
            app_state.market.circuit_states["openai_compatible"] = dict(state)


def test_openai_embeddings_batch_aborts_on_server_circuit_open_and_exception_429_4xx():
    _set_openai_circuit_state(None)
    calls_500 = 0

    class _Client500:
        def post(self, *args: Any, **kwargs: Any) -> Any:
            nonlocal calls_500
            calls_500 += 1
            return SimpleNamespace(status_code=503, text="upstream down")

    try:
        # Put circuit in HALF_OPEN so first failure immediately re-opens circuit
        _set_openai_circuit_state(
            {"status": "HALF_OPEN", "open_until": 0.0, "timeout_streak": 0, "probing": False}
        )
        with (
            patch("services.openai_service.get_openai_base_url", return_value="https://api.openai.com/v1"),
            patch("services.openai_service.validate_outbound_base_url", return_value=(True, "ok")),
            patch("services.openai_service._get_openai_client", return_value=_Client500()),
        ):
            out = openai_service.get_openai_embeddings_batch(
                ["a", "b", "c", "d"],
                api_key="sk-test-1234567890",
                batch_size=1,
            )
        assert calls_500 == 1
        assert out == [None, None, None, None]
        assert app_state.market.is_circuit_open("openai_compatible")

        # Raised exception carrying status_code=429 aborts remaining batches immediately
        _set_openai_circuit_state(None)
        calls_429 = 0

        class _RateLimitExc(Exception):
            status_code = 429

        class _Client429Exc:
            def post(self, *args: Any, **kwargs: Any) -> Any:
                nonlocal calls_429
                calls_429 += 1
                raise _RateLimitExc("rate limited")

        with (
            patch("services.openai_service.get_openai_base_url", return_value="https://api.openai.com/v1"),
            patch("services.openai_service.validate_outbound_base_url", return_value=(True, "ok")),
            patch("services.openai_service._get_openai_client", return_value=_Client429Exc()),
        ):
            out_429 = openai_service.get_openai_embeddings_batch(
                ["a", "b", "c"],
                api_key="sk-test-1234567890",
                batch_size=1,
            )
        assert calls_429 == 1
        assert out_429 == [None, None, None]

        # Raised exception carrying status_code=401 aborts remaining batches immediately
        _set_openai_circuit_state(None)
        calls_401 = 0

        class _AuthExc(Exception):
            def __init__(self) -> None:
                super().__init__("unauthorized")
                self.response = SimpleNamespace(status_code=401)

        class _Client401Exc:
            def post(self, *args: Any, **kwargs: Any) -> Any:
                nonlocal calls_401
                calls_401 += 1
                raise _AuthExc()

        with (
            patch("services.openai_service.get_openai_base_url", return_value="https://api.openai.com/v1"),
            patch("services.openai_service.validate_outbound_base_url", return_value=(True, "ok")),
            patch("services.openai_service._get_openai_client", return_value=_Client401Exc()),
        ):
            out_401 = openai_service.get_openai_embeddings_batch(
                ["a", "b", "c"],
                api_key="sk-test-1234567890",
                batch_size=1,
            )
        assert calls_401 == 1
        assert out_401 == [None, None, None]
    finally:
        _set_openai_circuit_state(None)


def test_yfinance_download_batch_handles_none_return():
    provider = YFinanceProvider()
    with app_state.market.yfinance_short_cache_lock:
        orig_short = dict(app_state.market.yfinance_short_cache)
        app_state.market.yfinance_short_cache.clear()
    try:
        with (
            patch.object(provider, "_yf_download_with_retry", return_value=None),
            patch.object(provider, "_load_history_disk_fallback", return_value=None),
            patch("services.stock.provider.logger.warning") as mock_warn,
        ):
            res_none = provider.download_batch(["AAPL"], period="5d", lightweight=True)
            assert isinstance(res_none, pd.DataFrame) and res_none.empty
            assert not any("Batch yf.download failed" in str(c) for c in mock_warn.call_args_list)

        empty_df = pd.DataFrame()
        with (
            patch.object(provider, "_yf_download_with_retry", return_value=empty_df),
            patch.object(provider, "_load_history_disk_fallback", return_value=None),
        ):
            res_empty = provider.download_batch(["MSFT"], period="5d", lightweight=True)
            assert isinstance(res_empty, pd.DataFrame) and res_empty.empty
    finally:
        with app_state.market.yfinance_short_cache_lock:
            app_state.market.yfinance_short_cache.clear()
            app_state.market.yfinance_short_cache.update(orig_short)


# ---------------------------------------------------------------------------
# ISS-06: Shared utility/parser consolidation
# ---------------------------------------------------------------------------


def test_shared_text_utils_and_nikkei225jp_parsers():
    assert ai_service._json_safe_tool_value is json_safe_tool_value
    assert openai_service._json_safe_tool_value is json_safe_tool_value
    assert ai_service._sanitize_prompt_text is sanitize_prompt_text
    assert openai_service._sanitize_prompt_text is sanitize_prompt_text

    safe = json_safe_tool_value(
        {
            "flag": True,
            "bad_float": float("nan"),
            "inf": float("inf"),
            "items": {"b", "a"},
        }
    )
    assert safe["flag"] is True
    assert safe["bad_float"] is None
    assert safe["inf"] is None
    assert safe["items"] == ["a", "b"]
    assert math.isfinite(json_safe_tool_value(1.5))

    cleaned = sanitize_prompt_text("Foo<script>\x00Bar>Baz")
    assert "<" not in cleaned and ">" not in cleaned and "\x00" not in cleaned

    assert Nikkei225JPProvider.INDEX_MAP is NIKKEI225JP_INDEX_MAP
    assert Nikkei225JPScraper.INDEX_MAP is NIKKEI225JP_INDEX_MAP

    adr_js = (
        'A0[0]="' + "_".join(["7203"] + ["x"] * 20) + '"\n'
        'A0[1]="short_row"\n'
    )
    parsed_adr = parse_adr_all_js(adr_js)
    assert "7203" in parsed_adr
    assert "short" not in parsed_adr

    idx_cache: dict[int, list[str]] = {}
    parse_ajax_index_js('A[111]="38500_120_0.31"\nA[999]="bad"', idx_cache)
    assert idx_cache == {111: ["38500", "120", "0.31"]}

    parse_ndy_min_js("var NDY111V=39000,NDY111Z=+200;var NDY211V=42000,NDY211Z=-10;", idx_cache)
    # Existing 111 preserved, missing 211 populated
    assert idx_cache[111] == ["38500", "120", "0.31"]
    assert idx_cache[211][0] == "42000"

    assert extract_direct_adr_parts('<script>var A0 = "7203_3000_10";</script>') == [
        "7203",
        "3000",
        "10",
    ]
    assert extract_direct_adr_parts("<html>no data</html>") is None


# ---------------------------------------------------------------------------
# ISS-07 & ISS-08: Chat cache/DB resilience & provider_error sanitization
# ---------------------------------------------------------------------------


def test_chat_job_stores_none_on_error_and_survives_history_db_failure():
    symbol = "ISS07CHAT"
    conv_scope = "iss07scope-1234567890"
    real_executor = ThreadPoolExecutor(max_workers=1)
    orig_executor = app_state.execution.executor
    orig_testing = app.config.get("TESTING")
    orig_csrf = app.config.get("WTF_CSRF_ENABLED")
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app_state.execution.executor = real_executor
    with ra.chat_fetch_lock:
        ra.chat_fetch_inflight.clear()
        ra.chat_result_cache.clear()
        ra.chat_result_fingerprints.clear()
    with app_state.ai.chat_history_lock:
        app_state.ai.chat_history.clear()

    try:
        # 1) Background job error keeps cached result None (not _EMPTY_CHAT_REPLY)
        err_token = "iss07-error-token-001"
        err_gate = threading.Event()

        def _delayed_error(*args: Any, **kwargs: Any) -> str:
            err_gate.wait(timeout=5)
            raise RuntimeError("boom")

        with (
            patch("routes.api_analysis.extract_api_key", return_value="test-key-32-chars"),
            patch("routes.api_analysis.get_stock_info_cached", return_value={}),
            patch("routes.api_analysis.CHAT_PREPARE_WAIT_SEC", 0.01),
            patch(
                "routes.api_analysis._call_mistral_chat_with_retry",
                side_effect=_delayed_error,
            ),
            app.test_client() as client,
        ):
            with client.session_transaction() as sess:
                sess["mns_analysis_conversation"] = conv_scope
            resp = client.post(
                "/api/chat",
                json={
                    "market": "us",
                    "symbol": symbol,
                    "message": "Trigger failure",
                    "request_token": err_token,
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
            assert resp.status_code == 200
            assert json.loads(resp.data).get("fetching") is True
            err_gate.set()
            real_executor.shutdown(wait=True)

            with ra.chat_fetch_lock:
                entries = [
                    val
                    for key, val in ra.chat_result_cache.items()
                    if err_token in key
                ]
            assert len(entries) == 1
            _, cached_res, cached_err = entries[0]
            assert cached_res is None
            assert isinstance(cached_err, RuntimeError)

        # 2) Auxiliary history write failure on sync & cache hit paths does not 500
        real_executor = ThreadPoolExecutor(max_workers=1)
        app_state.execution.executor = real_executor
        ok_token = "iss07-ok-token-00001"

        orig_append = app_state.ai.chat_history.append_message_if_not_duplicate
        call_count = 0

        def _fail_assistant_write(key: str, msg: dict[str, Any], normalize: Any = None) -> None:
            nonlocal call_count
            call_count += 1
            if msg.get("role") == "assistant":
                raise sqlite3.OperationalError("database is locked")
            return orig_append(key, msg, normalize=normalize)

        with (
            patch("routes.api_analysis.extract_api_key", return_value="test-key-32-chars"),
            patch("routes.api_analysis.get_stock_info_cached", return_value={}),
            patch("routes.api_analysis.CHAT_PREPARE_WAIT_SEC", 2.0),
            patch(
                "routes.api_analysis._call_mistral_chat_with_retry",
                return_value="Clean Sync Reply",
            ),
            patch.object(
                app_state.ai.chat_history,
                "append_message_if_not_duplicate",
                side_effect=_fail_assistant_write,
            ),
            app.test_client() as client,
        ):
            with client.session_transaction() as sess:
                sess["mns_analysis_conversation"] = conv_scope
            resp1 = client.post(
                "/api/chat",
                json={
                    "market": "us",
                    "symbol": symbol,
                    "message": "DB locked assistant write",
                    "request_token": ok_token,
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
            assert resp1.status_code == 200
            assert json.loads(resp1.data)["reply"] == "Clean Sync Reply"

            resp2 = client.post(
                "/api/chat",
                json={
                    "market": "us",
                    "symbol": symbol,
                    "message": "DB locked assistant write",
                    "request_token": ok_token,
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
            assert resp2.status_code == 200
            assert json.loads(resp2.data)["reply"] == "Clean Sync Reply"
            assert call_count >= 2
    finally:
        real_executor.shutdown(wait=False)
        app_state.execution.executor = orig_executor
        with ra.chat_fetch_lock:
            ra.chat_fetch_inflight.clear()
            ra.chat_result_cache.clear()
            ra.chat_result_fingerprints.clear()
        with app_state.ai.chat_history_lock:
            app_state.ai.chat_history.clear()
        if orig_testing is None:
            app.config.pop("TESTING", None)
        else:
            app.config["TESTING"] = orig_testing
        if orig_csrf is None:
            app.config.pop("WTF_CSRF_ENABLED", None)
        else:
            app.config["WTF_CSRF_ENABLED"] = orig_csrf


def test_analyze_v2_sanitizes_provider_error_secrets():
    sanitized = sanitize_provider_error_info(
        {
            "message": "Upstream error with Authorization: Bearer super-secret-token-xyz",
            "status_code": 401,
            "code": "auth_failed",
            "request_dump": {"api_key": "sk-secret-value"},
        }
    )
    assert "super-secret-token-xyz" not in sanitized["message"]
    assert "request_dump" not in sanitized
    assert sanitized["status_code"] == 401

    symbol = "ISS08ERR"
    token = "iss08-err-token-0001"
    orig_executor = app_state.execution.executor
    orig_testing = app.config.get("TESTING")
    orig_csrf = app.config.get("WTF_CSRF_ENABLED")
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    real_executor = ThreadPoolExecutor(max_workers=1)
    app_state.execution.executor = real_executor
    with ra.analyze_fetch_lock:
        ra.analyze_fetch_inflight.clear()
        ra.analyze_result_cache.clear()

    analyze_gate = threading.Event()
    try:
        provider_err_resp = {
            "error": {
                "message": "Invalid api_key=sk-supersecret999 provided",
                "status_code": 401,
                "headers": {"Authorization": "Bearer secret"},
            }
        }

        def _delayed_provider_err(*args: Any, **kwargs: Any) -> dict[str, Any]:
            analyze_gate.wait(timeout=5)
            return provider_err_resp

        with (
            patch("routes.api_analysis.extract_api_key", return_value="test-key-32-chars"),
            patch("routes.api_analysis.get_stock_info_cached", return_value={}),
            patch(
                "routes.api_analysis.collect_symbol_research_context",
                return_value={
                    "context": "",
                    "articles": [],
                    "query_variants": [],
                    "source_status": {},
                },
            ),
            patch("routes.api_analysis.CHAT_PREPARE_WAIT_SEC", 0.01),
            patch("routes.api_analysis.call_mistral_chat", side_effect=_delayed_provider_err),
            app.test_client() as client,
        ):
            resp1 = client.post(
                "/api/analyze-v2",
                json={
                    "market": "us",
                    "symbol": symbol,
                    "request_token": token,
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
            assert resp1.status_code == 200
            assert json.loads(resp1.data).get("fetching") is True
            analyze_gate.set()
            real_executor.shutdown(wait=True)

            resp2 = client.post(
                "/api/analyze-v2",
                json={
                    "market": "us",
                    "symbol": symbol,
                    "request_token": token,
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
            body2 = json.loads(resp2.data)
            serialized = json.dumps(body2, ensure_ascii=False)
            assert "sk-supersecret999" not in serialized
            assert "Bearer secret" not in serialized
    finally:
        analyze_gate.set()
        real_executor.shutdown(wait=False)
        app_state.execution.executor = orig_executor
        with ra.analyze_fetch_lock:
            ra.analyze_fetch_inflight.clear()
            ra.analyze_result_cache.clear()
        if orig_testing is None:
            app.config.pop("TESTING", None)
        else:
            app.config["TESTING"] = orig_testing
        if orig_csrf is None:
            app.config.pop("WTF_CSRF_ENABLED", None)
        else:
            app.config["WTF_CSRF_ENABLED"] = orig_csrf


# ---------------------------------------------------------------------------
# ISS-09, ISS-10, ISS-11: Tech lines sanitization, PTS formatting, polling errors
# ---------------------------------------------------------------------------


def test_backend_generate_ai_technical_lines_normalizes_colors_and_enums():
    history_data = [
        {"date": "2026-11-10", "close": 100.0},
        {"date": "2026-11-11", "close": 105.0},
    ]
    fake_response = {
        "summary": "要点",
        "trend_bias": "Bearish (下降トレンド優勢)",
        "lines": [
            {
                "id": "l1",
                "type": "RESISTANCE",
                "label": "抵抗線",
                "color": "#aBc",
                "style": "DASHED",
                "start_date": "2026-11-10",
                "start_price": 100.0,
                "end_date": "2026-11-11",
                "end_price": 105.0,
                "description": "test",
            },
            {
                "id": "l2",
                "type": "evil-type injected-class",
                "label": "悪性色",
                "color": "red; background:url(javascript:alert(1))",
                "style": "squiggly",
                "start_date": "2026-11-10",
                "start_price": 95.0,
                "end_date": "2026-11-11",
                "end_price": 96.0,
                "description": "bad",
            },
        ],
    }
    with patch("services.ai_service.call_mistral_chat", return_value=fake_response):
        result = ai_service.generate_ai_technical_lines(
            "test-key", "AAPL", "us", "1mo", history_data
        )
    assert result["trend_bias"] == "Bearish (下降トレンド優勢)"
    l1, l2 = result["lines"]
    assert l1["type"] == "resistance"
    assert l1["style"] == "dashed"
    assert l1["color"] == "#aabbcc"
    assert l2["type"] == "support"
    assert l2["style"] == "solid"
    assert l2["color"] == "#00ff88"


def test_frontend_tech_lines_pts_and_polling_error_contracts():
    script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = (cond, msg) => {
  if (!cond) throw new Error(msg || "assertion failed");
};

// --- 1. utils.js: sanitizeHexColor & formatPtsPriceText ---
function makeEl(tag, cls = "", text = "") {
  const attrs = {};
  const classes = new Set(String(cls || "").split(/\s+/).filter(Boolean));
  const el = {
    tagName: String(tag || "div").toUpperCase(),
    className: Array.from(classes).join(" "),
    classList: {
      add: (...c) => { c.forEach((x) => classes.add(x)); el.className = Array.from(classes).join(" "); },
      remove: (...c) => { c.forEach((x) => classes.delete(x)); el.className = Array.from(classes).join(" "); },
      contains: (c) => classes.has(c),
    },
    children: [],
    style: {},
    dataset: {},
    hidden: false,
    textContent: String(text || ""),
    addEventListener() {},
    removeEventListener() {},
    appendChild(child) { this.children.push(child); child.parentNode = this; return child; },
    replaceChildren(...nodes) { this.children = []; nodes.forEach((n) => this.appendChild(n)); },
    insertBefore(newNode) { this.children.push(newNode); return newNode; },
    setAttribute(k, v) { attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null; },
    removeAttribute(k) { delete attrs[k]; },
    querySelector(sel) {
      for (const ch of this.children) {
        if (sel.startsWith(".") && ch.classList.contains(sel.slice(1))) return ch;
        const deep = ch.querySelector(sel);
        if (deep) return deep;
      }
      return null;
    },
    closest() { return null; },
  };
  return el;
}

const doc = {
  readyState: "complete",
  hidden: false,
  addEventListener() {},
  getElementById: () => null,
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: (tag) => makeEl(tag),
  createTextNode: (t) => makeEl("#text", "", t),
};
const win = {
  location: { pathname: "/", search: "" },
  addEventListener() {},
  requestAnimationFrame(cb) { cb(); return 1; },
  getComputedStyle: () => ({ getPropertyValue: () => "" }),
  formatPrice: (v, stock) => `${stock?.market === "jp" ? "¥" : "$"}${Number(v).toLocaleString("en-US")}`,
};
const ctx = {
  console,
  document: doc,
  window: win,
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  navigator: {},
  URLSearchParams,
  AbortController,
  setTimeout,
  clearTimeout,
  setInterval: () => 0,
  clearInterval() {},
  state: { stocks: { us: [], jp: [], idx: [] }, chartPrefs: {} },
  DOM: { get: () => null },
  formatPrice: win.formatPrice,
  createEl: (tag, cls, txt) => makeEl(tag, cls, txt),
  isValidHexColor: undefined,
  sanitizeHexColor: undefined,
  currentDrawerActiveWrapper: null,
  CHAT_POLL_MAX_ATTEMPTS: 2,
  CHAT_POLL_INTERVAL_MS: 0,
  fetch: async () => ({ ok: true, json: async () => ({}) }),
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("static/js/utils.js", "utf8"), ctx, { filename: "utils.js" });

assert(ctx.sanitizeHexColor("#ABC") === "#aabbcc", "3-digit hex expansion failed");
assert(ctx.sanitizeHexColor("red; background:url(x)", "#00ff88") === "#00ff88", "XSS color rejected");

for (const badPts of [0, -10, NaN, Infinity, "--", "0", "-5.2", false, true, null, undefined, ""]) {
  assert(ctx.formatPtsPriceText(badPts, { market: "jp" }) === "", `expected empty for ${String(badPts)}`);
}
assert(ctx.formatPtsPriceText(2580, { market: "jp" }) === "PTS ¥2,580", "positive numeric PTS formatted");
assert(ctx.formatPtsPriceText("2580.5", { market: "jp" }) === "PTS ¥2,580.5", "positive decimal string PTS formatted");

// --- 2. ui.js: applyCompactPtsDisplay & renderAiTechnicalLinesSummary ---
ctx.window.formatPtsPriceText = ctx.formatPtsPriceText;
vm.runInContext(fs.readFileSync("static/js/ui.js", "utf8"), ctx, { filename: "ui.js" });

const ptsNode = makeEl("div", "compact-pts");
ctx.applyCompactPtsDisplay(ptsNode, "--", { market: "jp" });
assert(ptsNode.textContent === "" && ptsNode.hidden === true && ptsNode.getAttribute("aria-hidden") === "true", "placeholder PTS hidden");
ctx.applyCompactPtsDisplay(ptsNode, 3050, { market: "jp" });
assert(ptsNode.textContent === "PTS ¥3,050" && ptsNode.hidden === false && ptsNode.getAttribute("aria-hidden") === null, "positive PTS visible");

const wrapper = makeEl("div", "stock-wrapper");
wrapper.dataset.stockKey = "us:AAPL";
const chartContainer = makeEl("div", "chart-container");
wrapper.appendChild(chartContainer);
ctx.renderAiTechnicalLinesSummary(wrapper, {
  summary: "要約テキスト",
  trend_bias: "Bearish (下降トレンド優勢)",
  lines: [
    { type: "evil bad-class", color: "javascript:alert(1)", label: "X", description: "D" },
    { type: "breakout", color: "#fff", label: "B", description: "B2" },
  ],
});
const summaryEl = wrapper.querySelector(".ai-tech-lines-summary");
assert(summaryEl, "summary element created");
const biasEl = summaryEl.querySelector(".ai-tech-bias");
assert(biasEl.className === "ai-tech-bias bearish", `unexpected bias class: ${biasEl.className}`);
const lineItems = summaryEl.querySelector(".ai-tech-lines-list").children;
assert(lineItems[0].className === "ai-tech-line-item", `unsafe line.type must be stripped: ${lineItems[0].className}`);
assert(lineItems[0].children[0].style.backgroundColor === "#00ff88", "invalid line color fell back safely");
assert(lineItems[1].className === "ai-tech-line-item breakout", "breakout type kept");
assert(lineItems[1].children[0].style.backgroundColor === "#ffffff", "short hex dot expanded");

// --- 3. chart.js: aiTechnicalLinesPlugin stroke color sanitization ---
vm.runInContext(fs.readFileSync("static/js/chart.js", "utf8"), ctx, { filename: "chart.js" });
const aiPlugin = vm.runInContext("aiTechnicalLinesPlugin", ctx);
const strokes = [];
const mockChart = {
  $aiTechnicalLines: [
    { type: "resistance", color: "not-a-color;()", start_price: 10, end_price: 20, style: "solid" },
    { type: "breakout", color: "#abc", start_price: 15, end_price: 25, style: "dashed" },
  ],
  scales: {
    x: { left: 0, right: 100, getPixelForValue: () => 50 },
    y: { getPixelForValue: (v) => 200 - v },
  },
  ctx: {
    save() {},
    restore() {},
    beginPath() {},
    moveTo() {},
    lineTo() {},
    rect() {},
    fill() {},
    fillText() {},
    measureText: () => ({ width: 20 }),
    setLineDash() {},
    stroke() { strokes.push(this.strokeStyle); },
    set strokeStyle(val) { this._stroke = val; },
    get strokeStyle() { return this._stroke; },
  },
};
aiPlugin.afterDatasetsDraw(mockChart);
assert(strokes[0] === "#ff3366", `invalid resistance color fell back: ${strokes[0]}`);
assert(strokes[2] === "#aabbcc", `valid short hex expanded: ${strokes[2]}`);

// --- 4. realtime_client.js: invalid delta pts price hides badge (both with & without utils helper) ---
const rtPtsEl = makeEl("div", "compact-pts", "PTS 100");
rtPtsEl.isConnected = true;
const rtDoc = {
  readyState: "complete",
  hidden: true,
  addEventListener() {},
  querySelectorAll: (sel) => (sel.includes(".compact-pts") ? [rtPtsEl] : []),
  getElementById: () => null,
  querySelector: () => null,
};
const rtWin = {
  addEventListener() {},
  requestAnimationFrame: () => 1,
  getComputedStyle: () => ({ getPropertyValue: () => "" }),
  formatPtsPriceText: ctx.formatPtsPriceText,
  formatPrice: win.formatPrice,
};
const rtCtx = { console, document: rtDoc, window: rtWin, setTimeout, clearTimeout };
rtCtx.globalThis = rtCtx;
vm.createContext(rtCtx);
vm.runInContext(fs.readFileSync("static/js/realtime_client.js", "utf8"), rtCtx, { filename: "realtime_client.js" });
rtWin.realtimeClient.handlePtsDeltas({ AAPL: { price: 0, market: "us" } });
assert(rtPtsEl.textContent === "" && rtPtsEl.hidden === true && rtPtsEl.getAttribute("aria-hidden") === "true", "zero PTS cleared in realtime client");
rtWin.realtimeClient.handlePtsDeltas({ AAPL: { price: 123.5, market: "us" } });
assert(rtPtsEl.textContent === "PTS $123.5" && rtPtsEl.hidden === false, `positive PTS updated: ${rtPtsEl.textContent}`);
delete rtWin.formatPtsPriceText;
rtWin.realtimeClient.handlePtsDeltas({ AAPL: { price: "--", market: "us" } });
assert(rtPtsEl.textContent === "" && rtPtsEl.hidden === true && rtPtsEl.getAttribute("aria-hidden") === "true", "standalone realtime fallback hides placeholder PTS");

// --- 5. api.js chatPollingReply & ui.js sendAiDrawerMessage reject { ok: false, fetching: false } ---
let asyncChecksCompleted = false;
process.on("exit", (code) => {
  if (code === 0 && !asyncChecksCompleted) {
    console.error("Async polling assertions did not complete before exit");
    process.exit(1);
  }
});

(async () => {
  const apiWin = {
    location: { pathname: "/", search: "" },
    addEventListener() {},
  };
  const apiCtx = {
    console,
    logger: console,
    document: doc,
    window: apiWin,
    navigator: {},
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    state: { stocks: { us: [], jp: [], idx: [] }, chartPrefs: {} },
    DOM: { get: () => null },
    URLSearchParams,
    AbortController,
    AbortSignal: globalThis.AbortSignal,
    setTimeout,
    clearTimeout,
    setInterval: () => 0,
    clearInterval() {},
    EventSource: function () {},
    CHAT_POLL_MAX_ATTEMPTS: 2,
    CHAT_POLL_INTERVAL_MS: 0,
    fetch: async () => ({
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => ({ ok: false, fetching: false, details: { reason: "モデル呼び出しに失敗" } }),
      text: async () => JSON.stringify({ ok: false, fetching: false, details: { reason: "モデル呼び出しに失敗" } }),
    }),
  };
  apiCtx.globalThis = apiCtx;
  vm.createContext(apiCtx);
  vm.runInContext(fs.readFileSync("static/js/api_client.js", "utf8"), apiCtx, { filename: "api_client.js" });
  apiCtx.APIClient = apiCtx.APIClient || apiWin.APIClient;
  vm.runInContext(fs.readFileSync("static/js/api.js", "utf8"), apiCtx, { filename: "api.js" });
  apiCtx.apiFetch = async () => ({
    response: { ok: true, status: 200 },
    data: { ok: false, fetching: false, details: { reason: "モデル呼び出しに失敗" } },
  });
  let caught = null;
  try {
    await apiCtx.chatPollingReply({ message: "hi" });
  } catch (e) {
    caught = e;
  }
  assert(caught && caught.message.includes("モデル呼び出しに失敗"), `expected chatPollingReply error, got ${caught}`);

  apiCtx.apiFetch = async () => ({
    response: { ok: true, status: 200 },
    data: { error_flag: true, fetching: false, message: "error_flag path" },
  });
  let caughtFlag = null;
  try {
    await apiCtx.chatPollingReply({ message: "hi" });
  } catch (e) {
    caughtFlag = e;
  }
  assert(caughtFlag && caughtFlag.message.includes("error_flag path"), `expected error_flag error, got ${caughtFlag}`);

  // Also test ui.js sendAiDrawerMessage inline polling fallback (when chatPollingReply isn't present)
  const drawerInput = makeEl("input");
  drawerInput.value = "質問";
  drawerInput.focus = () => {};
  const drawerBtn = makeEl("button", "", "送信");
  const drawerMsgs = makeEl("div");
  const uiDocEls = {
    aiDrawerInput: drawerInput,
    sendAiDrawerBtn: drawerBtn,
    "ai-drawer-chat-messages": drawerMsgs,
  };
  const uiPollCtx = {
    ...ctx,
    document: { ...doc, getElementById: (id) => uiDocEls[id] || null },
    createRequestToken: () => "tok-1234567890123456",
    currentDrawerSymbol: "AAPL",
    currentDrawerMarket: "us",
    streamChatReply: undefined,
    chatPollingReply: undefined,
    apiFetch: async () => ({
      response: { ok: true, status: 200 },
      data: { ok: false, fetching: false, details: { reason: "ドロワーHTTP200エラー" } },
    }),
  };
  uiPollCtx.globalThis = uiPollCtx;
  vm.createContext(uiPollCtx);
  vm.runInContext(fs.readFileSync("static/js/ui.js", "utf8"), uiPollCtx, { filename: "ui.js" });
  vm.runInContext('currentDrawerSymbol = "AAPL"; currentDrawerMarket = "us";', uiPollCtx);
  await uiPollCtx.sendAiDrawerMessage();
  const lastMsg = drawerMsgs.children[drawerMsgs.children.length - 1];
  assert(
    lastMsg && lastMsg.textContent.includes("エラー: ドロワーHTTP200エラー"),
    `expected drawer polling error text, got ${lastMsg?.textContent}`,
  );
  asyncChecksCompleted = true;
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
"""
    _run_node(script)


# ---------------------------------------------------------------------------
# ISS-12: Case-sensitive Chrome extension ID check in PowerShell scripts
# ---------------------------------------------------------------------------


def test_native_host_powershell_rejects_uppercase_extension_ids():
    install_ps1 = ROOT / "native_host" / "install_host_windows.ps1"
    validate_ps1 = ROOT / "native_host" / "validate_native_host_windows.ps1"
    install_bytes = install_ps1.read_bytes()
    validate_bytes = validate_ps1.read_bytes()

    assert not install_bytes.startswith(b"\xef\xbb\xbf")
    assert not validate_bytes.startswith(b"\xef\xbb\xbf")
    install_src = install_bytes.decode("utf-8")
    validate_src = validate_bytes.decode("utf-8")
    assert "-cnotmatch '^[a-z0-9]{32}$'" in install_src
    assert "-cnotmatch '^chrome-extension://[a-z0-9]{32}/$'" in validate_src

    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if pwsh is None:
        pytest.skip("PowerShell is required for live Test-ExtensionId execution")

    ps_command = (
        "$ErrorActionPreference = 'Stop'; "
        f"$src = Get-Content -LiteralPath '{install_ps1}' -Raw; "
        "$tokens = $null; $errs = $null; "
        "$ast = [System.Management.Automation.Language.Parser]::ParseInput($src, [ref]$tokens, [ref]$errs); "
        "if ($errs.Count -gt 0) { throw $errs[0].Message }; "
        "$fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Test-ExtensionId' }, $true); "
        ". ([scriptblock]::Create($fn.Extent.Text)); "
        "$good = Test-ExtensionId ('a' * 32); "
        "$upper = Test-ExtensionId (('A' * 16) + ('a' * 16)); "
        "$originUpper = ('chrome-extension://' + ('A' + ('a' * 31)) + '/') -cnotmatch '^chrome-extension://[a-z0-9]{32}/$'; "
        "Write-Output (@{ good=$good; upper=$upper; originUpper=$originUpper } | ConvertTo-Json -Compress)"
    )
    proc = subprocess.run(
        [pwsh, "-NoProfile", "-Command", ps_command],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    json_line = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip().startswith("{")][-1]
    parsed = json.loads(json_line)
    assert parsed["good"] is True
    assert parsed["upper"] is False
    assert parsed["originUpper"] is True
