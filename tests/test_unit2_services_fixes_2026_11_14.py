"""Comprehensive regression tests for Unit 2 service/provider/realtime fixes (2026-11-14)."""

from __future__ import annotations

import json
import logging
from contextlib import nullcontext
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Self
from unittest.mock import MagicMock, patch

import httpx
import pandas as pd
import pytest

from app_state import app_state
from services import ai_service, openai_service
from services.realtime.engine import RealtimeMarketEngine
from services.realtime.scrapers import YahooJPRealtimeScraper
from services.search import tavily as tavily_search_mod
from services.stock.errors import YFRateLimitError
from services.stock.provider import YFinanceProvider

pytestmark = pytest.mark.unit


def _reset_circuit(name: str) -> None:
    app_state.market.report_circuit_result(name, success=True)
    app_state.market.release_circuit_probe(name)


def _trip_circuit(name: str, threshold: int = 3) -> None:
    for _ in range(threshold):
        app_state.market.report_circuit_result(name, success=False, threshold=threshold, open_sec=60)


def _open_half_open_circuit(name: str, threshold: int = 3) -> None:
    _reset_circuit(name)
    app_state.market.report_circuit_result(name, success=False, threshold=1, open_sec=-1.0)
    assert app_state.market.is_circuit_open(name) is False
    assert app_state.market.get_circuit_state(name)["status"] == "HALF_OPEN"


class _OpenCircuitOnAcquireSem:
    """Context manager mimicking a semaphore whose circuit opens while waiting."""

    def __init__(self, circuit_name: str, threshold: int = 3) -> None:
        self._circuit_name = circuit_name
        self._threshold = threshold

    def __enter__(self) -> Self:
        _trip_circuit(self._circuit_name, threshold=self._threshold)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


def test_u2_01_stream_mistral_rechecks_circuit_after_semaphore() -> None:
    _reset_circuit("mistral")
    try:
        sem = _OpenCircuitOnAcquireSem("mistral")
        with (
            patch.object(app_state.ai, "mistral_stream_semaphore", sem),
            patch("services.ai_service._get_mistral_client", return_value=MagicMock()),
            patch("services.ai_service._wait_for_rate_limit_slot", return_value="ready"),
            patch("services.ai_service._acquire_mistral_call_slot_with_reservation", return_value=(0.0, 0.0)),
            patch("services.ai_service.is_preview_api_key", return_value=False),
            patch("services.ai_service.is_openai_provider", return_value=False),
        ):
            events = list(
                ai_service.stream_mistral_chat("real-key-1234", [{"role": "user", "content": "hi"}])
            )
        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert events[0]["status_code"] == 503
    finally:
        _reset_circuit("mistral")


def test_u2_01_openai_call_and_stream_recheck_circuit_after_semaphore() -> None:
    _reset_circuit("openai_compatible")
    try:
        sem_call = _OpenCircuitOnAcquireSem("openai_compatible")
        with (
            patch.object(app_state.ai, "openai_call_semaphore", sem_call),
            patch("services.openai_service._get_openai_client") as mock_get_client,
            patch("services.openai_service.validate_outbound_base_url", return_value=(True, "")),
            patch("services.openai_service.get_openai_base_url", return_value="http://127.0.0.1:11434/v1"),
            patch("services.openai_service.is_preview_api_key", return_value=False),
        ):
            res = openai_service.call_openai_chat(
                "sk-real-1234", [{"role": "user", "content": "hi"}], use_cache=False
            )
        assert res["error"]["status_code"] == 503
        mock_get_client.assert_not_called()

        _reset_circuit("openai_compatible")
        sem_stream = _OpenCircuitOnAcquireSem("openai_compatible")
        with (
            patch.object(app_state.ai, "openai_stream_semaphore", sem_stream),
            patch("services.openai_service._get_openai_client") as mock_get_stream_client,
            patch("services.openai_service.validate_outbound_base_url", return_value=(True, "")),
            patch("services.openai_service.get_openai_base_url", return_value="http://127.0.0.1:11434/v1"),
            patch("services.openai_service.is_preview_api_key", return_value=False),
        ):
            stream_events = list(
                openai_service.stream_openai_chat(
                    "sk-real-1234", [{"role": "user", "content": "hi"}]
                )
            )
        assert len(stream_events) == 1
        assert stream_events[0]["type"] == "error"
        assert stream_events[0]["status_code"] == 503
        mock_get_stream_client.assert_not_called()
    finally:
        _reset_circuit("openai_compatible")


