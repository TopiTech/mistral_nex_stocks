"""Regression and edge-case tests for Unit 2 service/data/realtime fixes (2026-11-22)."""

from __future__ import annotations

import math
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

import constants
from app_state import app_state
from market_state import MarketDataState


def _set_circuit_state(
    service: str,
    *,
    status: str,
    open_until: float = 0.0,
    probing: bool = False,
    probing_since: float = 0.0,
) -> None:
    with app_state.market.circuit_lock:
        st = app_state.market.circuit_states[service]
        st["status"] = status
        st["open_until"] = open_until
        st["probing"] = probing
        st["probing_since"] = probing_since
        if status == "CLOSED":
            st["timeout_streak"] = 0


def _reset_market_circuit(service: str, symbol: str | None = None) -> None:
    app_state.market.report_circuit_result(service, success=True, symbol=symbol)
    if symbol is None:
        _set_circuit_state(service, status="CLOSED")
    else:
        with app_state.market.history_circuit_lock:
            app_state.market.history_circuit_states.pop(symbol, None)


# ---------------------------------------------------------------------------
# market_state: HALF_OPEN TTL reclaim & atomic enter_circuit_call
# ---------------------------------------------------------------------------


def test_circuit_enter_call_ttl_reclaim_and_closed_race() -> None:
    market = MarketDataState()
    svc = "mistral"
    with market.circuit_lock:
        st = market.circuit_states[svc]
        st["status"] = "HALF_OPEN"
        st["probing"] = False
        st["probing_since"] = 0.0

    # First caller claims the probe atomically.
    allowed1, claimed1, reason1 = market.enter_circuit_call(svc)
    assert (allowed1, claimed1, reason1) == (True, True, "")

    # Concurrent second caller is busy-rejected while probe is fresh.
    allowed2, claimed2, reason2 = market.enter_circuit_call(svc)
    assert (allowed2, claimed2, reason2) == (False, False, "busy")

    # Stale probe past _CIRCUIT_PROBE_TTL_SEC is reclaimed cleanly.
    with market.circuit_lock:
        market.circuit_states[svc]["probing_since"] = (
            time.monotonic() - (MarketDataState._CIRCUIT_PROBE_TTL_SEC + 5.0)
        )
    allowed3, claimed3, reason3 = market.enter_circuit_call(svc)
    assert (allowed3, claimed3, reason3) == (True, True, "")

    # If state flips HALF_OPEN -> CLOSED between get_circuit_state and try_claim,
    # enter_circuit_call allows the call cleanly without false busy 503.
    def _flip_to_closed(_service: str, symbol: str | None = None) -> bool:
        with market.circuit_lock:
            market.circuit_states[svc]["status"] = "CLOSED"
            market.circuit_states[svc]["probing"] = False
        return False

    with market.circuit_lock:
        market.circuit_states[svc]["status"] = "HALF_OPEN"
        market.circuit_states[svc]["probing"] = False
    with patch.object(market, "try_claim_circuit_probe", side_effect=_flip_to_closed):
        allowed4, claimed4, reason4 = market.enter_circuit_call(svc)
    assert (allowed4, claimed4, reason4) == (True, False, "")


# ---------------------------------------------------------------------------
# ai_portfolio_service: word boundary + preserve saved portfolio on failure
# ---------------------------------------------------------------------------


