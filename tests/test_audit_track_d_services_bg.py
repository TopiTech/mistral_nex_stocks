# tests/test_audit_track_d_services_bg.py
"""Regression tests for Track D (services/**, bg/**, app_bg, trend_sources, mistral_compat)."""

from __future__ import annotations

import importlib.util
import json
import queue
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx2
import pandas as pd
import pytest
from pydantic import BaseModel

import app_bg
import bg.common as bg_common
import bg.leader_election as le
import bg.sync.control as sync_control
import bg.sync.fetch as sync_fetch
import bg.sync.process as sync_process
import bg.sync.state as sync_state
import mistral_compat
import trend_sources
from app_state import app_state
from bg import sse_interpolator
from services import (
    ai_portfolio_service,
    ai_service,
    ai_tools,
    embeddings_service,
    news_service,
    openai_service,
)
from services.fallback_provider import (
    AlphaVantageProvider,
    CompositeFallbackProvider,
    Nikkei225JPProvider,
    _CurlCffiSessionProvider,
)
from services.realtime.engine import RealtimeMarketEngine
from services.realtime.scrapers import SBISecuritiesScraper
from services.search import ddgs as ddgs_search
from services.search import tavily as tavily_search
from services.stock.provider import YFinanceProvider
from utils.validators import StockAnalysis

# ---------------------------------------------------------------------------
# CORE-002: SSRF validation and follow_redirects=False in openai_service
# ---------------------------------------------------------------------------


def test_core002_openai_client_rejects_ssrf_url_and_disables_redirects() -> None:
    """_get_openai_client blocks link-local URLs and uses follow_redirects=False."""
    with pytest.raises(ValueError, match="Invalid outbound base URL"):
        openai_service._get_openai_client("http://169.254.169.254/latest/meta-data", "sk-real-key-12345")

    created_clients: list[MagicMock] = []

    def _fake_create_httpx_client(**kwargs: object) -> MagicMock:
        assert kwargs.get("follow_redirects") is False
        client = MagicMock()
        created_clients.append(client)
        return client

    with (
        patch.object(app_state.ai, "get_or_create_openai_client", side_effect=RuntimeError("pool unavailable")),
        patch("services.openai_service._create_httpx_client", side_effect=_fake_create_httpx_client),
        patch(
            "utils.networking.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
        ),
    ):
        client = openai_service._get_openai_client("http://example.test/v1", "sk-real-key-12345")
        try:
            assert len(created_clients) == 1
            assert getattr(client, openai_service._CALL_OWNED_CLIENT_ATTR, False) is True
        finally:
            openai_service._close_call_owned_openai_client(client)
            created_clients[0].close.assert_called_once()


def test_core002_openai_chat_stream_embeddings_block_invalid_base_url() -> None:
    """call_openai_chat, stream_openai_chat, and get_openai_embeddings_batch reject unsafe base_url."""
    with (
        patch("services.openai_service.get_openai_base_url", return_value="http://169.254.169.254/v1"),
        patch("services.openai_service.get_openai_model", return_value="gpt-4o-mini"),
    ):
        chat_res = openai_service.call_openai_chat("sk-real-12345", [{"role": "user", "content": "hi"}], use_cache=False)
        assert chat_res["error"]["status_code"] == 400

        stream_events = list(openai_service.stream_openai_chat("sk-real-12345", [{"role": "user", "content": "hi"}]))
        assert stream_events
        assert stream_events[0]["type"] == "error"

        embs = openai_service.get_openai_embeddings_batch(["hello"], "sk-real-12345")
        assert embs == [None]


# ---------------------------------------------------------------------------
# CORE-005 & BG-001: Sync lock hierarchy, rollback, canonical delegation & wedged recovery
# ---------------------------------------------------------------------------


def test_core005_schedule_sync_restores_sync_forced_on_submit_failure() -> None:
    """schedule_sync_all_stocks_now rolls back both sync_scheduled and sync_forced on submit error."""
    with app_state.market.sync_schedule_lock:
        orig_scheduled = app_state.market.sync_scheduled
        orig_pending = app_state.market.sync_pending
        orig_forced = app_state.market.sync_forced
        app_state.market.sync_scheduled = False
        app_state.market.sync_pending = False
        app_state.market.sync_forced = False
    with app_state.market.is_syncing_lock:
        orig_syncing = app_state.market.is_syncing
        app_state.market.is_syncing = False

    failing_executor = MagicMock()
    failing_executor.submit.side_effect = queue.Full("queue full")
    try:
        with patch.object(app_state.execution, "sync_refresh_executor", failing_executor):
            ok = sync_control.schedule_sync_all_stocks_now(force=True)
        assert ok is False
        with app_state.market.sync_schedule_lock:
            assert app_state.market.sync_scheduled is False
            assert app_state.market.sync_forced is False

        # When sync_forced was already True, a failed submit must keep it True.
        with app_state.market.sync_schedule_lock:
            app_state.market.sync_forced = True
        failing_executor.submit.side_effect = RuntimeError("executor closed")
        with patch.object(app_state.execution, "sync_refresh_executor", failing_executor):
            ok2 = app_bg.schedule_sync_all_stocks_now(force=True)
        assert ok2 is False
        with app_state.market.sync_schedule_lock:
            assert app_state.market.sync_scheduled is False
            assert app_state.market.sync_forced is True
    finally:
        with app_state.market.sync_schedule_lock:
            app_state.market.sync_scheduled = orig_scheduled
            app_state.market.sync_pending = orig_pending
            app_state.market.sync_forced = orig_forced
        with app_state.market.is_syncing_lock:
            app_state.market.is_syncing = orig_syncing


def test_bg001_wedged_lock_rotation_prevents_permanent_lockout() -> None:
    """Timing out on a wedged sync lock rotates the default lock so future syncs recover."""
    orig_lock = sync_state._sync_execution_lock
    orig_app_lock = app_bg._sync_execution_lock
    orig_gen = sync_state._get_sync_generation()
    orig_start = sync_state._get_sync_start_time()
    with app_state.market.is_syncing_lock:
        orig_syncing = app_state.market.is_syncing

    wedged_lock = threading.Lock()
    wedged_lock.acquire()
    sync_state._sync_execution_lock = wedged_lock
    app_bg._sync_execution_lock = wedged_lock
    fetch_calls = 0

    def _fake_fetch(items: list[tuple[str, str, str]], **kwargs: object) -> list[dict[str, object]]:
        nonlocal fetch_calls
        fetch_calls += 1
        return []

    try:
        with app_state.market.is_syncing_lock:
            app_state.market.is_syncing = True
        sync_state._set_sync_start_time(time.time() - (sync_state.SYNC_STALE_TIMEOUT_SEC + 30.0))

        with (
            patch.object(app_bg, "SYNC_STALE_LOCK_WAIT_SEC", 0.01),
            patch.object(app_bg, "_is_sync_leader", True),
            patch("app_bg._warm_payload_cache_from_disk"),
            patch("app_bg._prepare_sync_items", return_value=[]),
            patch("app_bg.fetch_stocks_batch", side_effect=_fake_fetch),
            patch("app_bg._auto_remove_invalid_symbols"),
            patch("app_bg._invalidate_sse_payload_cache"),
            patch("app_bg.announce_current_market_state"),
        ):
            # Call 1 hits the wedged lock and times out -> must NOT deadlock and must rotate lock.
            sync_control.sync_all_stocks_now()
            assert fetch_calls == 0
            assert sync_state._sync_execution_lock is not wedged_lock
            assert app_bg._sync_execution_lock is sync_state._sync_execution_lock
            assert sync_state._get_sync_generation() > orig_gen

            # Call 2 now acquires the rotated lock and completes cleanly even while wedged_lock is still held.
            sync_control.sync_all_stocks_now()
            assert fetch_calls >= 1
            with app_state.market.is_syncing_lock:
                assert app_state.market.is_syncing is False
    finally:
        sync_state._clear_wedged_sync_lock(wedged_lock)
        if wedged_lock.locked():
            wedged_lock.release()
        sync_state._sync_execution_lock = orig_lock
        app_bg._sync_execution_lock = orig_app_lock
        sync_state._set_sync_generation(orig_gen)
        sync_state._set_sync_start_time(orig_start)
        with app_state.market.is_syncing_lock:
            app_state.market.is_syncing = orig_syncing