def test_u2_01_agent_sdk_error_marks_429_preserves_status_and_sanitizes_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _AgentRateLimitError(Exception):
        def __init__(self, msg: str, status_code: int) -> None:
            super().__init__(msg)
            self.status_code = status_code
            self.response = SimpleNamespace(status_code=status_code, headers={"Retry-After": "12"})

    class _FakeAgent:
        async def run(self, _prompt: object, **_kwargs: object) -> None:
            raise _AgentRateLimitError(
                "upstream failed Authorization: Bearer supersecret-token-xyz", 429
            )

    _reset_circuit("mistral")
    try:
        with (
            caplog.at_level(logging.ERROR),
            patch.object(app_state.ai, "mistral_stream_semaphore", nullcontext()),
            patch.object(app_state.ai, "mark_mistral_429") as mock_mark_429,
            patch("services.ai_service.is_preview_api_key", return_value=False),
            patch("services.ai_service.is_openai_provider", return_value=False),
            patch("services.ai_service.is_agents_mode", return_value=True),
            patch("services.ai_service.get_agent_id", return_value="ag_123"),
            patch("services.ai_service.HAS_AGENTS_SDK", True),
            # The optional agents SDK is not installed on every supported
            # Python (the dependency is 3.12+), so simulate its module too;
            # patching HAS_AGENTS_SDK alone still took the API-agents path.
            patch("services.ai_service.mistral_agents", SimpleNamespace(TextDelta=None)),
            patch("services.ai_service.load_sdk_agent", return_value=_FakeAgent()),
            patch("services.ai_service._get_mistral_client", return_value=MagicMock()),
            patch("services.ai_service._wait_for_rate_limit_slot", return_value="ready"),
            patch("services.ai_service._acquire_mistral_call_slot_with_reservation", return_value=(0.0, 0.0)),
        ):
            events = list(
                ai_service.stream_mistral_chat("real-key-1234", [{"role": "user", "content": "hi"}])
            )
        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert events[0]["status_code"] == 429
        assert "supersecret-token-xyz" not in str(events[0]["message"])
        mock_mark_429.assert_called_once_with(12.0)
        assert "supersecret-token-xyz" not in caplog.text
    finally:
        _reset_circuit("mistral")


