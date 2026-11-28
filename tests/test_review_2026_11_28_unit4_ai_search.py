"""Regression tests for Unit 4 fixes (AI services, OpenAI-compat, portfolio, tools, search, trends)."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

import mistral_compat
import trend_sources as ts
from app_state import app_state
from services import ai_portfolio_service, ai_service, ai_tools, openai_service
from services.search import ddgs as ddgs_mod
from services.search import langsearch as ls_mod
from services.search import tavily as tavily_mod


class _FakeStreamCM:
    def __init__(self, stream):
        self._stream = stream

    def __enter__(self):
        return self._stream

    def __exit__(self, *args):
        return False


def _reset_circuits(*names: str) -> None:
    for name in names:
        with app_state.market.circuit_lock:
            app_state.market.circuit_states.pop(name, None)


def test_openai_stream_inband_error_and_empty_stream_d4_01(monkeypatch) -> None:
    _reset_circuits("openai_compatible")
    monkeypatch.setattr(openai_service, "get_openai_base_url", lambda: "https://api.example.com/v1")
    monkeypatch.setattr(openai_service, "validate_outbound_base_url", lambda _u: (True, ""))
    monkeypatch.setattr(openai_service, "get_openai_model", lambda: "gpt-test")
    monkeypatch.setattr(openai_service, "_close_call_owned_openai_client", lambda _c: None)

    inband_stream = SimpleNamespace(
        status_code=200,
        headers={},
        iter_lines=lambda: [
            b'data: {"choices":[{"delta":{"reasoning":{"text":"step"}}}]}',
            b'data: {"error":{"message":"upstream exploded","code":502}}',
        ],
    )
    fake_client = SimpleNamespace(stream=lambda *a, **kw: _FakeStreamCM(inband_stream))
    monkeypatch.setattr(openai_service, "_get_openai_client", lambda *_a, **_kw: fake_client)

    events = list(openai_service.stream_openai_chat("sk-test", [{"role": "user", "content": "hi"}], include_thinking=True))
    assert events[0] == {"type": "thinking", "text": "step"}
    assert events[1]["type"] == "error"
    assert events[1]["status_code"] == 502
    assert all(ev["type"] != "done" for ev in events)

    # Empty stream without JSON chunks yields 502 error instead of done
    _reset_circuits("openai_compatible")
    empty_stream = SimpleNamespace(
        status_code=200,
        headers={},
        iter_lines=lambda: [b"data: [DONE]"],
    )
    fake_client2 = SimpleNamespace(stream=lambda *a, **kw: _FakeStreamCM(empty_stream))
    monkeypatch.setattr(openai_service, "_get_openai_client", lambda *_a, **_kw: fake_client2)
    events2 = list(openai_service.stream_openai_chat("sk-test", [{"role": "user", "content": "hi"}]))
    assert len(events2) == 1
    assert events2[0]["type"] == "error"
    assert events2[0]["status_code"] == 502


def test_mistral_think_chunk_extraction_and_filtering_d4_02() -> None:
    text_inner = SimpleNamespace(type="text", text="chain-of-thought")
    think_chunk = SimpleNamespace(type="thinking", thinking=[text_inner])
    visible_chunk = SimpleNamespace(type="text", text="final-answer")
    sneaky_thinking_dict = {"type": "thinking", "text": "leak-me"}
    chunk = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=[think_chunk, sneaky_thinking_dict, visible_chunk])
            )
        ]
    )

    assert ai_service._extract_thinking_delta(chunk) == "chain-of-thoughtleak-me"
    assert ai_service._extract_stream_delta(chunk, include_thinking=False) == "final-answer"
    assert ai_service._extract_stream_delta(chunk, include_thinking=True) == (
        "chain-of-thoughtleak-mefinal-answer"
    )


def test_builtin_timeout_error_status_and_circuit_d4_03() -> None:
    exc = TimeoutError("socket timed out")
    assert ai_service._exception_status_code(exc) == 504
    assert ai_service._should_trip_mistral_circuit(exc, 504, None) is True
    assert "タイムアウト" in ai_service._public_mistral_error_message(exc, 504)


def test_sanitize_ai_portfolio_mismatch_and_retry_same_symbol_d4_04() -> None:
    cleaned = ai_portfolio_service.sanitize_ai_portfolio(
        {
            "title": "Test",
            "items": [
                {"symbol": "7203.T", "market": "us", "weight_pct": 50},
                {"symbol": "AAPL", "market": "jp", "weight_pct": 50},
                {"symbol": "NVDA", "market": "us", "weight_pct": -5},
                {"symbol": "NVDA", "market": "us", "weight_pct": 100},
            ],
        }
    )
    assert [it["symbol"] for it in cleaned["items"]] == ["NVDA"]
    assert cleaned["items"][0]["weight_pct"] == 100.0


def test_ai_portfolio_wait_uses_captured_event_d4_05(monkeypatch) -> None:
    event = threading.Event()
    calls: list[str] = []

    def _boom(*_a, **_kw):
        calls.append("second_lookup")
        raise AssertionError("should wait on inflight_event directly")

    monkeypatch.setattr(ai_portfolio_service, "_wait_ai_generation_slot", _boom)
    monkeypatch.setattr(ai_portfolio_service, "_acquire_ai_generation_slot", lambda _k: False)
    monkeypatch.setattr(ai_portfolio_service, "_get_ai_generation_event", lambda _k: event)
    ai_portfolio_service._AI_GEN_EVENT_OUTCOMES[event] = {
        "result": {"id": "p1", "items": []},
        "error": None,
    }
    event.set()
    monkeypatch.setattr(ai_portfolio_service, "_find_saved_ai_portfolio", lambda *_a: None)
    monkeypatch.setattr(ai_portfolio_service, "get_mistral_api_key", lambda: "mns-preview-test")

    out = ai_portfolio_service.generate_ai_portfolio_by_theme("AI", force_rebalance=True, api_key="mns-preview-test")
    assert out == {"id": "p1", "items": []}
    assert calls == []


def test_tavily_atomic_enter_circuit_call_d4_06(monkeypatch) -> None:
    _reset_circuits("tavily")
    try:
        for _ in range(5):
            app_state.market.report_circuit_result("tavily", success=False, threshold=3, open_sec=60)
        with app_state.market.circuit_lock:
            app_state.market.circuit_states["tavily"]["status"] = "HALF_OPEN"
            app_state.market.circuit_states["tavily"]["probing"] = True
            app_state.market.circuit_states["tavily"]["probing_since"] = time.monotonic()

        called = False

        def _should_not_create_client(_key):
            nonlocal called
            called = True
            return MagicMock()

        monkeypatch.setattr(tavily_mod, "_get_tavily_client", _should_not_create_client)
        assert tavily_mod.tavily_search("AAPL", api_key="tvly-test") == []
        assert called is False
    finally:
        _reset_circuits("tavily")


def test_ddgs_news_search_timelimit_none_preserves_primary_d4_07(monkeypatch) -> None:
    seen_calls: list[dict] = []

    class _RecordingSession:
        def news(self, **kw):
            seen_calls.append(kw)
            return []

    monkeypatch.setattr(ddgs_mod.time, "sleep", lambda _s: None)
    ddgs_mod.ddgs_news_search("AAPL", timelimit=None, ddgs_session=_RecordingSession(), backend="auto")
    assert len(seen_calls) == 1
    assert seen_calls[0]["query"] == "AAPL"


def test_google_trends_deadline_before_advancing_clock_d4_08(monkeypatch) -> None:
    monkeypatch.setattr(ts, "TrendReq", object)
    old_ts = ts._GOOGLE_TRENDS_LAST_CALL
    try:
        ts._GOOGLE_TRENDS_LAST_CALL = 100.0
        monkeypatch.setattr(ts, "_is_collection_deadline_exceeded", lambda: True)
        assert ts._trend_queries_for_keyword("nvidia", "us") == []
        assert ts._GOOGLE_TRENDS_LAST_CALL == 100.0
    finally:
        ts._GOOGLE_TRENDS_LAST_CALL = old_ts


def test_tech_lines_schema_and_sentinel_guard_d4_09(monkeypatch) -> None:
    items_schema = ai_service._TECH_LINES_REPAIR_SCHEMA["properties"]["lines"]["items"]
    assert items_schema["additionalProperties"] is False
    assert "start_price" in items_schema["properties"]
    assert "start_price" in items_schema["required"]

    repair_calls: list[str] = []
    monkeypatch.setattr(
        ai_service,
        "call_mistral_chat",
        lambda *_a, **_kw: {"choices": [{"message": {"content": "(思考プロセスのみの応答でした)"}}]},
    )
    monkeypatch.setattr(
        ai_service,
        "repair_technical_lines_json_with_llm",
        lambda *_a: (repair_calls.append("called") or {}, ""),
    )
    res = ai_service.generate_ai_technical_lines(
        "k", "AAPL", "us", "3mo", [{"date": "2026-01-01", "close": 100}]
    )
    assert repair_calls == []
    assert "JSONパースに一部不備" in res["summary"]


def test_mistral_fast_fail_before_rate_limit_slot_d4_10(monkeypatch) -> None:
    _reset_circuits("mistral")
    try:
        for _ in range(3):
            app_state.market.report_circuit_result("mistral", success=False, threshold=3, open_sec=60)
        slot_calls: list[str] = []
        monkeypatch.setattr(ai_service, "is_openai_provider", lambda: False)
        monkeypatch.setattr(ai_service, "_get_mistral_client", lambda _k: MagicMock())
        monkeypatch.setattr(
            ai_service,
            "_acquire_mistral_call_slot_with_reservation",
            lambda _i: slot_calls.append("slot") or (0.0, 0.0),
        )
        out = ai_service.call_mistral_chat("real-key", [{"role": "user", "content": "hi"}], use_cache=False)
        assert slot_calls == []
        assert out["error"]["status_code"] == 503
    finally:
        _reset_circuits("mistral")


def test_ai_tools_skips_yfinance_fallback_when_rate_limited_d4_11(monkeypatch) -> None:
    monkeypatch.setattr(app_state.market, "is_yf_rate_limited", lambda: True)
    with (
        patch("utils.stock_payload.get_stock_info_cached", return_value={}),
        patch("services.realtime_engine.realtime_market_engine.get_market_snapshot", return_value={}),
        patch.object(app_state.payload_disk_cache, "get", return_value=None),
        patch("utils.market_utils.safe_get_ticker", side_effect=AssertionError("must not call yfinance")),
    ):
        out = ai_tools._tool_get_stock_quote({"symbol": "AAPL", "market": "us"})
    assert "データ取得不可" in out["status"]


def test_chart_image_normalization_and_literal_ip_guard_d4_12(monkeypatch) -> None:
    called_urls: list[str] = []
    monkeypatch.setattr(openai_service, "get_openai_model", lambda: "gpt-4o")

    def _fake_chat(_key, **kwargs):
        called_urls.append(kwargs["messages"][1]["content"][1]["image_url"]["url"])
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(openai_service, "call_openai_chat", _fake_chat)
    res_ok = openai_service.analyze_chart_image_with_openai("sk", "DATA:IMAGE/PNG;base64,QUJD")
    assert res_ok["analysis"] == "ok"
    assert called_urls[-1].startswith("data:image/PNG;base64,")

    for bad_url in (
        "http://169.254.169.254/latest.png",
        "http://[::ffff:169.254.169.254]/latest.png",
        "https://user:pass@example.com/a.png",
        "data:text/html,<script>",
    ):
        assert "error" in openai_service.analyze_chart_image_with_openai("sk", bad_url)
        assert "error" in ai_service.analyze_chart_image_with_mistral("k", bad_url)


def test_http_size_caps_and_langsearch_redirect_guard_d4_13(monkeypatch) -> None:
    resp_big = SimpleNamespace(headers={"Content-Length": str(10 * 1024 * 1024)}, content=b"")
    with pytest.raises(ValueError, match="exceeds"):
        openai_service._enforce_openai_response_size(resp_big, 1024, "OpenAI chat")

    sess = ts._get_trend_session()
    assert sess.trust_env is False

    redir_resp = MagicMock(status_code=302, ok=False, headers={})
    with patch.object(ls_mod.curl_requests, "post", return_value=redir_resp) as mock_post:
        with pytest.raises(requests.HTTPError, match="redirects"):
            ls_mod._request_json_post("https://api.langsearch.com/v1/web-search", {}, {})
    assert mock_post.call_args.kwargs.get("allow_redirects") is False


def test_wikipedia_and_gdelt_non_list_payloads_d4_14(monkeypatch) -> None:
    monkeypatch.setattr(ts, "_request_json", lambda *_a, **_kw: {"items": [{"articles": "bad"}]})
    assert ts.collect_wikipedia_top_items("us") == []

    monkeypatch.setattr(ts, "_request_json", lambda *_a, **_kw: {"articles": {"bad": 1}})
    assert ts.collect_gdelt_items(["AAPL"], "us") == []


def test_tool_call_duplicate_id_isolation_d4_15(monkeypatch) -> None:
    responses = iter(
        [
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {"id": "dup", "type": "function", "function": {"name": "", "arguments": "{}"}},
                                {"id": "dup", "type": "function", "function": {"name": "get_stock_quote", "arguments": "{}"}},
                            ],
                        }
                    }
                ]
            },
            {"choices": [{"message": {"content": "done"}}]},
        ]
    )
    executed: list[str] = []
    monkeypatch.setattr(openai_service, "call_openai_chat", lambda *_a, **_kw: next(responses))
    monkeypatch.setattr(
        openai_service,
        "execute_mistral_tool_call",
        lambda name, _args: executed.append(name) or {"ok": True},
    )
    res = openai_service.call_openai_chat_with_tools("sk", [{"role": "user", "content": "hi"}])
    assert res["choices"][0]["message"]["content"] == "done"
    assert executed == ["get_stock_quote"]


def test_load_sdk_agent_isolates_uuid_parse_error_d4_16(monkeypatch) -> None:
    calls: list[dict] = []

    class _FakeAgent:
        @staticmethod
        def load_from(**kw):
            calls.append(kw)
            if "agent_id" in kw:
                raise ValueError("backend rejected agent_id")
            return "by_name"

    fake_agents_mod = SimpleNamespace(Agent=_FakeAgent)
    monkeypatch.setattr(mistral_compat, "HAS_AGENTS_SDK", True)
    monkeypatch.setattr(mistral_compat, "mistral_agents", fake_agents_mod)

    assert mistral_compat.load_sdk_agent("my-agent-name", api_key="k") == "by_name"
    with pytest.raises(ValueError, match="backend rejected agent_id"):
        mistral_compat.load_sdk_agent("12345678-1234-5678-1234-567812345678", api_key="k")