# ---------------------------------------------------------------------------
# CFG-001: umask critical section serialization in ai_portfolio_service
# ---------------------------------------------------------------------------


def test_cfg001_write_saved_portfolios_holds_restrictive_umask_lock(tmp_path: Path) -> None:
    """_write_saved_ai_portfolios wraps os.umask calls inside restrictive_umask_lock."""
    target_path = tmp_path / "ai_portfolios.enc"
    lock_events: list[str] = []

    class _RecordingLockCtx:
        def __enter__(self) -> None:
            lock_events.append("enter")

        def __exit__(self, *args: object) -> None:
            lock_events.append("exit")

    def _recording_umask(val: int) -> int:
        lock_events.append(f"umask:{oct(val)}")
        return 0o022

    from cryptography.fernet import Fernet

    test_key = Fernet.generate_key().decode("ascii")
    with (
        patch.object(ai_portfolio_service, "AI_PORTFOLIO_STORAGE_FILE", target_path),
        patch("services.ai_portfolio_service._is_windows", return_value=False),
        patch("services.ai_portfolio_service.restrictive_umask_lock", return_value=_RecordingLockCtx()),
        patch("services.ai_portfolio_service.os.umask", side_effect=_recording_umask),
    ):
        ai_portfolio_service._write_saved_ai_portfolios(
            [{"id": "custom-1", "theme": "Tech", "items": [{"symbol": "AAPL", "weight_pct": 100.0}]}],
            master_key=test_key,
        )

    assert target_path.exists()
    assert lock_events == ["enter", "umask:0o77", "umask:0o22", "exit"]


# ---------------------------------------------------------------------------
# BE-001: USD/JPY snapshot & atomic update under usdjpy_rate_lock
# ---------------------------------------------------------------------------


def test_be001_update_indices_data_uses_usdjpy_rate_methods() -> None:
    """_update_indices_data updates USD/JPY via get_usdjpy_rate_snapshot and update_usdjpy_rate."""
    orig_rate, orig_ts = app_state.market.get_usdjpy_rate_snapshot()
    orig_persisted = getattr(app_state.market, "last_usdjpy_persisted_ts", 0.0)
    try:
        app_state.market.update_usdjpy_rate(149.0, ts=100.0)
        with app_state.market.user_stocks_lock:
            app_state.market.last_usdjpy_persisted_ts = 100.0

        with (
            patch.object(app_state.market, "get_usdjpy_rate_snapshot", wraps=app_state.market.get_usdjpy_rate_snapshot) as get_snap,
            patch.object(app_state.market, "update_usdjpy_rate", wraps=app_state.market.update_usdjpy_rate) as upd_rate,
            patch.object(app_state.market, "is_yf_rate_limited", return_value=True),
            patch("app_bg.save_user_stocks") as mock_save,
            patch("app_bg.time.time", return_value=500.0),
        ):
            sync_process._update_indices_data(
                [{"symbol": "USDJPY=X", "price": "153.25", "market_state": "REGULAR"}],
                [],
                [],
            )

        get_snap.assert_called_once()
        upd_rate.assert_called_once_with(153.25, ts=500.0)
        mock_save.assert_called_once()
        cur_rate, cur_ts = app_state.market.get_usdjpy_rate_snapshot()
        assert cur_rate == pytest.approx(153.25)
        assert cur_ts == pytest.approx(500.0)
    finally:
        app_state.market.update_usdjpy_rate(orig_rate, ts=orig_ts)
        with app_state.market.user_stocks_lock:
            app_state.market.last_usdjpy_persisted_ts = orig_persisted


# ---------------------------------------------------------------------------
# SVC-001, SVC-002, SVC-003: ai_service semaphore recursion, thinking stream, empty agent_id
# ---------------------------------------------------------------------------