def test_u2_01_stream_mistral_fallback_closes_sdk_stream_before_retrying() -> None:
    _reset_circuit("mistral")
    close_calls = 0

    class _FailingStreamCM:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        def __iter__(self) -> _FailingStreamCM:
            return self

        def __next__(self) -> object:
            req = httpx.Request("POST", "https://api.mistral.ai/v1/chat/completions")
            resp = httpx.Response(
                400,
                request=req,
                json={"message": "unsupported reasoning parameter"},
            )
            raise httpx.HTTPStatusError("400 Bad Request", request=req, response=resp)

        def close(self) -> None:
            nonlocal close_calls
            close_calls += 1

    class _SuccessStreamCM:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        def __iter__(self):
            yield SimpleNamespace(
                data=SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="fallback-ok"))],
                    usage=None,
                )
            )

        def close(self) -> None:
            return None

    client = MagicMock()
    client.chat.stream.side_effect = [_FailingStreamCM(), _SuccessStreamCM()]

    try:
        with (
            patch.object(app_state.ai, "mistral_stream_semaphore", nullcontext()),
            patch("services.ai_service.is_preview_api_key", return_value=False),
            patch("services.ai_service.is_openai_provider", return_value=False),
            patch("services.ai_service.is_agents_mode", return_value=False),
            patch("services.ai_service._get_mistral_model_name", return_value="magistral-medium-2506"),
            patch("services.ai_service._supports_reasoning_effort", return_value=True),
            patch("services.ai_service._resolve_reasoning_effort", side_effect=["high", None]),
            patch("services.ai_service._get_mistral_client", return_value=client),
            patch("services.ai_service._wait_for_rate_limit_slot", return_value="ready"),
            patch("services.ai_service._acquire_mistral_call_slot_with_reservation", return_value=(0.0, 0.0)),
        ):
            events = list(
                ai_service.stream_mistral_chat("real-key-1234", [{"role": "user", "content": "hi"}])
            )
        assert close_calls >= 1
        assert any(ev.get("type") == "done" and ev.get("text") == "fallback-ok" for ev in events)
    finally:
        _reset_circuit("mistral")