def test_ai_portfolio_keyword_boundary_and_preserve_existing_on_error(tmp_path, monkeypatch) -> None:
    from services import ai_portfolio_service as aps

    assert aps._theme_matches_keywords("ai半導体", (), ("ai",)) is True
    assert aps._theme_matches_keywords("gen-ai", (), ("ai",)) is True
    assert aps._theme_matches_keywords("retail & airlines", (), ("ai",)) is False
    assert aps._theme_matches_keywords("container shipping", (), ("ai",)) is False

    store_file = tmp_path / "ai_portfolios.json"
    monkeypatch.setattr(aps, "AI_PORTFOLIO_STORAGE_FILE", store_file)

    existing = {
        "id": "custom-1234",
        "theme": "retail",
        "title": "Retail Leaders",
        "description": "Saved custom portfolio",
        "generated_by": "ai",
        "items": [{"symbol": "WMT", "market": "us", "name": "Walmart", "weight": 100.0}],
    }
    monkeypatch.setattr(aps, "_load_saved_ai_portfolios_strict", lambda: [existing])
    with (
        patch("credential_manager.is_openai_provider", return_value=False),
        patch.object(aps, "get_mistral_api_key", return_value=""),
    ):
        result = aps.generate_ai_portfolio_by_theme("retail", force_rebalance=True, api_key="")
    assert result["id"] == "custom-1234"
    assert result["items"][0]["symbol"] == "WMT"
    assert "\n" not in aps._safe_log_theme("ai\nline\r\tctrl")


# ---------------------------------------------------------------------------
# news_service & embeddings_service & market_data_service
# ---------------------------------------------------------------------------


def test_news_service_downgrades_placeholder_text() -> None:
    from services import news_service as ns

    with (
        patch.object(ns, "get_cached_context_with_negative_cache", return_value="US/JP news context"),
        patch.object(ns, "collect_market_trending_titles", return_value=["AAPL"]),
        patch.object(ns, "is_openai_provider", return_value=False),
        patch.object(
            ns,
            "get_cached",
            return_value={"us": "解析中...", "jp": "解析エラー", "trends": "強気トレンドです"},
        ),
    ):
        res = ns.news_service.get_synchronized_market_news("real-key", "", "")
    assert res["us"]["status"] == "error"
    assert res["us"]["content"] == ""
    assert res["jp"]["status"] == "error"
    assert res["jp"]["content"] == ""
    assert res["trends"]["status"] == "success"
    assert "強気" in res["trends"]["content"]


def test_embeddings_service_string_guard_and_copy_isolation() -> None:
    from services import embeddings_service as emb

    assert emb.get_mistral_embeddings_batch("hello", api_key="k") == []  # type: ignore[arg-type]

    _reset_market_circuit("mistral")
    fake_client = MagicMock()
    fake_client.embeddings.create.return_value = SimpleNamespace(
        data=[{"index": 0, "embedding": [0.1, 0.2, 0.3]}]
    )
    with emb._EMBEDDINGS_CACHE_LOCK:
        emb._EMBEDDINGS_CACHE.clear()
    with (
        patch("credential_manager.is_openai_provider", return_value=False),
        patch.object(emb, "_get_client", return_value=fake_client),
    ):
        first = emb.get_mistral_embeddings_batch(["alpha"], api_key="mistral-real-key-123")
        assert first == [[0.1, 0.2, 0.3]]
        assert first[0] is not None
        first[0][0] = 999.0
        second = emb.get_mistral_embeddings_batch(["alpha"], api_key="mistral-real-key-123")
        assert second == [[0.1, 0.2, 0.3]]
    with emb._EMBEDDINGS_CACHE_LOCK:
        emb._EMBEDDINGS_CACHE.clear()


def test_market_data_service_non_string_sector_and_name() -> None:
    from services.market_data_service import _build_market_row, build_heatmap_payload

    row = _build_market_row(
        "AAPL",
        "us",
        {"price": 190.0, "sector": {"bad": 1}, "name": ["bad"], "shortName": 123},
        "Apple Fallback",
    )
    assert isinstance(row["sector"], str) and row["sector"]
    assert isinstance(row["name"], str) and row["name"]

    hm = build_heatmap_payload(
        "us",
        ["AAPL"],
        fetch_batch_fn=lambda _items, **_kw: [
            "bad-item",
            {"symbol": "AAPL", "price": 190.0, "market_cap": 1_000_000, "sector": 99, "name": {}},
        ],
    )
    assert len(hm["stocks"]) == 1
    assert isinstance(hm["stocks"][0]["sector"], str)
    assert isinstance(hm["stocks"][0]["name"], str)