def test_svc001_call_mistral_chat_fallback_does_not_reacquire_semaphore() -> None:
    """call_mistral_chat HTTP 400 fallback recursion must not deadlock mistral_call_semaphore."""
    err_resp = httpx2.Response(
        400,
        text="reasoning_effort unsupported",
        request=httpx2.Request("POST", "https://api.mistral.ai/v1/chat/completions"),
    )
    sdk_err = mistral_compat.SDKError(
        "reasoning unsupported",
        err_resp,
        body='{"message":"reasoning_effort unsupported"}',
    )
    success_resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        model_dump=lambda: {
            "choices": [{"message": {"content": '{"ok": true}'}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )

    mock_client = MagicMock()
    mock_client.chat.complete.side_effect = [sdk_err, success_resp]

    with (
        patch("services.ai_service.is_openai_provider", return_value=False),
        patch("services.ai_service.is_preview_api_key", return_value=False),
        patch("services.ai_service._resolve_agents_target", return_value=(False, "")),
        patch("services.ai_service._get_mistral_client", return_value=mock_client),
        patch("services.ai_service._get_mistral_model_name", return_value="mistral-medium-latest"),
        patch("services.ai_service.app_state.market.is_circuit_open", return_value=False),
        patch("services.ai_service.app_state.market.get_circuit_state", return_value={"status": "CLOSED"}),
        patch("services.ai_service._acquire_mistral_call_slot_with_reservation", return_value=(0.0, 1.0)),
        patch("services.ai_service._wait_for_rate_limit_slot", return_value="ready"),
        patch("services.ai_service._release_mistral_call_slot"),
        patch("services.ai_service.app_state.market.report_circuit_result") as mock_rec_ok,
        patch.object(app_state.ai, "mistral_call_semaphore", threading.BoundedSemaphore(1)),
    ):
        res = ai_service.call_mistral_chat(
            "real-key-abc123",
            [{"role": "user", "content": "hi"}],
            reasoning_effort="high",
            use_cache=False,
        )

    assert "choices" in res
    assert res["choices"][0]["message"]["content"] == '{"ok": true}'
    assert mock_client.chat.complete.call_count == 2
    mock_rec_ok.assert_called_once_with("mistral", success=True)


def test_svc002_stream_mistral_chat_forwards_include_thinking_and_yields_thinking_events() -> None:
    """stream_mistral_chat preserves include_thinking=True across events and fallback retries."""
    err_resp = httpx2.Response(
        400,
        text="reasoning unsupported",
        request=httpx2.Request("POST", "https://api.mistral.ai/v1/chat/completions"),
    )
    sdk_err = mistral_compat.SDKError(
        "reasoning unsupported",
        err_resp,
        body='{"message":"reasoning unsupported"}',
    )

    think_chunk = SimpleNamespace(
        data=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=[SimpleNamespace(type="thinking", thinking="step 1")],
                    )
                )
            ]
        )
    )
    text_chunk = SimpleNamespace(
        data=SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="final answer"))])
    )

    mock_client = MagicMock()
    mock_client.chat.stream.side_effect = [sdk_err, iter([think_chunk, text_chunk])]

    with (
        patch("services.ai_service.is_openai_provider", return_value=False),
        patch("services.ai_service.is_preview_api_key", return_value=False),
        patch("services.ai_service._resolve_agents_target", return_value=(False, "")),
        patch("services.ai_service._get_mistral_client", return_value=mock_client),
        patch("services.ai_service._get_mistral_model_name", return_value="mistral-medium-latest"),
        patch("services.ai_service.app_state.market.is_circuit_open", return_value=False),
        patch("services.ai_service.app_state.market.get_circuit_state", return_value={"status": "CLOSED"}),
        patch("services.ai_service._acquire_mistral_call_slot_with_reservation", return_value=(0.0, 1.0)),
        patch("services.ai_service._wait_for_rate_limit_slot", return_value="ready"),
        patch("services.ai_service._release_mistral_call_slot"),
        patch("services.ai_service.app_state.market.report_circuit_result"),
        patch.object(app_state.ai, "mistral_stream_semaphore", threading.BoundedSemaphore(1)),
    ):
        events = list(
            ai_service.stream_mistral_chat(
                "real-key-abc123",
                [{"role": "user", "content": "Analyze"}],
                include_thinking=True,
                reasoning_effort="high",
            )
        )

    assert any(ev.get("type") == "thinking" and "step 1" in str(ev.get("text") or "") for ev in events)
    assert any(ev.get("type") == "delta" and "final answer" in str(ev.get("text") or "") for ev in events)


def test_svc003_resolve_agents_target_falls_back_when_agent_id_empty() -> None:
    """Empty agent_id without SDK falls back to standard chat API instead of invoking client.agents."""
    with (
        patch("services.ai_service.is_agents_mode", return_value=True),
        patch("services.ai_service.get_agent_id", return_value=""),
        patch("services.ai_service.HAS_AGENTS_SDK", False),
    ):
        assert ai_service._resolve_agents_target() == (False, "")

    with (
        patch("services.ai_service.is_agents_mode", return_value=True),
        patch("services.ai_service.get_agent_id", return_value="ag_123"),
        patch("services.ai_service.HAS_AGENTS_SDK", False),
    ):
        assert ai_service._resolve_agents_target() == (True, "ag_123")


# ---------------------------------------------------------------------------
# SVC-004, SVC-005, SVC-006: openai_service model gating, tool synthesis, vision guard
# ---------------------------------------------------------------------------


def test_svc004_model_gating_and_narrowed_rf_reject_keywords() -> None:
    """Non-StockAnalysis BaseModels never get StockAnalysis alias/prompt rules, and unrelated 400 keeps format."""

    class OtherSchema(BaseModel):
        headline: str

    assert openai_service._is_stock_analysis_model(StockAnalysis) is True
    assert openai_service._is_stock_analysis_model(OtherSchema) is False

    bad_resp = MagicMock()
    bad_resp.status_code = 400
    bad_resp.text = '{"error":{"message":"invalid date format in user message"}}'
    bad_resp.headers = {}

    captured_payloads: list[dict[str, object]] = []

    def _fake_post(url: str, *, json: dict[str, object], headers: dict[str, str]) -> MagicMock:
        captured_payloads.append(json)
        return bad_resp

    mock_client = MagicMock()
    mock_client.post.side_effect = _fake_post

    with (
        patch("services.openai_service.is_preview_api_key", return_value=False),
        patch("services.openai_service.get_openai_base_url", return_value="http://example.test/v1"),
        patch("services.openai_service.get_openai_model", return_value="gpt-4o-mini"),
        patch("services.openai_service._get_openai_client", return_value=mock_client),
        patch("services.openai_service.app_state.market.is_circuit_open", return_value=False),
        patch("services.openai_service.app_state.market.get_circuit_state", return_value={"status": "CLOSED"}),
        patch("services.openai_service.app_state.market.report_circuit_result"),
        patch("utils.validators._normalize_analysis_aliases") as mock_alias,
        patch(
            "utils.networking.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
        ),
    ):
        res = openai_service.call_openai_chat(
            "sk-real-12345",
            [{"role": "user", "content": "hi"}],
            response_format=OtherSchema,
            use_cache=False,
        )

    assert "error" in res
    assert len(captured_payloads) == 1
    assert "response_format" in captured_payloads[0]
    mock_alias.assert_not_called()


def test_svc005_openai_tool_loop_synthesizes_structured_response_when_no_tools_called() -> None:
    """If tool loop finishes early without tool calls and response_format was requested, enforce schema."""

    class StructuredSummary(BaseModel):
        summary: str

    calls: list[dict[str, object]] = []

    def _fake_call_openai_chat(
        api_key: str,
        messages: list[dict[str, object]],
        **kwargs: object,
    ) -> dict[str, object]:
        calls.append(dict(kwargs))
        if len(calls) == 1:
            return {"choices": [{"message": {"role": "assistant", "content": "draft text"}}]}
        return {"choices": [{"message": {"role": "assistant", "content": '{"summary":"structured"}'}}]}

    with patch("services.openai_service.call_openai_chat", side_effect=_fake_call_openai_chat):
        res = openai_service.call_openai_chat_with_tools(
            "sk-real-12345",
            [{"role": "user", "content": "summarize"}],
            response_format=StructuredSummary,
            max_tool_iterations=3,
        )

    assert len(calls) == 2
    assert calls[0]["response_format"] is None
    assert calls[1]["response_format"] is StructuredSummary
    assert res["choices"][0]["message"]["content"] == '{"summary":"structured"}'


def test_svc006_vision_chart_rejects_empty_or_non_string_image_data() -> None:
    """Both OpenAI and Mistral chart image analyzers fail closed on non-str or blank image_data."""
    for bad in ("", "   "):
        err_oa = openai_service.analyze_chart_image_with_openai("sk-real", bad)
        err_mi = ai_service.analyze_chart_image_with_mistral("sk-real", bad)
        assert isinstance(err_oa, dict) and "error" in err_oa
        assert isinstance(err_mi, dict) and "error" in err_mi

    err_none_oa = openai_service.analyze_chart_image_with_openai("sk-real", "")
    assert "error" in err_none_oa