def test_u2_02_dict_tool_arguments_size_limit_and_invalid_openai_tool_short_circuit() -> None:
    huge_dict = {"blob": "x" * 20_000}
    mistral_turn = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "get_stock_quote", "arguments": huge_dict},
                        }
                    ],
                }
            }
        ]
    }
    with (
        patch("services.ai_service.call_mistral_chat", return_value=mistral_turn),
        patch("services.ai_service.is_openai_provider", return_value=False),
        patch("services.ai_tools.execute_mistral_tool_call") as mock_mistral_exec,
    ):
        res = ai_service.call_mistral_chat_with_tools("real-key", [{"role": "user", "content": "q"}])
    assert res["error"]["status_code"] == 502
    mock_mistral_exec.assert_not_called()

    openai_huge_turn = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_huge",
                            "type": "function",
                            "function": {"name": "get_stock_quote", "arguments": huge_dict},
                        }
                    ],
                }
            }
        ]
    }
    with (
        patch("services.openai_service.call_openai_chat", return_value=openai_huge_turn),
        patch("services.openai_service.execute_mistral_tool_call") as mock_oa_exec,
    ):
        res_oa_huge = openai_service.call_openai_chat_with_tools(
            "sk-real", [{"role": "user", "content": "q"}]
        )
    assert res_oa_huge["error"]["status_code"] == 502
    mock_oa_exec.assert_not_called()

    captured_turns: list[list[dict[str, object]]] = []

    def _stub_oa_chat(_api_key: str, msgs: list[dict[str, object]], **_kw: object) -> dict[str, object]:
        captured_turns.append(list(msgs))
        if len(captured_turns) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_bad_args",
                                    "type": "function",
                                    "function": {"name": "get_stock_quote", "arguments": [1, 2, 3]},
                                },
                                {
                                    "id": "call_bad_name",
                                    "type": "function",
                                    "function": {"name": "   ", "arguments": "{}"},
                                },
                            ],
                        }
                    }
                ]
            }
        return {"choices": [{"message": {"role": "assistant", "content": "done"}}]}

    with (
        patch("services.openai_service.call_openai_chat", side_effect=_stub_oa_chat),
        patch("services.openai_service.execute_mistral_tool_call") as mock_oa_bad_exec,
    ):
        res_oa_bad = openai_service.call_openai_chat_with_tools(
            "sk-real", [{"role": "user", "content": "q"}]
        )
    assert res_oa_bad["choices"][0]["message"]["content"] == "done"
    mock_oa_bad_exec.assert_not_called()
    tool_msgs = [m for m in captured_turns[1] if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    assert all(
        json.loads(str(m["content"]))["error"] == "ツールの実行に失敗しました" for m in tool_msgs
    )


def test_u2_03_openai_embeddings_half_open_and_log_sanitization(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _reset_circuit("openai_compatible")
    try:
        _open_half_open_circuit("openai_compatible")

        resp_500 = SimpleNamespace(
            status_code=500,
            text="upstream failure Authorization: Bearer secret-embed-tok",
        )
        client_500 = MagicMock()
        client_500.post.return_value = resp_500

        with (
            caplog.at_level(logging.DEBUG),
            patch("services.openai_service.is_preview_api_key", return_value=False),
            patch("services.openai_service.validate_outbound_base_url", return_value=(True, "")),
            patch("services.openai_service.get_openai_base_url", return_value="http://127.0.0.1:11434/v1"),
            patch("services.openai_service._get_openai_client", return_value=client_500),
            patch("services.openai_service._close_call_owned_openai_client"),
        ):
            res = openai_service.get_openai_embeddings_batch(["hello"], "sk-real-123")
        assert res == [None]
        assert "secret-embed-tok" not in caplog.text
        assert app_state.market.get_circuit_state("openai_compatible")["status"] == "OPEN"

        _open_half_open_circuit("openai_compatible")
        resp_200 = SimpleNamespace(
            status_code=200,
            json=lambda: {"data": [{"index": 0, "embedding": [0.1, 0.2]}]},
        )
        client_200 = MagicMock()
        client_200.post.return_value = resp_200
        with (
            patch("services.openai_service.is_preview_api_key", return_value=False),
            patch("services.openai_service.validate_outbound_base_url", return_value=(True, "")),
            patch("services.openai_service.get_openai_base_url", return_value="http://127.0.0.1:11434/v1"),
            patch("services.openai_service._get_openai_client", return_value=client_200),
            patch("services.openai_service._close_call_owned_openai_client"),
        ):
            ok = openai_service.get_openai_embeddings_batch(["hello"], "sk-real-123")
        assert ok == [[0.1, 0.2]]
        assert app_state.market.get_circuit_state("openai_compatible")["status"] == "CLOSED"
    finally:
        _reset_circuit("openai_compatible")


def test_u2_03_tavily_half_open_probe_released_on_4xx() -> None:
    _reset_circuit("tavily")
    try:
        _open_half_open_circuit("tavily")

        class _ClientBadReq(Exception):
            status_code = 400

        with (
            patch("services.search.tavily._get_tavily_client", return_value=MagicMock()),
            patch("services.search.tavily._tavily_client_search", side_effect=_ClientBadReq("bad")),
        ):
            rows = tavily_search_mod.tavily_search("nvda", api_key="tvly-test-key")
        assert rows == []
        state = app_state.market.get_circuit_state("tavily")
        assert state["status"] == "HALF_OPEN"
        assert state["probing"] is False
    finally:
        _reset_circuit("tavily")


def test_u2_04_yfinance_rate_limit_caught_and_calendar_targets_sanitized() -> None:
    provider = YFinanceProvider()
    fake_df = pd.DataFrame({"Close": [123.4]}, index=pd.to_datetime(["2026-11-10"]))

    class _FakeTicker:
        def history(self, **_kw: object) -> pd.DataFrame:
            raise YFRateLimitError()

        def get_analyst_price_targets(self) -> dict[str, object]:
            return {"current": float("nan"), "mean": 150.25, "high": float("inf")}

        def get_calendar(self) -> dict[str, object]:
            return {
                "Earnings Date": [date(2026, 11, 20), pd.NaT],
                "Ex-Dividend Date": datetime(2026, 12, 1, 9, 0, tzinfo=UTC),
                "Dividend Yield": float("nan"),
                "EmptyInf": [float("-inf"), 2.5],
            }

    fake_ticker = _FakeTicker()
    with (
        patch.object(provider, "get_ticker", return_value=fake_ticker),
        patch.object(provider, "_load_history_disk_fallback", return_value=fake_df),
        patch("services.stock.retry.time.sleep"),
        patch("services.stock_provider._handle_yf_rate_limit") as mock_rate_limit,
    ):
        hist = provider._fetch_single_history("AAPL", "1mo", app_state.market)
    assert not hist.empty
    assert float(hist["Close"].iloc[0]) == pytest.approx(123.4)
    assert any(
        c.kwargs.get("context") == "single_history symbol=AAPL"
        for c in mock_rate_limit.call_args_list
    )

    with patch.object(provider, "get_ticker", return_value=fake_ticker):
        targets = provider.get_analyst_targets("AAPL")
        calendar = provider.get_calendar("AAPL")

    assert targets == {"mean": 150.25}
    json.dumps(targets, allow_nan=False)
    assert calendar == {
        "Earnings Date": ["2026-11-20", None],
        "Ex-Dividend Date": "2026-12-01T09:00:00+00:00",
        "Dividend Yield": None,
        "EmptyInf": [None, 2.5],
    }
    json.dumps(calendar, allow_nan=False)


def test_u2_05_realtime_engine_resolves_yf_volume_outside_store_lock() -> None:
    engine = RealtimeMarketEngine()
    observed_locks: list[bool] = []

    def _probe_yf_vol(_symbol: str) -> int:
        acquired = engine.store_lock.acquire(blocking=False)
        observed_locks.append(acquired)
        if acquired:
            engine.store_lock.release()
        return 999

    reg = engine._activate_registration("AAPL", "us")
    with patch("services.realtime.engine._get_yfinance_volume", side_effect=_probe_yf_vol):
        engine._handle_producer_update(
            {"symbol": "AAPL", "market": "us", "price": 210.0, "source": "tradingview"},
            registration=reg,
        )
        engine._handle_pts_update(
            {"symbol": "7203", "market": "jp", "price": 2500.0, "source": "yahoojp"},
            registration=None,
        )

    assert observed_locks == [True, True]
    assert engine.market_store["AAPL"]["volume"] == 999
    assert engine.pts_store["7203"]["volume"] == 999


def test_u2_05_scraper_retains_dot_t_dispatch_prices_and_uses_interruptible_sleep() -> None:
    scraper = YahooJPRealtimeScraper(symbols=["7203", "6758"])
    assert scraper._dispatch_price_changed({"symbol": "7203.T", "price": 2500.0}) is True
    scraper._last_dispatch_price["9999.T"] = 100.0
    scraper._last_dispatch_price["8888.T"] = 200.0
    scraper._last_dispatch_price["1111.T"] = 300.0
    scraper._last_dispatch_price["2222.T"] = 400.0
    scraper._last_dispatch_price["3333.T"] = 500.0

    sleep_calls: list[float] = []

    def _fake_interruptible_sleep(check: object, seconds: float) -> None:
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            scraper.running = False

    scraper.running = True

    class _DoneFuture:
        def result(self) -> None:
            return None

    class _ImmediateExec:
        def submit(self, fn: object, *args: object, **kwargs: object) -> _DoneFuture:
            return _DoneFuture()

    def _immediate_done_executor(*_args: object, **_kwargs: object) -> _ImmediateExec:
        return _ImmediateExec()

    with (
        patch("services.realtime.scrapers._is_scraper_blocked", return_value=False),
        patch("services.realtime.scrapers._is_yf_rate_limited", return_value=False),
        patch("utils.market_utils.is_market_open", return_value=True),
        patch("services.realtime.scrapers._interruptible_sleep", side_effect=_fake_interruptible_sleep),
        patch("services.realtime.scrapers.DaemonThreadPoolExecutor", side_effect=_immediate_done_executor),
        patch.object(scraper, "_fetch_regular_with_fallback", return_value=None),
    ):
        scraper._worker_loop()

    assert len(sleep_calls) >= 1
    assert "7203.T" in scraper._last_dispatch_price
    assert "9999.T" not in scraper._last_dispatch_price