# ---------------------------------------------------------------------------
# openai_service & ai_service: circuit/cache/streaming/tools/vision
# ---------------------------------------------------------------------------


def test_openai_service_temp_cache_key_vision_400_embeddings_and_stream_disconnect() -> None:
    from services import openai_service as os_mod

    _reset_market_circuit("openai_compatible")
    try:
        # 1. AI-05: Clamped temperatures (3.2 -> 2.0) hit the same cache key.
        resp_ok = SimpleNamespace(
            status_code=200,
            text='{"choices":[{"message":{"content":"ok"}}]}',
            json=lambda: {"choices": [{"message": {"content": "ok"}}]},
        )
        mock_client = MagicMock()
        mock_client.post.return_value = resp_ok
        with (
            patch.object(os_mod, "_get_openai_client", return_value=mock_client),
            patch.object(os_mod, "get_openai_base_url", return_value="https://api.openai.com/v1"),
            patch.object(os_mod, "validate_outbound_base_url", return_value=(True, "")),
        ):
            r1 = os_mod.call_openai_chat(
                "sk-test-1234",
                [{"role": "user", "content": "hi"}],
                temperature=3.2,
                use_cache=True,
                cache_key_override="ai05_temp_norm_test",
            )
            r2 = os_mod.call_openai_chat(
                "sk-test-1234",
                [{"role": "user", "content": "hi"}],
                temperature=2.0,
                use_cache=True,
                cache_key_override="ai05_temp_norm_test",
            )
        assert r1["choices"][0]["message"]["content"] == "ok"
        assert r2["choices"][0]["message"]["content"] == "ok"
        assert mock_client.post.call_count == 1

        # 2. Vision 400 vs base_url 400 error discrimination.
        with patch.object(
            os_mod,
            "call_openai_chat",
            return_value={
                "error": {
                    "message": "Invalid openai_base_url rejected by policy",
                    "status_code": 400,
                }
            },
        ):
            err_res = os_mod.analyze_chart_image_with_openai(
                "sk-test-1234", "data:image/png;base64,AAAA"
            )
        assert "画像認識（Vision）に対応していません" not in err_res["error"]["message"]

        # 3. AI-03: Embeddings HTTP 200 with invalid JSON does NOT close HALF_OPEN circuit.
        _set_circuit_state("openai_compatible", status="HALF_OPEN")
        bad_json_resp = SimpleNamespace(
            status_code=200,
            text="<html>bad gateway</html>",
            json=MagicMock(side_effect=ValueError("invalid json")),
        )
        mock_emb_client = MagicMock()
        mock_emb_client.post.return_value = bad_json_resp
        with (
            patch.object(os_mod, "_get_openai_client", return_value=mock_emb_client),
            patch.object(os_mod, "get_openai_base_url", return_value="https://api.openai.com/v1"),
            patch.object(os_mod, "validate_outbound_base_url", return_value=(True, "")),
        ):
            out = os_mod.get_openai_embeddings_batch(["alpha"], api_key="sk-test-1234")
        assert out == [None]
        st_after = app_state.market.get_circuit_state("openai_compatible")
        assert st_after["status"] == "HALF_OPEN"
        assert st_after["probing"] is False

        # 4. AI-06: Client disconnect after receiving chunk promotes HALF_OPEN -> CLOSED.
        _set_circuit_state("openai_compatible", status="HALF_OPEN")

        class _FakeStreamCtx:
            status_code = 200

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return None

            def close(self):
                return None

            def iter_lines(self):
                yield b'data: {"choices":[{"delta":{"content":"hello"}}]}'
                yield b'data: {"choices":[{"delta":{"content":" world"}}]}'

        mock_stream_client = MagicMock()
        mock_stream_client.stream.return_value = _FakeStreamCtx()
        with (
            patch.object(os_mod, "_get_openai_client", return_value=mock_stream_client),
            patch.object(os_mod, "get_openai_base_url", return_value="https://api.openai.com/v1"),
            patch.object(os_mod, "validate_outbound_base_url", return_value=(True, "")),
        ):
            gen = os_mod.stream_openai_chat("sk-test-1234", [{"role": "user", "content": "hi"}])
            first_evt = next(gen)
            assert first_evt == {"type": "delta", "text": "hello"}
            gen.close()
        st_closed = app_state.market.get_circuit_state("openai_compatible")
        assert st_closed["status"] == "CLOSED"
    finally:
        _reset_market_circuit("openai_compatible")