# ---------------------------------------------------------------------------
# SVC-007: ai_tools NaN/Inf/bool sanitization across fallbacks
# ---------------------------------------------------------------------------


def test_svc007_tool_get_stock_quote_sanitizes_non_finite_values() -> None:
    """_tool_get_stock_quote filters NaN/Inf/bool in realtime, cache, and history fallbacks."""
    rt_engine = SimpleNamespace(
        get_market_snapshot=lambda: {
            "AAPL": {
                "price": float("nan"),
                "change": True,
                "change_percent": float("inf"),
                "volume": False,
            }
        }
    )
    bad_payload = {
        "price": float("nan"),
        "change": "N/A",
        "change_percent": float("-inf"),
        "volume": True,
        "high": float("nan"),
        "low": None,
        "open": "bad",
    }
    hist_df = pd.DataFrame(
        {
            "Close": [100.0, 105.0],
            "Open": [float("nan"), 102.0],
            "High": [106.0, float("inf")],
            "Low": [99.0, True],
            "Volume": [1000.0, True],
        }
    )
    fake_ticker = SimpleNamespace(fast_info={}, history=MagicMock(return_value=hist_df))

    with (
        patch("utils.stock_payload.get_stock_info_cached", return_value={"shortName": "Apple"}),
        patch("services.realtime_engine.realtime_market_engine", rt_engine),
        patch.object(app_state.payload_disk_cache, "get", return_value=bad_payload),
        patch("utils.market_utils.safe_get_ticker", return_value=fake_ticker),
    ):
        quote = ai_tools._tool_get_stock_quote({"symbol": "AAPL", "market": "us"})

    serialized = json.dumps(quote, allow_nan=False)
    assert serialized
    assert quote["price"] == 105.0
    assert quote["open"] == 102.0
    assert quote["high"] is None
    assert quote["low"] is None
    assert quote["volume"] is None

    # Fallback 1 (realtime snapshot) and Fallback 2 (payload cache) also strip NaN/Inf/bool fields.
    rt_ok_engine = SimpleNamespace(
        get_market_snapshot=lambda: {
            "AAPL": {
                "price": 200.0,
                "change": True,
                "change_percent": float("inf"),
                "volume": False,
            }
        }
    )
    with (
        patch("utils.stock_payload.get_stock_info_cached", return_value={}),
        patch("services.realtime_engine.realtime_market_engine", rt_ok_engine),
    ):
        rt_quote = ai_tools._tool_get_stock_quote({"symbol": "AAPL", "market": "us"})
    assert rt_quote["price"] == 200.0
    assert rt_quote["change"] is None
    assert rt_quote["change_pct"] is None
    assert rt_quote["volume"] is None


# ---------------------------------------------------------------------------
# SVC-008: Preview key guard & batch_size clamping in embeddings & openai services
# ---------------------------------------------------------------------------


def test_svc008_preview_key_and_batch_size_guards() -> None:
    """Preview keys return empty/default without network calls; batch_size<=0 never raises ValueError."""
    from credential_manager import PREVIEW_API_KEY

    with patch("services.ai_service._acquire_mistral_call_slot", side_effect=AssertionError("must not call")):
        assert embeddings_service.get_mistral_embeddings_batch(["a", "b"], PREVIEW_API_KEY) == [None, None]
        assert openai_service.get_openai_embeddings_batch(["a", "b"], PREVIEW_API_KEY) == [None, None]
        items = [{"title": "Headline 1"}]
        assert embeddings_service.rank_news_by_semantic_relevance(items, ["AI"], PREVIEW_API_KEY) == items

    with embeddings_service._EMBEDDINGS_CACHE_LOCK:
        embeddings_service._EMBEDDINGS_CACHE.clear()

    mock_client = MagicMock()
    mock_client.embeddings.create.return_value = SimpleNamespace(
        data=[SimpleNamespace(embedding=[0.1, 0.2])]
    )
    with (
        patch("credential_manager.is_openai_provider", return_value=False),
        patch("services.embeddings_service._get_client", return_value=mock_client),
        patch("services.ai_service._acquire_mistral_call_slot", return_value=0.0),
        patch("services.ai_service._wait_for_rate_limit_slot", return_value="ready"),
        patch("services.ai_service._release_mistral_call_slot"),
        patch.object(app_state.market, "is_circuit_open", return_value=False),
        patch.object(app_state.market, "get_circuit_state", return_value={"status": "CLOSED"}),
        patch.object(app_state.market, "report_circuit_result"),
    ):
        res = embeddings_service.get_mistral_embeddings_batch(["unique_text_xyz"], "real-key-abc", batch_size=0)
    assert res == [[0.1, 0.2]]

    oa_resp = SimpleNamespace(
        status_code=200,
        json=lambda: {"data": [{"index": 0, "embedding": [0.5, 0.6]}]},
        text="",
    )
    mock_oa_client = MagicMock()
    mock_oa_client.post.return_value = oa_resp
    with (
        patch("services.openai_service.get_openai_base_url", return_value="http://example.test/v1"),
        patch("services.openai_service._get_openai_client", return_value=mock_oa_client),
        patch.object(app_state.market, "is_circuit_open", return_value=False),
        patch(
            "utils.networking.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
        ),
    ):
        oa_res = openai_service.get_openai_embeddings_batch(["unique_oa_xyz"], "sk-real-abc", batch_size=-5)
    assert oa_res == [[0.5, 0.6]]


# ---------------------------------------------------------------------------
# SVC-009: force_refresh writes fresh bundle to LLM cache in news_service
# ---------------------------------------------------------------------------


