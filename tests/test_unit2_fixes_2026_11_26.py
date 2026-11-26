"""Regression tests for Unit 2 (services layer) fixes."""

from __future__ import annotations

import json
import threading
import time
import weakref
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest


def test_u2_1_extract_mistral_wait_seconds_rejects_non_finite_and_clamps() -> None:
    from services.ai_service import _extract_mistral_wait_seconds

    assert _extract_mistral_wait_seconds({"Retry-After": "inf"}) == 0.0
    assert _extract_mistral_wait_seconds({"Retry-After": "Infinity"}) == 0.0
    assert _extract_mistral_wait_seconds({"Retry-After": "nan"}) == 0.0
    assert _extract_mistral_wait_seconds({"Retry-After": "infms"}) == 0.0
    assert _extract_mistral_wait_seconds({"Retry-After": "1e309"}) == 0.0
    assert _extract_mistral_wait_seconds({"X-RateLimit-Reset": "inf"}) == 0.0
    assert _extract_mistral_wait_seconds({"X-RateLimit-Reset": "nan"}) == 0.0

    assert _extract_mistral_wait_seconds({"Retry-After": "3.5"}) == pytest.approx(3.5)
    assert _extract_mistral_wait_seconds({"Retry-After": "2500ms"}) == pytest.approx(2.5)
    assert _extract_mistral_wait_seconds({"Retry-After": "999999"}) == 3600.0


def test_u2_2_stream_mistral_chat_preserves_visible_delta_matching_thinking() -> None:
    from app_state import app_state
    from services import ai_service

    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "content": "株価",
                        "reasoning_content": "株価",
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "content": "Analysis",
                        "reasoning_content": "Ana",
                    }
                }
            ]
        },
    ]

    mock_client = MagicMock()
    mock_client.chat.stream.return_value = iter(chunks)

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
            )
        )

    deltas = [ev["text"] for ev in events if ev.get("type") == "delta"]
    thinkings = [ev["text"] for ev in events if ev.get("type") == "thinking"]
    done_ev = next(ev for ev in events if ev.get("type") == "done")
    assert deltas == ["株価", "Analysis"]
    assert thinkings == ["株価", "Ana"]
    assert done_ev["text"] == "株価Analysis"