def test_ai_service_fallback_circuit_gate_stream_disconnect_and_tool_cap() -> None:
    from services import ai_service

    _reset_market_circuit("mistral")
    try:
        # AI-04: _is_fallback=True must still obey an OPEN circuit breaker.
        _set_circuit_state("mistral", status="OPEN", open_until=time.time() + 120.0)
        events = list(
            ai_service.stream_mistral_chat(
                "mistral-real-key-123",
                [{"role": "user", "content": "hi"}],
                _is_fallback=True,
            )
        )
        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert events[0]["status_code"] == 503

        # Preview mode still works even while Mistral circuit is OPEN.
        from credential_manager import PREVIEW_API_KEY

        with patch.object(ai_service, "is_openai_provider", return_value=False):
            tech = ai_service.generate_ai_technical_lines(
                PREVIEW_API_KEY,
                "AAPL",
                "us",
                "1mo",
                [{"date": "2026-01-01", "open": 100, "high": 102, "low": 99, "close": 101}],
            )
        assert "error" not in tech

        # AI-06: Client disconnect on HALF_OPEN stream after chunk closes circuit.
        _set_circuit_state("mistral", status="HALF_OPEN")
        chunk1 = SimpleNamespace(
            data=SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="part1"))])
        )
        chunk2 = SimpleNamespace(
            data=SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="part2"))])
        )
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(stream=lambda **_kw: iter([chunk1, chunk2]))
        )
        with (
            patch.object(ai_service, "is_openai_provider", return_value=False),
            patch.object(ai_service, "_get_mistral_client", return_value=fake_client),
            patch.object(ai_service, "_resolve_agents_target", return_value=(False, "")),
        ):
            gen = ai_service.stream_mistral_chat(
                "mistral-real-key-123", [{"role": "user", "content": "hi"}]
            )
            evt = next(gen)
            assert evt == {"type": "delta", "text": "part1"}
            gen.close()
        assert app_state.market.get_circuit_state("mistral")["status"] == "CLOSED"

        # AI-09: MAX_TOOL_ITERATIONS caps loop symmetrically across Mistral & OpenAI.
        tool_resp = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {"id": "tc1", "function": {"name": "get_stock_quote", "arguments": None}}
                        ],
                    }
                }
            ]
        }
        with (
            patch.object(ai_service, "is_openai_provider", return_value=False),
            patch.object(ai_service, "call_mistral_chat", return_value=tool_resp) as mock_call,
            patch("services.ai_tools.execute_mistral_tool_call", return_value='{"ok": true}'),
        ):
            ai_service.call_mistral_chat_with_tools(
                "mistral-real-key-123",
                [{"role": "user", "content": "AAPL"}],
                max_tool_iterations=999,
            )
        assert mock_call.call_count == constants.MAX_TOOL_ITERATIONS + 1

        # Agents stream terminates immediately upon "done" even if stop_sentinel is omitted.
        _reset_market_circuit("mistral")
        fake_res = SimpleNamespace(
            text="Agent final reply",
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

        class _FakeAgent:
            async def run(self, _prompt, on_event=None, timeout=None):
                if on_event:
                    on_event(SimpleNamespace(text="Agent "))
                return fake_res

        with (
            patch.object(ai_service, "is_openai_provider", return_value=False),
            patch.object(ai_service, "_get_mistral_client", return_value=MagicMock()),
            patch.object(ai_service, "_resolve_agents_target", return_value=(True, "ag-1")),
            patch.object(ai_service, "HAS_AGENTS_SDK", True),
            patch.object(ai_service, "mistral_agents", SimpleNamespace(TextDelta=None)),
            patch.object(ai_service, "load_sdk_agent", return_value=_FakeAgent()),
        ):
            agent_events = list(
                ai_service.stream_mistral_chat(
                    "mistral-real-key-123", [{"role": "user", "content": "hi"}]
                )
            )
        assert any(e.get("type") == "done" for e in agent_events)
    finally:
        _reset_market_circuit("mistral")


# ---------------------------------------------------------------------------
# langsearch & tavily: logical-call failure accounting & HALF_OPEN race
# ---------------------------------------------------------------------------


def test_langsearch_retries_count_as_one_circuit_failure() -> None:
    from services.search import langsearch as ls

    _reset_market_circuit("langsearch")
    try:
        with (
            patch.object(ls, "_request_json_post", side_effect=requests.Timeout("slow")),
            patch("tenacity.nap.time.sleep"),
        ):
            with pytest.raises(requests.Timeout):
                ls._langsearch_post_json("http://example.test", {}, {})
        st = app_state.market.get_circuit_state("langsearch")
        # 1 failed logical call (3 HTTP attempts) increments timeout_streak by 1, not 3.
        assert st["status"] == "CLOSED"
        assert st["timeout_streak"] == 1
    finally:
        _reset_market_circuit("langsearch")


def test_tavily_half_open_to_closed_race_does_not_skip() -> None:
    from services.search import tavily as tv

    mock_client = MagicMock()
    mock_client.search.return_value = {"results": [{"title": "Hit", "url": "https://x", "content": "ok"}]}
    with (
        patch.object(tv, "_is_tavily_circuit_open", return_value=False),
        patch.object(tv, "_is_tavily_circuit_half_open", side_effect=[True, False]),
        patch.object(tv, "_try_claim_tavily_probe", return_value=False),
        patch.object(tv, "_get_tavily_client", return_value=mock_client),
        patch.object(tv, "_report_tavily_circuit"),
    ):
        rows = tv.tavily_search("AAPL", api_key="tvly-test-key")
    assert len(rows) == 1
    assert rows[0]["title"] == "Hit"


# ---------------------------------------------------------------------------
# stock/provider + retry + realtime + fallback_provider
# ---------------------------------------------------------------------------


def test_stock_fast_info_sanitizes_nan_and_retry_skip_preserves_429() -> None:
    from services.stock.provider import YFinanceProvider
    from services.stock.retry import with_yfinance_retry
    from services.stock_service import _history_with_timeout

    provider = YFinanceProvider()
    fake_ticker = SimpleNamespace(
        fast_info={
            "previous_close": float("nan"),
            "market_cap": float("inf"),
            "currency": "USD",
            "exchange": "NASDAQ",
            "quote_type": "EQUITY",
        }
    )
    sym = "_TEST_NAN_FAST_INFO_"
    with patch.object(provider, "get_ticker", return_value=fake_ticker):
        fi = provider.get_fast_info(sym)
    for val in fi.values():
        if isinstance(val, float):
            assert math.isfinite(val)
    assert "previousClose" not in fi
    assert "marketCap" not in fi

    news_ticker = SimpleNamespace(
        get_news=lambda: [{"content": {"title": {"bad": 1}, "summary": 42}}]
    )
    with patch.object(provider, "get_ticker", return_value=news_ticker):
        articles = provider.get_news(sym, limit=3)
    assert len(articles) == 1
    assert articles[0]["title"] == ""
    assert articles[0]["summary"] == ""

    # Circuit-open early return must not reset 429 streak in @with_yfinance_retry.
    _reset_market_circuit("yfinance_history", symbol="us:TESTCIR")
    with app_state.market.yfinance_lock:
        orig_rl = app_state.market.is_yfinance_rate_limited
        orig_streak = app_state.market.yfinance_429_streak
        app_state.market.is_yfinance_rate_limited = False
        app_state.market.yfinance_429_streak = 4
    try:
        from market_state import CircuitState

        with app_state.market.history_circuit_lock:
            app_state.market.history_circuit_states["us:TESTCIR"] = CircuitState(
                status="OPEN",
                open_until=time.time() + 120.0,
                timeout_streak=3,
                probing=False,
            )
        # Re-wrapped check: simulate skipped fetch via open circuit inside @with_yfinance_retry.
        res = _history_with_timeout("1mo", "1d", "TESTCIR", market="us")
        assert res.empty
        assert app_state.market.yfinance_429_streak == 4

        @with_yfinance_retry(max_retries=1)
        def _direct_skip():
            from services.stock.retry import suppress_yfinance_retry_reset

            suppress_yfinance_retry_reset()
            return "skipped"

        assert _direct_skip() == "skipped"
        assert app_state.market.yfinance_429_streak == 4
    finally:
        with app_state.market.yfinance_lock:
            app_state.market.is_yfinance_rate_limited = orig_rl
            app_state.market.yfinance_429_streak = orig_streak
        _reset_market_circuit("yfinance_history", symbol="us:TESTCIR")


def test_realtime_utils_string_aware_scan_kabutan_nan_prev_close_and_pts_reg() -> None:
    from services.fallback_provider import Nikkei225JPProvider
    from services.realtime.engine import RealtimeMarketEngine
    from services.realtime.scrapers import YahooJPRealtimeScraper
    from services.realtime.utils import _extract_pts_price_data

    # RT-02: Braces inside string literals must not end extraction early.
    html = 'data:{"ptsPriceData": {"label": "bad } brace", "price": "1,234"}}end'
    seg = _extract_pts_price_data(html)
    assert seg == '{"label": "bad } brace", "price": "1,234"}'

    escaped_html = r'ptsPriceData\":{\"label\":\"x}y\",\"price\":\"2,000\"}tail'
    seg2 = _extract_pts_price_data(escaped_html)
    assert seg2 == r'{\"label\":\"x}y\",\"price\":\"2,000\"}'

    # Malformed kabutan text like "100.5.2" must return 0.0 instead of crashing.
    assert YahooJPRealtimeScraper._parse_positive_kabuka("100.5.2") == 0.0
    assert YahooJPRealtimeScraper._parse_positive_kabuka("1,234.50円") == 1234.5

    # Nikkei225JP previous close <= 0 must be omitted (None), not negative.
    nk = Nikkei225JPProvider()
    with (
        patch.object(nk, "_get_client", return_value=(MagicMock(), False)),
        patch.object(nk, "_refresh_index_cache", return_value={111: ["100.0", "250.0"]}),
    ):
        q = nk.get_latest_quote("^N225")
    assert q is not None
    assert q["regularMarketPreviousClose"] is None

    # DATA-02: Stale registration prevents _handle_pts_update from resurrecting state.
    engine = RealtimeMarketEngine()
    engine.register_symbol("7203.T", "jp")
    with engine.store_lock:
        tok = engine._registration_tokens[("jp", "7203.T")]
        reg = ("jp", "7203.T", tok, engine._engine_epoch)
    engine.unregister_symbol("7203.T", "jp")
    engine._handle_pts_update(
        {"symbol": "7203.T", "price": 2950.0, "pts": True, "updated_at": time.time()},
        registration=reg,
    )
    assert "7203.T" not in engine.pts_store