def test_svc009_news_force_refresh_updates_llm_bundle_cache() -> None:
    """get_synchronized_market_news(force_refresh=True) refreshes cache for subsequent normal calls."""
    import hashlib

    llm_hash = hashlib.sha256(b"US context|JP context|- Nvidia rally").hexdigest()
    cached_store: dict[str, object] = {
        f"news_bundle_llm_{llm_hash}": {
            "us": "Stale US summary line.",
            "jp": "Stale JP summary line.",
            "trends": "Stale trends summary line.",
        }
    }

    def _fake_get_cached(key: str, fn: object, duration: int = 0, valid_func: object = None) -> object:
        existing = cached_store.get(key)
        if existing is not None and (not callable(valid_func) or valid_func(existing)):
            return existing
        if callable(fn):
            val = fn()
            cached_store[key] = val
            return val
        return existing

    def _fake_set_cached(key: str, value: object, duration: int = 0) -> None:
        cached_store[key] = value

    fresh_json = json.dumps(
        {
            "us": "Fresh US market headline rallied broadly today.\nSecond US detail line updated now.",
            "jp": "Fresh JP market headline rallied broadly today.\nSecond JP detail line updated now.",
            "trends": "Fresh semiconductor trend surged broadly today.\nSecond trend detail line updated now.",
        }
    )

    def _fake_ctx(cache_key: str, fn: object, ttl: int = 0, neg_ttl: int = 0, force: bool = False) -> str:
        return "US context" if "_us_" in cache_key else "JP context"

    svc = news_service.NewsService()
    with (
        patch("services.news_service._determine_search_strategy", return_value="default"),
        patch("services.news_service.get_cached_context_with_negative_cache", side_effect=_fake_ctx),
        patch("services.news_service.collect_market_trending_titles", side_effect=[["Nvidia rally"], [], ["Nvidia rally"], []]),
        patch("services.news_service.get_cached", side_effect=_fake_get_cached),
        patch("services.news_service._set_cached_value", side_effect=_fake_set_cached),
        patch(
            "services.news_service.call_mistral_chat",
            return_value={"choices": [{"message": {"content": fresh_json}}]},
        ) as mock_llm,
    ):
        forced = svc.get_synchronized_market_news("real-key", "", "", force_refresh=True)
        subsequent = svc.get_synchronized_market_news("real-key", "", "", force_refresh=False)

    assert "Fresh US market headline rallied broadly today." in forced["us"]["content"]
    assert "Fresh US market headline rallied broadly today." in subsequent["us"]["content"]
    mock_llm.assert_called_once()


# ---------------------------------------------------------------------------
# SVC-010: ai_portfolio_service weights, preset preserve, inflight key, curly braces
# ---------------------------------------------------------------------------


def test_svc010_zero_weight_preserved_and_braces_in_theme_supported() -> None:
    """Renormalization applies diff only to positive holdings; themes with '{' / '}' work."""
    sanitized = ai_portfolio_service.sanitize_ai_portfolio(
        {
            "id": "custom-1",
            "theme": "Tech {2026}",
            "items": [
                {"symbol": "AAPL", "weight_pct": 0.0},
                {"symbol": "MSFT", "weight_pct": 33.3},
                {"symbol": "NVDA", "weight_pct": 33.3},
                {"symbol": "GOOGL", "weight_pct": 33.3},
            ],
        }
    )
    weights = {it["symbol"]: it["weight_pct"] for it in sanitized["items"]}
    assert weights["AAPL"] == 0.0
    assert round(sum(weights.values()), 1) == 100.0

    with (
        patch("services.ai_portfolio_service._find_saved_ai_portfolio", return_value=None),
        patch("services.ai_portfolio_service.collect_symbol_research_context", return_value=""),
        patch("services.ai_portfolio_service.get_mistral_api_key", return_value="mns_preview_123"),
        patch("services.ai_portfolio_service.load_saved_ai_portfolios", return_value=[]),
        patch("services.ai_portfolio_service.save_custom_ai_portfolio"),
    ):
        # Preview fallback works cleanly even when theme contains curly braces.
        result = ai_portfolio_service.generate_ai_portfolio_by_theme("AI {Next} & Robotics}")
    assert result["theme"] == "AI {Next} & Robotics}"


# ---------------------------------------------------------------------------
# SVC-011: Fallback providers epoch close, invalid price/volume rejection, Nikkei225JP block, NDX
# ---------------------------------------------------------------------------


def test_svc011_fallback_providers_lifecycle_and_validation() -> None:
    """_CurlCffiSessionProvider rotates epoch on close, validates positive prices and 429 propagation."""

    class _DummySession:
        def __init__(self, impersonate: str = "") -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    dummy_requests = SimpleNamespace(Session=_DummySession)

    class _TestCurlProvider(_CurlCffiSessionProvider):
        def get_latest_quote(self, symbol: str) -> dict[str, object] | None:
            return None

    provider = _TestCurlProvider()
    provider.requests = dummy_requests
    sess1, ok1 = provider._get_client()
    assert ok1 is True and sess1 is not None
    provider.close()
    assert sess1.closed is True
    sess2, ok2 = provider._get_client()
    assert ok2 is True and sess2 is not None and sess2 is not sess1
    provider.close()

    # AlphaVantage rejects non-positive price and clamps negative volume to 0.
    av = AlphaVantageProvider()
    zero_price_resp = SimpleNamespace(
        status_code=200,
        raise_for_status=lambda: None,
        json=lambda: {"Global Quote": {"05. price": "0", "06. volume": "10"}},
    )
    neg_vol_resp = SimpleNamespace(
        status_code=200,
        raise_for_status=lambda: None,
        json=lambda: {"Global Quote": {"05. price": "100.0", "06. volume": "-5"}},
    )
    with patch("services.fallback_provider.get_alphavantage_api_key", return_value="demo-av-key"):
        with patch("requests.get", return_value=zero_price_resp):
            assert av.get_latest_quote("AAPL") is None
        with patch("requests.get", return_value=neg_vol_resp):
            av_quote = av.get_latest_quote("AAPL")
            assert av_quote is not None and av_quote["regularMarketVolume"] == 0

    # Nikkei225JP 429 triggers _mark_yahoo_block(is_yahoo_host=False).
    nk = Nikkei225JPProvider()
    rate_resp = SimpleNamespace(status_code=429, text="Too Many Requests", headers={"Retry-After": "10"})
    fake_client = SimpleNamespace(get=MagicMock(return_value=rate_resp))
    with patch("services.fallback_provider._mark_yahoo_block") as mock_mark:
        cache_out = nk._refresh_index_cache(fake_client, True)
    assert cache_out == {}
    mock_mark.assert_called_once()

    # CompositeFallbackProvider includes NASDAQ100 in Nikkei225JP index dispatch.
    comp = CompositeFallbackProvider()
    with (
        patch.object(comp.alpha_vantage, "get_latest_quote", return_value=None),
        patch.object(
            comp.nikkei225jp,
            "get_latest_quote",
            return_value={"regularMarketPrice": 20000.0},
        ) as mock_nk,
    ):
        out = comp.get_latest_quote("NASDAQ100")
    assert out is not None and out["regularMarketPrice"] == 20000.0
    assert out["source"] == "nikkei225jp"
    mock_nk.assert_called_once_with("NASDAQ100")
    comp.close()


# ---------------------------------------------------------------------------
# SVC-012: ddgs & tavily bounds, timeout kwarg, circuit breaker classification
# ---------------------------------------------------------------------------