def test_u2_3_openai_tool_loop_serializes_dict_arguments_to_json_string() -> None:
    from services import openai_service

    captured_messages: list[list[dict[str, Any]]] = []

    def _fake_call_openai_chat(api_key: str, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        captured_messages.append(json.loads(json.dumps(messages)))
        if len(captured_messages) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_dict_1",
                                    "type": "function",
                                    "function": {
                                        "name": "get_stock_quote",
                                        "arguments": {"symbol": "AAPL", "market": "us"},
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        return {
            "choices": [{"message": {"role": "assistant", "content": "AAPL is $200"}}],
            "usage": {"total_tokens": 10},
        }

    with (
        patch.object(openai_service, "call_openai_chat", side_effect=_fake_call_openai_chat),
        patch("services.ai_tools.execute_mistral_tool_call", return_value='{"price": 200.0}'),
    ):
        resp = openai_service.call_openai_chat_with_tools(
            "sk-real",
            [{"role": "user", "content": "Quote AAPL"}],
        )

    assert resp["choices"][0]["message"]["content"] == "AAPL is $200"
    assert len(captured_messages) == 2
    assistant_msg = next(m for m in captured_messages[1] if m.get("role") == "assistant")
    tc_args = assistant_msg["tool_calls"][0]["function"]["arguments"]
    assert isinstance(tc_args, str)
    assert json.loads(tc_args) == {"symbol": "AAPL", "market": "us"}


def test_u2_4_openai_verify_and_embeddings_handle_null_data_list() -> None:
    from app_state import app_state
    from services import openai_service

    fake_verify_resp = SimpleNamespace(status_code=200, json=lambda: {"data": None})

    class _FakeHttpxClient:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, *_args: Any, **_kwargs: Any) -> Any:
            return fake_verify_resp

    with (
        patch.object(openai_service, "validate_outbound_base_url", return_value=(True, "")),
        patch.object(openai_service, "get_openai_model", return_value="gpt-4o-mini"),
        patch.object(openai_service, "_create_httpx_client", return_value=_FakeHttpxClient()),
    ):
        verify_res = openai_service.verify_openai_connection(
            api_key="sk-real", base_url="https://api.example.com/v1"
        )
    assert verify_res["ok"] is True
    assert verify_res["model_count"] == 0

    fake_emb_resp = SimpleNamespace(status_code=200, json=lambda: {"data": None, "usage": {}})
    fake_emb_client = SimpleNamespace(
        post=lambda *_a, **_kw: fake_emb_resp,
        close=lambda: None,
    )
    app_state.market.report_circuit_result("openai_compatible", success=True)
    with (
        patch.object(openai_service, "get_openai_base_url", return_value="https://api.example.com/v1"),
        patch.object(openai_service, "validate_outbound_base_url", return_value=(True, "")),
        patch.object(openai_service, "_get_openai_client", return_value=fake_emb_client),
    ):
        embs = openai_service.get_openai_embeddings_batch(
            ["hello"],
            api_key="sk-real",
            model="text-embedding-3-small",
        )
    assert embs == [None]


def test_u2_5_concurrent_force_rebalance_propagates_fallback_warning_and_timeout(
    tmp_path: Path,
) -> None:
    from services import ai_portfolio_service as aps

    storage_file = tmp_path / "ai_portfolios.json"
    existing_portfolio = {
        "id": "custom-u2-5",
        "theme": "Semiconductors",
        "title": "Existing Portfolio",
        "description": "Existing",
        "risk_level": "mid",
        "expected_return": "10%",
        "commentary": "Original",
        "items": [
            {
                "symbol": "NVDA",
                "market": "us",
                "weight_pct": 100.0,
                "target_price": 150.0,
                "rationale": "Leader",
                "risk_level": "mid",
            }
        ],
    }

    leader_entered = threading.Event()
    release_leader = threading.Event()
    waiter_results: list[dict[str, Any]] = []
    waiter_errors: list[Exception] = []

    def _slow_research(**_kwargs: Any) -> str:
        leader_entered.set()
        assert release_leader.wait(timeout=5.0)
        return ""

    aps._AI_GEN_INFLIGHT.clear()
    try:
        with (
            patch.object(aps, "AI_PORTFOLIO_STORAGE_FILE", storage_file),
            patch.object(aps, "_find_saved_ai_portfolio", return_value=existing_portfolio),
            patch.object(aps, "collect_symbol_research_context", side_effect=_slow_research),
            patch.object(aps, "get_mistral_api_key", return_value=""),
            patch.object(aps, "get_tavily_api_key", return_value=""),
            patch.object(aps, "get_langsearch_api_key", return_value=""),
        ):
            t_leader = threading.Thread(
                target=lambda: aps.generate_ai_portfolio_by_theme(
                    "custom-u2-5", force_rebalance=True
                )
            )
            t_leader.start()
            assert leader_entered.wait(timeout=5.0)

            def _run_waiter() -> None:
                try:
                    waiter_results.append(
                        aps.generate_ai_portfolio_by_theme("custom-u2-5", force_rebalance=True)
                    )
                except Exception as exc:
                    waiter_errors.append(exc)

            t_waiter = threading.Thread(target=_run_waiter)
            t_waiter.start()
            time.sleep(0.05)
            release_leader.set()
            t_leader.join(timeout=5.0)
            t_waiter.join(timeout=5.0)

        assert not waiter_errors
        assert len(waiter_results) == 1
        assert "fallback_warning" in waiter_results[0]

        # Timeout case: waiter must NOT return stale pre-rebalance existing_portfolio
        aps._AI_GEN_INFLIGHT["custom-u2-5"] = threading.Event()
        with (
            patch.object(aps, "_find_saved_ai_portfolio", return_value=existing_portfolio),
            patch.object(aps, "_AI_GEN_HANDOFF_TIMEOUT_SEC", 0.02),
            patch.object(aps, "_AI_GEN_HANDOFF_MAX_ATTEMPTS", 1),
        ):
            with pytest.raises(aps.PortfolioStorageError) as exc_info:
                aps.generate_ai_portfolio_by_theme("custom-u2-5", force_rebalance=True)
            assert exc_info.value.status_code == 503
    finally:
        aps._AI_GEN_INFLIGHT.clear()


def test_u2_6_ai_tools_pass_timeout_and_record_yf_rate_limit() -> None:
    from app_state import app_state
    from constants import YFINANCE_TIMEOUT_SINGLE
    from services import ai_tools
    from services.stock.errors import YFRateLimitError

    observed_timeouts: list[Any] = []

    class _RateLimitTicker:
        fast_info = None

        def history(self, **kwargs: Any) -> pd.DataFrame:
            observed_timeouts.append(kwargs.get("timeout"))
            raise YFRateLimitError("429 Too Many Requests")

    with (
        patch("services.ai_tools._lookup_cached_quote", return_value=None)
        if hasattr(ai_tools, "_lookup_cached_quote")
        else patch("utils.stock_payload.get_stock_info_cached", return_value={}),
        patch("services.realtime_engine.realtime_market_engine.get_market_snapshot", return_value={}),
        patch.object(app_state.payload_disk_cache, "get", return_value=None),
        patch("utils.market_utils.safe_get_ticker", return_value=_RateLimitTicker()),
        patch.object(app_state.market, "is_yf_rate_limited", return_value=False),
        patch("services.stock.errors._handle_yf_rate_limit") as mock_rl,
    ):
        quote_res = ai_tools._tool_get_stock_quote({"symbol": "AAPL", "market": "us"})
        assert observed_timeouts == [YFINANCE_TIMEOUT_SINGLE]
        assert mock_rl.call_count == 1
        assert quote_res["status"] == "データ取得不可または市場外"

        tech_res = ai_tools._tool_calculate_technical_levels({"symbol": "AAPL", "market": "us"})
        assert mock_rl.call_count == 2
        assert "error" in tech_res


def test_u2_7_and_u2_8_scraper_bounded_stop_no_duplicate_start_and_session_epoch() -> None:
    from services.realtime import scrapers as scr_mod

    # U2-8: in-flight session created before close() finishes must not be stored
    tl = threading.local()
    lock = threading.Lock()
    all_sessions: weakref.WeakSet[Any] = weakref.WeakSet()
    epoch_ref = [0]
    closed_orphans: list[str] = []

    class _OrphanSession:
        def close(self) -> None:
            closed_orphans.append("closed")

    def _create_during_close() -> _OrphanSession:
        epoch_ref[0] += 1
        return _OrphanSession()

    with patch.object(scr_mod, "_create_cffi_session", side_effect=_create_during_close):
        sess = scr_mod._get_or_create_weak_session(tl, lock, all_sessions, epoch_ref)

    assert isinstance(sess, _OrphanSession)
    assert closed_orphans == ["closed"]
    assert getattr(tl, "session", None) is None
    assert len(all_sessions) == 0

    # U2-7: _worker_loop exits promptly when stopped while a future is still pending,
    # and start() refuses to spawn a duplicate thread if old thread is alive.
    scraper = scr_mod.YahooJPRealtimeScraper(symbols=["7203.T", "6758.T"])
    stuck_future: Future[Any] = Future()
    fake_executor = MagicMock()
    fake_executor.submit.return_value = stuck_future
    scraper._executor = fake_executor

    with (
        patch.object(scraper, "_is_startup_ready", return_value=True),
        patch.object(scr_mod, "_is_scraper_blocked", return_value=False),
        patch.object(scr_mod, "_is_yf_rate_limited", return_value=False),
        patch("constants.SCRAPER_REQUEST_STAGGER_SEC", 0.0),
    ):
        scraper.start()
        deadline = time.monotonic() + 2.0
        while fake_executor.submit.call_count == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert fake_executor.submit.call_count >= 1
        t0 = time.monotonic()
        scraper.stop()
        elapsed = time.monotonic() - t0
        assert elapsed < 1.5
        assert scraper.thread is None

    alive_blocker = threading.Event()

    def _hold() -> None:
        alive_blocker.wait(timeout=2.0)

    stuck_thread = threading.Thread(target=_hold, daemon=True)
    stuck_thread.start()
    try:
        scraper.running = False
        scraper.thread = stuck_thread
        with pytest.raises(RuntimeError, match="still running"):
            scraper.start()
    finally:
        alive_blocker.set()
        stuck_thread.join(timeout=2.0)
        scraper.thread = None


def test_u2_9_pts_worker_publishes_user_jp_symbol_not_in_scraper_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app_state import app_state
    from services.realtime import engine as engine_mod

    rt = engine_mod.RealtimeMarketEngine()
    with app_state.market.user_stocks_lock:
        orig_user_jp = dict(app_state.market.user_jp)
        app_state.market.user_jp = {"9984.T": {"name": "SoftBank"}}

    payload = {
        "symbol": "9984.T",
        "price": 9100.0,
        "change": 50.0,
        "change_percent": 0.55,
        "volume": 1000,
        "source": "yahoojp_pts",
        "pts": True,
        "pts_trading": True,
        "pts_time": "18:30",
        "updated_at": time.time(),
    }
    published = threading.Event()
    received: list[dict[str, Any]] = []

    def _capture(p: dict[str, Any], **_kw: Any) -> None:
        received.append(p)
        published.set()

    try:
        assert "9984.T" not in rt.yahoojp_scraper.symbols
        monkeypatch.setattr(rt, "_handle_pts_update", _capture)
        rt.running = True
        with (
            patch.object(rt.yahoojp_scraper, "_is_startup_ready", return_value=True),
            patch.object(engine_mod, "is_pts_session", return_value=True),
            patch.object(engine_mod, "_is_scraper_blocked", return_value=False),
            patch.object(engine_mod, "_is_yf_rate_limited", return_value=False),
            patch.object(rt, "_fetch_pts_with_fallback", return_value=payload),
            patch.object(engine_mod, "_interruptible_sleep", return_value=None),
        ):
            t = threading.Thread(target=rt._pts_worker_loop, daemon=True)
            t.start()
            assert published.wait(timeout=2.0)
            rt.running = False
            t.join(timeout=2.0)

        assert len(received) >= 1
        assert received[0]["symbol"] == "9984.T"
    finally:
        rt.stop()
        with app_state.market.user_stocks_lock:
            app_state.market.user_jp.clear()
            app_state.market.user_jp.update(orig_user_jp)


def test_u2_10_df_to_records_normalizes_numpy_non_finite_and_timestamps() -> None:
    from services.stock.provider import YFinanceProvider

    provider = YFinanceProvider()
    df = pd.DataFrame(
        {
            "Date Reported": [pd.Timestamp("2026-11-26T00:00:00Z"), pd.NaT],
            "Float32Val": pd.Series([np.float32("nan"), np.float32(12.5)], dtype="float32"),
            "Float16Val": pd.Series([np.float16("inf"), np.float16(1.5)], dtype="float16"),
        }
    )
    records = provider._df_to_records(df)
    assert len(records) == 2
    assert records[0]["Date Reported"] == "2026-11-26T00:00:00+00:00"
    assert records[0]["Float32Val"] is None
    assert records[0]["Float16Val"] is None
    assert records[1]["Date Reported"] is None
    serialized = json.dumps(records, allow_nan=False)
    assert "2026-11-26T00:00:00+00:00" in serialized
