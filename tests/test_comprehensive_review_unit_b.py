"""Regression tests for Unit B code review fixes ([B-1] through [B-3])."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pydantic import BaseModel

from app_state import app_state
from credential_manager import PREVIEW_API_KEY

# ---------------------------------------------------------------------------
# [B-1] services/news_service.py: negative cache bypass + preview/provider cache isolation
# ---------------------------------------------------------------------------


def test_news_force_refresh_bypasses_negative_cache(monkeypatch):
    from services.news_service import NewsService

    bypass_flags: list[tuple[str, bool]] = []

    def fake_neg_cache(key, fetcher, ttl, neg_ttl, bypass_negative_cache=False):
        bypass_flags.append((key, bypass_negative_cache))
        return "Sample context headline"

    monkeypatch.setattr("services.news_service.get_cached_context_with_negative_cache", fake_neg_cache)
    monkeypatch.setattr("services.news_service.collect_market_trending_titles", lambda *a, **kw: ["NVDA"])
    monkeypatch.setattr(
        "services.news_service.get_cached",
        lambda k, fn, duration=0, valid_func=None: fn(),
    )
    monkeypatch.setattr("services.news_service._set_cached_value", lambda *a, **kw: None)
    monkeypatch.setattr(
        "services.news_service.call_mistral_chat",
        lambda *a, **kw: {"choices": [{"message": {"content": '{"us":"u","jp":"j","trends":"t"}'}}]},
    )

    NewsService().get_synchronized_market_news("real-mistral-key", "", "", force_refresh=True)
    assert all(flag is True for _, flag in bypass_flags)
    assert len(bypass_flags) == 2

    bypass_flags.clear()
    NewsService().get_synchronized_market_news("real-mistral-key", "", "", force_refresh=False)
    assert all(flag is False for _, flag in bypass_flags)
    assert len(bypass_flags) == 2


def test_news_preview_mode_and_provider_switch_do_not_poison_shared_cache(monkeypatch):
    from services.news_service import NewsService

    in_memory_cache: dict[str, dict] = {}

    def fake_get_cached(key, fn, duration=0, valid_func=None):
        if key in in_memory_cache:
            return in_memory_cache[key]
        val = fn()
        if valid_func is None or valid_func(val):
            in_memory_cache[key] = val
        return val

    monkeypatch.setattr(
        "services.news_service.get_cached_context_with_negative_cache",
        lambda k, fn, *a, **kw: "US headline",
    )
    monkeypatch.setattr(
        "services.news_service.collect_market_trending_titles", lambda *a, **kw: ["Trend1"]
    )
    monkeypatch.setattr("services.news_service.get_cached", fake_get_cached)
    monkeypatch.setattr(
        "services.news_service._set_cached_value",
        lambda k, v, duration=0: in_memory_cache.__setitem__(k, v),
    )

    provider_state = {"openai": False}
    monkeypatch.setattr("services.news_service.is_openai_provider", lambda: provider_state["openai"])
    monkeypatch.setattr("services.news_service.get_openai_model", lambda: "gpt-4o-mini")

    calls: list[str] = []

    def fake_llm(api_key, messages, *a, **kw):
        calls.append(f"{api_key}:{provider_state['openai']}")
        if api_key.startswith("mns-preview"):
            payload = {
                "us": "Preview dummy market summary headline with enough length for formatter.",
                "jp": "Preview JP market summary headline with enough length for formatter.",
                "trends": "Preview trends summary headline with enough length for formatter.",
            }
        elif provider_state["openai"]:
            payload = {
                "us": "OpenAI real market summary headline with enough length for formatter.",
                "jp": "OpenAI JP market summary headline with enough length for formatter.",
                "trends": "OpenAI trends summary headline with enough length for formatter.",
            }
        else:
            payload = {
                "us": "Mistral real market summary headline with enough length for formatter.",
                "jp": "Mistral JP market summary headline with enough length for formatter.",
                "trends": "Mistral trends summary headline with enough length for formatter.",
            }
        return {"choices": [{"message": {"content": json.dumps(payload)}}]}

    monkeypatch.setattr("services.news_service.call_mistral_chat", fake_llm)

    svc = NewsService()
    prev = svc.get_synchronized_market_news(PREVIEW_API_KEY, "", "", force_refresh=False)
    assert "Preview dummy market summary" in prev["us"]["content"]
    assert in_memory_cache == {}

    real_mi = svc.get_synchronized_market_news("real-mistral-key", "", "", force_refresh=False)
    assert "Mistral real market summary" in real_mi["us"]["content"]

    provider_state["openai"] = True
    real_oa = svc.get_synchronized_market_news("sk-openai-real", "", "", force_refresh=False)
    assert "OpenAI real market summary" in real_oa["us"]["content"]
    assert len(calls) == 3


# ---------------------------------------------------------------------------
# [B-2] services/openai_service.py: message normalization, dual deltas, chart error
# ---------------------------------------------------------------------------


def test_openai_call_and_stream_normalize_non_dict_messages_and_emit_dual_deltas(monkeypatch):
    from services import openai_service

    class MsgModel(BaseModel):
        role: str
        content: str

    app_state.market.report_circuit_result("openai_compatible", success=True)
    monkeypatch.setattr(openai_service, "get_openai_base_url", lambda: "https://api.example.com/v1")
    monkeypatch.setattr(openai_service, "validate_outbound_base_url", lambda url: (True, ""))
    monkeypatch.setattr(openai_service, "get_openai_model", lambda: "deepseek-reasoner")

    posted_payloads: list[dict] = []

    class FakeResp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": '{"ok": true}'}}]}

    class DummySchema(BaseModel):
        ok: bool

    class FakeStreamResp:
        status_code = 200

        def iter_lines(self):
            chunk = json.dumps(
                {
                    "choices": [
                        {
                            "delta": {
                                "content": "Visible answer",
                                "reasoning_content": "Internal step",
                            }
                        }
                    ]
                }
            )
            yield f"data: {chunk}"
            yield "data: [DONE]"

        def close(self):
            return None

    class FakeStreamCM:
        def __enter__(self):
            return FakeStreamResp()

        def __exit__(self, *args):
            return False

    class FakeClient:
        def post(self, url, json=None, headers=None):
            # Ensure JSON serializability holds without TypeError.
            import json as _json

            _json.dumps(json)
            posted_payloads.append(json)
            return FakeResp()

        def stream(self, method, url, json=None, headers=None):
            import json as _json

            _json.dumps(json)
            posted_payloads.append(json)
            return FakeStreamCM()

    monkeypatch.setattr(openai_service, "_get_openai_client", lambda base_url, api_key: FakeClient())

    msgs = [
        MsgModel(role="system", content="System base."),
        SimpleNamespace(role="user", content="Analyze AAPL"),
    ]
    res = openai_service.call_openai_chat(
        "sk-real",
        msgs,
        use_cache=False,
        response_format=DummySchema,
    )
    assert res["choices"][0]["message"]["content"] == {"ok": True}
    assert posted_payloads[0]["messages"][0]["role"] == "system"
    assert "スキーマ定義" in posted_payloads[0]["messages"][0]["content"]
    assert posted_payloads[0]["messages"][1] == {"role": "user", "content": "Analyze AAPL"}

    events = list(
        openai_service.stream_openai_chat(
            "sk-real",
            msgs,
            include_thinking=True,
        )
    )
    assert {"type": "delta", "text": "Visible answer"} in events
    assert {"type": "thinking", "text": "Internal step"} in events


def test_analyze_chart_image_with_openai_returns_structured_vision_error(monkeypatch):
    from services import openai_service

    with patch.object(
        openai_service,
        "call_openai_chat",
        return_value={"error": {"message": "image_url not supported", "status_code": 400}},
    ):
        err = openai_service.analyze_chart_image_with_openai("sk-real", "aGVsbG8=", model="gpt-4")

    assert isinstance(err["error"], dict)
    assert err["error"]["status_code"] == 400
    assert "Vision" in err["error"]["message"]


# ---------------------------------------------------------------------------
# [B-3] Response close on success/error + post-fetch cache timestamps
# ---------------------------------------------------------------------------


def _make_close_tracker(status_code: int = 200, text: str = "", raise_on_text: bool = False):
    tracker = {"closed": 0}

    class _FakeResp:
        def __init__(self):
            self.status_code = status_code
            self.headers = {}
            self.content = text.encode("utf-8")

        @property
        def text(self):
            if raise_on_text:
                raise RuntimeError("boom reading body")
            return text

        def raise_for_status(self):
            if status_code >= 400:
                raise RuntimeError(f"HTTP {status_code}")

        def json(self):
            return {"ok": True, "quotes": []}

        def close(self):
            tracker["closed"] += 1

    return _FakeResp(), tracker


def test_http_responses_are_closed_and_nikkei_cache_uses_post_fetch_time(monkeypatch):
    import trend_sources
    from services import fallback_provider
    from services.realtime import scrapers
    from services.stock import provider as stock_provider
    from utils.http_utils import close_response_quietly

    # Helper safety on None / missing close / failing close.
    close_response_quietly(None)
    close_response_quietly(SimpleNamespace())
    close_response_quietly(SimpleNamespace(close=lambda: (_ for _ in ()).throw(OSError("ignore"))))

    # 1) trend_sources _request_json & _fetch_rss_feed close even on raise_for_status failure.
    bad_resp, bad_tracker = _make_close_tracker(status_code=500)
    monkeypatch.setattr(trend_sources, "_get_trend_session", lambda: MagicMock(get=lambda *a, **k: bad_resp))
    try:
        trend_sources._request_json("https://example.com/api")
    except RuntimeError:
        pass
    assert bad_tracker["closed"] == 1

    rss_resp, rss_tracker = _make_close_tracker(status_code=200, text="<rss></rss>")
    monkeypatch.setattr(trend_sources, "_get_trend_session", lambda: MagicMock(get=lambda *a, **k: rss_resp))
    monkeypatch.setattr(trend_sources, "feedparser", SimpleNamespace(parse=lambda txt: {"entries": []}))
    trend_sources._fetch_rss_feed("https://example.com/rss")
    assert rss_tracker["closed"] == 1

    # 2) YFinanceProvider._search_fallback closes response.
    sp_resp, sp_tracker = _make_close_tracker(status_code=200)
    monkeypatch.setattr(stock_provider, "_is_yfinance_rate_limit_error", lambda exc: False)
    provider = stock_provider.YFinanceProvider()
    fake_shim = SimpleNamespace(
        yf_session_manager=SimpleNamespace(get_session=lambda: SimpleNamespace(get=lambda *a, **kw: sp_resp)),
        _handle_yf_rate_limit=lambda *a, **kw: None,
    )
    monkeypatch.setattr(stock_provider, "_shim", lambda: fake_shim)
    provider._search_fallback("AAPL", 10, app_state.market)
    assert sp_tracker["closed"] == 1

    # 3) Nikkei225JPProvider & Nikkei225JPScraper close response AND stamp post-fetch time.
    clock = {"t": 100.0}
    monkeypatch.setattr(fallback_provider.time, "time", lambda: clock["t"])
    monkeypatch.setattr(scrapers.time, "time", lambda: clock["t"])
    monkeypatch.setattr(fallback_provider, "_is_scraper_blocked", lambda: False)
    monkeypatch.setattr(scrapers, "_is_scraper_blocked", lambda: False)

    adr_body = 'A0[0]="7203_Toyota_0_0_0_0_0_0_2500_+10_+0.4_0_0_0_0_0_0_0_0_0_0";'
    fb_resp, fb_tracker = _make_close_tracker(status_code=200, text=adr_body)

    def slow_fb_get(*args, **kwargs):
        clock["t"] += 15.0  # Simulate > 10s network round-trip
        return fb_resp

    nk_fb = fallback_provider.Nikkei225JPProvider()
    nk_fb._refresh_adr_cache(SimpleNamespace(get=slow_fb_get), is_session=True, max_age=10.0)
    assert fb_tracker["closed"] == 1
    assert nk_fb._adr_cache_time == clock["t"]

    sc_resp, sc_tracker = _make_close_tracker(status_code=200, text=adr_body)

    def slow_sc_get(*args, **kwargs):
        clock["t"] += 15.0
        return sc_resp

    nk_sc = scrapers.Nikkei225JPScraper()
    nk_sc._get_session = lambda: SimpleNamespace(get=slow_sc_get)
    nk_sc._refresh_adr_cache(max_age=10.0)
    assert sc_tracker["closed"] == 1
    assert nk_sc._adr_cache_time == clock["t"]

    # 4) Fallback HTML scrapers close responses even on parsing exceptions / non-200.
    err_resp1, err_tr1 = _make_close_tracker(status_code=503)
    y_web = fallback_provider.YahooWebScraperProvider()
    y_web._get_client = lambda: (SimpleNamespace(get=lambda *a, **kw: err_resp1), True)
    y_web.get_latest_quote("AAPL")
    assert err_tr1["closed"] == 1

    err_resp2, err_tr2 = _make_close_tracker(status_code=200, raise_on_text=True)
    get_calls2 = {"n": 0}

    def counted_get2(*a, **kw):
        get_calls2["n"] += 1
        return err_resp2

    y_jp_sc = scrapers.YahooJPRealtimeScraper()
    y_jp_sc._get_session = lambda: SimpleNamespace(get=counted_get2)
    y_jp_sc.fetch_jp_symbol("7203.T")
    assert get_calls2["n"] >= 1
    assert err_tr2["closed"] == get_calls2["n"]

    err_resp3, err_tr3 = _make_close_tracker(status_code=200, raise_on_text=True)
    get_calls3 = {"n": 0}

    def counted_get3(*a, **kw):
        get_calls3["n"] += 1
        return err_resp3

    y_jp_sc._get_session = lambda: SimpleNamespace(get=counted_get3)
    y_jp_sc.fetch_pts_symbol("7203.T")
    assert get_calls3["n"] >= 1
    assert err_tr3["closed"] == get_calls3["n"]