def test_svc012_ddgs_and_tavily_bounds_timeout_and_circuit() -> None:
    """ddgs rejects non-positive limits and skips disabled paths; tavily enforces timeout & circuit."""
    assert ddgs_search._collect_ddgs_items(["nvidia"], "us-en", "w", 3, 2, limit=0) == []
    assert ddgs_search._collect_ddgs_items(["nvidia"], "us-en", "w", 3, 2, query_limit=0) == []
    assert ddgs_search._collect_ddgs_items(["nvidia"], "us-en", "w", 0, 0, limit=5) == []

    with (
        patch("services.search.ddgs.ddgs_news_search") as mock_news,
        patch("services.search.ddgs.ddgs_text_search", return_value=[{"title": "T", "href": "https://ex.test", "body": "B"}]) as mock_text,
    ):
        res = ddgs_search._collect_ddgs_items(["nvidia"], "us-en", "w", 0, 2, limit=5)
    mock_news.assert_not_called()
    mock_text.assert_called_once()
    assert len(res) == 1

    # Tavily: timeout=10.0 passed and 401 does NOT trip circuit breaker.
    mock_tavily_client = MagicMock()
    mock_tavily_client.search.return_value = {"results": [{"title": "Hit", "url": "https://ex.test", "content": "ok"}]}
    with (
        patch("services.search.tavily._get_tavily_client", return_value=mock_tavily_client),
        patch("services.search.tavily._is_tavily_circuit_open", return_value=False),
        patch("services.search.tavily._report_tavily_circuit") as mock_report,
    ):
        hits = tavily_search.tavily_search("tvly-real", "AAPL", max_results=3)
    assert len(hits) == 1
    assert mock_tavily_client.search.call_args.kwargs.get("timeout") == 10.0
    mock_report.assert_called_once_with(True)

    err_401 = RuntimeError("401 unauthorized")
    assert tavily_search._is_tavily_circuit_failure(err_401) is False
    assert tavily_search._is_tavily_fatal_batch_error(err_401) is True
    assert tavily_search._is_tavily_circuit_failure(TimeoutError("timed out")) is True


# ---------------------------------------------------------------------------
# SVC-013: trend_sources curly braces & distinct Google Trends URLs
# ---------------------------------------------------------------------------


def test_svc013_trend_sources_brace_safety_and_distinct_keyword_urls() -> None:
    """QueryTemplates handles braces safely and Google Trends keyword items preserve distinct URLs."""
    queries = trend_sources.QueryTemplates.get_symbol_queries("SYM{1}", "Corp {A}", "us")
    assert queries
    assert any("SYM{1}" in q for q in queries)

    mock_client = MagicMock()
    with (
        patch("trend_sources._google_trends_client", return_value=mock_client),
        patch("trend_sources._trend_queries_for_keyword", return_value=["NVIDIA", "AMD"]),
    ):
        assert trend_sources.collect_google_trends_keyword_items("semiconductors", limit=0) == []
        items = trend_sources.collect_google_trends_keyword_items("semiconductors", limit=5)

    assert len(items) == 2
    assert len({it["url"] for it in items}) == 2
    deduped = trend_sources.dedupe_items(items)
    assert len(deduped) == 2


# ---------------------------------------------------------------------------
# SVC-014: YFinanceProvider fast_info prewarm merge & get_news guard
# ---------------------------------------------------------------------------


def test_svc014_stock_provider_fastinfo_merge_upgrade_and_get_news_guards() -> None:
    """History pre-warm merges without dropping metadata; get_fast_info upgrades history-only cache."""
    provider = YFinanceProvider()
    sym = "UNITTEST_SVC014"
    cache_key = f"fastinfo_{sym}"
    with app_state.market.yfinance_short_cache_lock:
        had_orig = cache_key in app_state.market.yfinance_short_cache
        orig_cached = app_state.market.yfinance_short_cache.get(cache_key)
    try:
        with app_state.market.yfinance_short_cache_lock:
            app_state.market.yfinance_short_cache[cache_key] = {
                "previousClose": 100.0,
                "marketCap": 1_000_000_000,
                "quoteType": "EQUITY",
            }
        df = pd.DataFrame(
            {
                "Close": [101.0, 102.0],
                "Open": [100.5, 101.5],
                "High": [103.0, 103.0],
                "Low": [100.0, 101.0],
                "Volume": [1000, 1200],
            }
        )
        with patch.object(provider, "get_ticker", return_value=None):
            provider._pre_warm_caches_from_history({sym: df}, app_state.market)
        with app_state.market.yfinance_short_cache_lock:
            merged = dict(app_state.market.yfinance_short_cache[cache_key])
        assert merged["marketCap"] == 1_000_000_000
        assert merged["quoteType"] == "EQUITY"

        # History-only cache (no marketCap/quoteType) must NOT skip network when not rate-limited.
        with app_state.market.yfinance_short_cache_lock:
            app_state.market.yfinance_short_cache[cache_key] = {
                "previousClose": 98.0,
                "regularMarketPreviousClose": 98.0,
            }
        fake_ticker = SimpleNamespace(
            fast_info={"last_price": 150.0, "previous_close": 148.0, "market_cap": 500_000, "quote_type": "EQUITY"},
            history_metadata={"currency": "USD", "exchangeName": "NMS"},
            info={},
        )
        with (
            patch.object(provider, "get_ticker", return_value=fake_ticker),
            patch.object(app_state.market, "is_yf_rate_limited", return_value=False),
        ):
            fi = provider.get_fast_info(sym)
        assert fi.get("previousClose") == 148.0
        assert fi.get("marketCap") == 500_000
        assert fi.get("quoteType") == "EQUITY"

        # get_news handles limit<=0 and content=None cleanly.
        assert provider.get_news(sym, limit=0) == []
        news_ticker = SimpleNamespace(
            get_news=lambda: [
                {"content": None},
                {"content": {"title": "Headline", "canonicalUrl": None, "clickThroughUrl": {}}},
            ]
        )
        with patch.object(provider, "get_ticker", return_value=news_ticker):
            articles = provider.get_news(sym, limit=5)
        assert len(articles) == 1
        assert articles[0]["title"] == "Headline"
        assert articles[0]["link"] == ""
    finally:
        with app_state.market.yfinance_short_cache_lock:
            if had_orig:
                app_state.market.yfinance_short_cache[cache_key] = orig_cached
            else:
                app_state.market.yfinance_short_cache.pop(cache_key, None)


# ---------------------------------------------------------------------------
# BG-002: 429 streak preservation, fetch_stock error handling, full-length batch results
# ---------------------------------------------------------------------------


def test_bg002_error_handling_and_full_length_batch_results() -> None:
    """Unrelated errors preserve 429 streak; fetch_stock handles 429; rate-limited batch keeps input length."""
    with app_state.market.yfinance_lock:
        orig_streak = app_state.market.yfinance_429_streak
        orig_until = app_state.market.yfinance_rate_limit_until
        orig_limited = app_state.market.is_yfinance_rate_limited
        app_state.market.yfinance_429_streak = 3
    try:
        sync_fetch._handle_yfinance_error(ValueError("unrelated parse glitch"), "AAPL")
        with app_state.market.yfinance_lock:
            assert app_state.market.yfinance_429_streak == 3

        # fetch_stock invokes _handle_yfinance_error on get_history failure.
        with (
            patch("bg.sync_worker.acquire_yfinance_slot", return_value=True),
            patch.object(app_state.stock_provider, "get_history", side_effect=ValueError("429 Too Many Requests")),
            patch.object(app_state.payload_disk_cache, "get", return_value=None),
            patch("bg.sync.fetch._handle_yfinance_error") as mock_err,
        ):
            assert sync_fetch.fetch_stock("AAPL", "Apple", "us") is None
        mock_err.assert_called_once()

        # Rate-limited batch of 15 items slices down for yfinance but still returns 15 positional entries.
        items = [(f"SYM{i}", f"Name {i}", "us") for i in range(15)]
        downloaded = pd.DataFrame({("SYM0", "Close"): [100.0, 101.0]})
        downloaded.columns = pd.MultiIndex.from_tuples(downloaded.columns)
        with (
            patch("app_bg.fetch_stocks_batch", sync_fetch.fetch_stocks_batch),
            patch("utils.disk_cache.is_disk_cache_degraded", return_value=False),
            patch.object(app_state.market, "is_yf_rate_limited", return_value=True),
            patch("bg.sync_worker.acquire_yfinance_slot", return_value=True),
            patch.object(app_state.stock_provider, "download_batch", return_value=downloaded),
            patch("app_bg.extract_batch_history", return_value=pd.DataFrame({"Close": [100.0, 101.0]})),
            patch("app_bg.build_stock_payload", return_value={"symbol": "SYM0", "price": 101.0}),
        ):
            out = sync_fetch.fetch_stocks_batch(items)
        assert len(out) == 15
        assert out[0] == {"symbol": "SYM0", "price": 101.0}
        assert all(entry is None for entry in out[10:])
        with app_state.market.yfinance_lock:
            assert app_state.market.yfinance_429_streak == 0
    finally:
        with app_state.market.yfinance_lock:
            app_state.market.yfinance_429_streak = orig_streak
            app_state.market.yfinance_rate_limit_until = orig_until
            app_state.market.is_yfinance_rate_limited = orig_limited


# ---------------------------------------------------------------------------
# BG-003: Dead realtime worker visibility + missing_worker_names watchdog
# ---------------------------------------------------------------------------


def test_bg003_tv_client_unexpected_exit_retains_dead_thread_and_watchdog_restarts() -> None:
    """Unexpected _run_ws exit retains dead thread so worker_threads() and watchdog recover it."""
    engine = RealtimeMarketEngine()
    engine.tv_client.running = True
    engine.tv_client._worker_epoch = 1

    t = threading.Thread(target=lambda: engine.tv_client._run_ws(1), daemon=True, name="TradingViewWSWorker")
    engine.tv_client.thread = t
    # Force immediate loop exit while keeping running=True and matching epoch -> unexpected exit!
    with patch.object(engine.tv_client, "_is_worker_current", return_value=False):
        t.start()
        t.join(timeout=2.0)

    assert not t.is_alive()
    assert engine.tv_client.thread is t
    assert t in engine.worker_threads()

    with patch.object(engine, "restart") as mock_restart:
        restarted = sync_control._watchdog_restart_dead_realtime_engine(engine)
    assert "TradingViewWSWorker" in restarted
    mock_restart.assert_called_once()

    # Also verify missing_worker_names catches None threads while engine.running=True.
    engine.running = True
    engine.tv_client.thread = None
    assert "TradingViewWSWorker" in engine.missing_worker_names()
    engine.running = False
    engine._bg_executor.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------------------
# BG-004: SSE indices diffing & interpolate loop index-only publish
# ---------------------------------------------------------------------------


def test_bg004_sse_diff_and_announce_publish_when_only_indices_change() -> None:
    """_raw_announce_current_market_state emits a diff frame when only indices changed."""
    # Backward-compatible 2-arg call keeps only stock market keys.
    assert set(bg_common._build_sse_diff({"us": []}, {})) == {"us", "jp", "idx"}

    orig_stocks = bg_common._sse_prev_stocks
    orig_indices = bg_common._sse_prev_indices
    orig_counter = bg_common._sse_full_snapshot_counter
    orig_gen = bg_common._sse_payload_generation
    orig_cached_gen = bg_common._sse_payload_cached_generation
    with app_state.cache.sse_data_lock:
        orig_cur_stocks = dict(app_state.market.current_stocks_cache)
        orig_cur_indices = dict(app_state.market.current_indices_cache)

    announced: list[tuple[int, str]] = []
    fake_announcer = SimpleNamespace(announce=lambda msg: announced.append(msg))
    try:
        bg_common._sse_prev_stocks = {"us": {}, "jp": {}, "idx": {}}
        bg_common._sse_prev_indices = {"N225": {"price": "38,000.00"}}
        bg_common._sse_full_snapshot_counter = 0
        app_bg._sse_full_snapshot_counter = 0
        bg_common._sse_payload_generation = 10
        bg_common._sse_payload_cached_generation = 9
        app_bg._sse_prev_stocks = bg_common._sse_prev_stocks
        app_bg._sse_payload_generation = 10
        app_bg._sse_payload_cached_generation = 9
        with app_state.cache.sse_data_lock:
            app_state.market.current_stocks_cache = {"us": [], "jp": [], "idx": []}
            app_state.market.current_indices_cache = {"N225": {"price": "38,250.00"}}

        with (
            patch.object(app_state, "sse_announcer", fake_announcer),
            patch("bg.common.is_market_open", return_value=False),
        ):
            bg_common._raw_announce_current_market_state()

        assert len(announced) == 1
        seq, frame = announced[0]
        assert isinstance(seq, int)
        assert frame.startswith("data: ")
        payload = json.loads(frame.removeprefix("data: ").strip())
        assert payload["stream_event"] == "diff"
        assert set(payload["stocks"].keys()) == {"us", "jp", "idx"}
        assert payload["indices"]["N225"]["price"] == "38,250.00"
    finally:
        bg_common._sse_prev_stocks = orig_stocks
        bg_common._sse_prev_indices = orig_indices
        bg_common._sse_full_snapshot_counter = orig_counter
        bg_common._sse_payload_generation = orig_gen
        bg_common._sse_payload_cached_generation = orig_cached_gen
        bg_common._set_app_bg_attr("_sse_prev_stocks", orig_stocks)
        bg_common._set_app_bg_attr("_sse_prev_indices", orig_indices)
        bg_common._set_app_bg_attr("_sse_full_snapshot_counter", orig_counter)
        bg_common._set_app_bg_attr("_sse_payload_generation", orig_gen)
        bg_common._set_app_bg_attr("_sse_payload_cached_generation", orig_cached_gen)
        with app_state.cache.sse_data_lock:
            app_state.market.current_stocks_cache = orig_cur_stocks
            app_state.market.current_indices_cache = orig_cur_indices


# ---------------------------------------------------------------------------
# BG-005: Leader lock mutex serialization under concurrent acquire & release
# ---------------------------------------------------------------------------


def test_bg005_leader_lock_acquire_and_release_concurrent_safety(tmp_path: Path) -> None:
    """Concurrent acquire and release of the leader lock file must not raise closed-file ValueError."""
    orig_dir = le.APP_DATA_DIR
    orig_app_dir = app_bg.APP_DATA_DIR
    errors: list[BaseException] = []
    try:
        le._release_leader_lock()
        le.APP_DATA_DIR = tmp_path
        app_bg.APP_DATA_DIR = tmp_path

        def _worker(i: int) -> None:
            try:
                for _ in range(15):
                    le._try_acquire_leader_lock()
                    if i % 2 == 0:
                        le._release_leader_lock()
            except BaseException as exc:
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(_worker, range(4)))
        assert errors == []
    finally:
        le._release_leader_lock()
        le.APP_DATA_DIR = orig_dir
        app_bg.APP_DATA_DIR = orig_app_dir


# ---------------------------------------------------------------------------
# BG-006: mistral_compat fallback & SBI change_percent calculation
# ---------------------------------------------------------------------------


def test_bg006_mistral_compat_and_sbi_change_percent() -> None:
    """mistral_compat fallback classes match expected surface; SBI fetch_quote computes change_percent."""
    mod_name = "_mistral_compat_fallback_isolated"
    compat_path = getattr(mistral_compat, "__file__", "")
    assert compat_path
    spec = importlib.util.spec_from_file_location(mod_name, compat_path)
    assert spec is not None and spec.loader is not None
    fallback_mod = importlib.util.module_from_spec(spec)
    blocked = {
        "mistralai": None,
        "mistralai.client": None,
        "mistralai.client.errors": None,
        "mistralai.client.utils": None,
        "mistralai.errors": None,
        "mistralai.agents": None,
    }
    try:
        with patch.dict(sys.modules, blocked):
            spec.loader.exec_module(fallback_mod)
        fallback_client = fallback_mod.Mistral("dummy-key")
        with pytest.raises(fallback_mod.SDKError):
            fallback_client.chat.complete()
        with pytest.raises(fallback_mod.SDKError):
            fallback_client.agents.complete()
        assert isinstance(fallback_mod.ToolMessage("ok", "call_1", name="fn"), dict)
    finally:
        sys.modules.pop(mod_name, None)

    scraper = SBISecuritiesScraper()
    html = "現在値 2,200 前日比 +200"
    with (
        patch.object(scraper, "_fetch_page", return_value=html),
        patch.object(scraper, "_is_in_cooldown", return_value=False),
        patch.object(scraper, "_record_fetch_success"),
    ):
        quote = scraper.fetch_quote("7203.T")
    assert quote is not None
    assert quote["price"] == 2200.0
    assert quote["change"] == 200.0
    assert quote["change_percent"] == pytest.approx(10.0)


def test_svc010_preset_rebalance_failure_preserves_existing_saved() -> None:
    """When rebalancing an existing saved preset fails, existing_saved is returned with warning."""
    preset_id, preset_cfg = next(iter(ai_portfolio_service.DEFAULT_PRESET_CONFIGS.items()))
    saved_preset = {
        "id": preset_id,
        "title": preset_cfg["title"],
        "theme": preset_cfg["theme"],
        "description": preset_cfg["description"],
        "items": [{"symbol": "AAPL", "weight_pct": 100.0}],
        "generated_by": "ai",
    }
    with (
        patch("services.ai_portfolio_service._find_saved_ai_portfolio", return_value=saved_preset),
        patch("services.ai_portfolio_service.collect_symbol_research_context", return_value=""),
        patch("services.ai_portfolio_service.get_mistral_api_key", return_value="real-key-abc"),
        patch("credential_manager.is_openai_provider", return_value=False),
        patch("credential_manager.is_preview_api_key", return_value=False),
        patch("services.ai_portfolio_service.call_mistral_chat", side_effect=RuntimeError("llm outage")),
        patch("services.ai_portfolio_service._persist_generated_ai_portfolio") as mock_persist,
    ):
        res = ai_portfolio_service.generate_ai_portfolio_by_theme(preset_id, force_rebalance=True)

    assert "fallback_warning" in res
    assert res["items"][0]["symbol"] == "AAPL"
    mock_persist.assert_not_called()


def test_bg004_interpolate_loop_publishes_index_only_updates() -> None:
    """bg_interpolate_loop fluctuates and announces even when target_stocks_cache is empty."""
    with app_state.cache.sse_data_lock:
        orig_targets = dict(app_state.market.target_stocks_cache)
        orig_cur_stocks = dict(app_state.market.current_stocks_cache)
        orig_cur_indices = dict(app_state.market.current_indices_cache)

    orig_wait = app_state.execution.shutdown_event.wait
    announced_count = 0

    def _fake_announce() -> None:
        nonlocal announced_count
        announced_count += 1
        app_state.execution.shutdown_event.set()

    fake_announcer = SimpleNamespace(listener_count=lambda: 1)
    try:
        app_state.execution.shutdown_event.clear()
        with app_state.cache.sse_data_lock:
            app_state.market.target_stocks_cache = {"us": [], "jp": [], "idx": []}
            app_state.market.current_stocks_cache = {"us": [], "jp": [], "idx": []}
            app_state.market.current_indices_cache = {"N225": {"price": "38,000.00"}}

        wait_calls = 0

        def _fast_wait(timeout: float | None = None) -> bool:
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls >= 3:
                app_state.execution.shutdown_event.set()
            return app_state.execution.shutdown_event.is_set()

        with (
            patch.object(app_state, "sse_announcer_mode1", fake_announcer),
            patch.object(app_state.execution.shutdown_event, "wait", side_effect=_fast_wait),
            patch("bg.sse_interpolator._is_market_open", return_value=True),
            patch("bg.sse_interpolator._fluctuate_indices"),
            patch("bg.sse_interpolator._invalidate_sse_payload_cache"),
            patch("bg.sse_interpolator.announce_current_market_state", side_effect=_fake_announce),
        ):
            sse_interpolator.bg_interpolate_loop()

        assert announced_count == 1
    finally:
        app_state.execution.shutdown_event.clear()
        with app_state.cache.sse_data_lock:
            app_state.market.target_stocks_cache = orig_targets
            app_state.market.current_stocks_cache = orig_cur_stocks
            app_state.market.current_indices_cache = orig_cur_indices
        assert callable(orig_wait)


def test_bg006_sbi_change_percent_edge_cases() -> None:
    """SBI scraper handles negative delta and missing change gracefully."""
    scraper = SBISecuritiesScraper()
    with (
        patch.object(scraper, "_fetch_page", return_value="現在値 950 前日比 ▼50"),
        patch.object(scraper, "_is_in_cooldown", return_value=False),
        patch.object(scraper, "_record_fetch_success"),
    ):
        neg_quote = scraper.fetch_quote("7203.T")
    assert neg_quote is not None
    assert neg_quote["change"] == -50.0
    assert neg_quote["change_percent"] == pytest.approx(-5.0)

    with (
        patch.object(scraper, "_fetch_page", return_value="現在値 950"),
        patch.object(scraper, "_is_in_cooldown", return_value=False),
        patch.object(scraper, "_record_fetch_success"),
    ):
        no_change_quote = scraper.fetch_quote("7203.T")
    assert no_change_quote is not None
    assert no_change_quote["change"] == 0.0
    assert no_change_quote["change_percent"] == 0.0
