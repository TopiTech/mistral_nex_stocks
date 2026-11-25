"""Regression, concurrency, security, and edge-case tests for 2026-11-26 full code review."""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from app_state import app_state
from market_state import MarketDataState
from utils.stock_payload import get_stock_info_cached
from utils.validators import extract_json_payload

# ---------------------------------------------------------------------------
# Unit 1: Foundational Core, Cache/Payload, Storage & Validators
# ---------------------------------------------------------------------------


def test_found01_partial_fast_info_merges_disk_and_prior_when_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FOUND-01: Rate-limited partial fast_info must merge disk/prior fundamentals & update caches."""
    sym = "FOUND01TEST"
    app_state.stock_disk_cache.set(
        f"info_disk_{sym}",
        {"shortName": "Found01 Corp", "currency": "USD"},
    )

    state_flags = {"limited": False}
    monkeypatch.setattr(app_state.market, "is_yf_rate_limited", lambda: state_flags["limited"])
    monkeypatch.setattr("utils.market_utils.acquire_yfinance_slot", lambda: True)

    def _fake_fast_info(s: str) -> dict[str, float]:
        if s == sym:
            state_flags["limited"] = True
            return {"regularMarketPrice": 155.25}
        return {}

    monkeypatch.setattr(app_state.stock_provider, "get_fast_info", _fake_fast_info)
    monkeypatch.setattr(
        app_state.fallback_provider,
        "get_latest_quote",
        lambda _s: None,
    )

    info = get_stock_info_cached(sym)
    assert info.get("regularMarketPrice") == 155.25
    assert info.get("shortName") == "Found01 Corp"
    assert info.get("currency") == "USD"

    with app_state.yfinance_short_cache_lock:
        assert f"info_short_{sym}" in app_state.yfinance_short_cache


def test_found02_auto_remove_marks_removed_and_bounds_tombstone_set() -> None:
    """FOUND-02: Auto-removal marks removed_watchlist_symbols, clears on rollback, and enforces cap."""
    import app_bg

    # 1. Removal marks removed_watchlist_symbols across matching markets
    with app_state.market.user_stocks_lock:
        app_state.market.user_us["DELIST26"] = {
            "name": "Delist Co",
            "shares": 0,
            "avg_price": 0,
        }
    threshold = app_state.market.INVALID_SYMBOL_REMOVAL_THRESHOLD
    app_state.market.invalid_symbol_streak["DELIST26"] = threshold - 1

    with patch("app_bg.save_user_stocks"):
        app_bg._auto_remove_invalid_symbols(
            [("DELIST26", "Delist Co", "us")],
            [(app_bg._BATCH_INVALID_MARKER, "DELIST26")],
        )
    assert app_state.market.is_watchlist_removed("DELIST26", "us") is True

    # 2. Failed save clears the tombstone upon rollback
    with app_state.market.user_stocks_lock:
        app_state.market.user_us["ROLLBACK26"] = {
            "name": "Rollback Co",
            "shares": 0,
            "avg_price": 0,
        }
    app_state.market.invalid_symbol_streak["ROLLBACK26"] = threshold - 1
    with (
        patch("app_bg.save_user_stocks", side_effect=OSError("disk error")),
        patch("app_bg.remove_stock_from_caches"),
        patch("app_bg.invalidate_stock_caches"),
    ):
        app_bg._auto_remove_invalid_symbols(
            [("ROLLBACK26", "Rollback Co", "us")],
            [(app_bg._BATCH_INVALID_MARKER, "ROLLBACK26")],
        )
    assert "ROLLBACK26" in app_state.market.user_us
    assert app_state.market.is_watchlist_removed("ROLLBACK26", "us") is False

    # 3. FIFO cap on MarketDataState.removed_watchlist_symbols
    state = MarketDataState()
    state._removed_watchlist_maxsize = 5
    for idx in range(8):
        state.mark_watchlist_removed(f"SYM{idx}", "us")
    assert len(state.removed_watchlist_symbols) <= 5
    assert state.is_watchlist_removed("SYM7", "us") is True
    assert state.is_watchlist_removed("SYM0", "us") is False


def test_found03_extract_json_payload_linear_fence_avoids_redos() -> None:
    """FOUND-03: Unclosed markdown fence with huge whitespace fails fast without ReDoS."""
    adversarial = "```json\n" + (" " * 80000) + "\nnot-valid-json"
    start = time.monotonic()
    with pytest.raises(ValueError):
        extract_json_payload(adversarial)
    elapsed = time.monotonic() - start
    assert elapsed < 1.0

    # Large valid fenced payload (>50k chars) must still parse cleanly
    large_fenced = f"```json\n{json.dumps({'summary': 'X' * 60000, 'ok': True})}\n```"
    parsed = json.loads(extract_json_payload(large_fenced))
    assert len(parsed["summary"]) == 60000
    assert parsed["ok"] is True


# ---------------------------------------------------------------------------
# Unit 2: Services, Workers, Search & Retry
# ---------------------------------------------------------------------------


def test_svc01_openai_error_redaction_and_temperature_validation(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SVC-01: Invalid temperature returns 400 envelope/event; stream logs redact upstream body."""
    import logging
    from types import SimpleNamespace

    from services import openai_service

    monkeypatch.setattr(openai_service, "get_openai_base_url", lambda: "https://api.openai.com/v1")
    monkeypatch.setattr(openai_service, "get_openai_model", lambda: "gpt-4o-mini")
    monkeypatch.setattr(openai_service, "validate_outbound_base_url", lambda _u: (True, ""))
    monkeypatch.setattr(openai_service, "is_preview_api_key", lambda _k: False)

    # Non-finite / boolean temperature must return structured 400 error instead of raising ValueError
    res = openai_service.call_openai_chat(
        "sk-test-valid-key-1234",
        [{"role": "user", "content": "hi"}],
        temperature=float("nan"),
        use_cache=False,
    )
    assert isinstance(res.get("error"), dict)
    assert res["error"]["status_code"] == 400

    stream_events = list(
        openai_service.stream_openai_chat(
            "sk-test-valid-key-1234",
            [{"role": "user", "content": "hi"}],
            temperature=json.loads("true"),
        )
    )
    assert stream_events == [
        {
            "type": "error",
            "message": "temperature は 0.0〜2.0 の範囲の有限数値で指定してください。",
            "status_code": 400,
        }
    ]

    # Upstream stream HTTP error must never echo untrusted prompt/secret content to warning logs
    secret_body = "prompt leak: user holds 100 AAPL secret_portfolio_marker"

    class _FakeErrStream:
        status_code = 400
        text = secret_body

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return None

        def read(self):
            return secret_body.encode("utf-8")

        def close(self):
            return None

    fake_client = SimpleNamespace(stream=lambda *_a, **_kw: _FakeErrStream())
    monkeypatch.setattr(openai_service, "_get_openai_client", lambda *_a, **_kw: fake_client)
    monkeypatch.setattr(
        app_state.market,
        "enter_circuit_call",
        lambda _svc: (True, False, ""),
    )

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        events = list(
            openai_service.stream_openai_chat(
                "sk-test-valid-key-1234",
                [{"role": "user", "content": "hi"}],
            )
        )
    assert events and events[-1]["type"] == "error"
    assert events[-1]["status_code"] == 400
    assert "secret_portfolio_marker" not in caplog.text
    assert "不正" in events[-1]["message"]


def test_svc02_retry_target_log_ddgs_tail_sleep_scraper_stop_and_trend_deadline(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SVC-02: Retry target extracts ticker symbol, DDGS skips final sleep, scraper stop is non-blocking, and trend workers respect deadline."""
    import logging
    from unittest.mock import MagicMock

    import trend_sources
    from services.realtime.scrapers import YahooJPRealtimeScraper
    from services.search import ddgs as ddgs_mod
    from services.stock import retry as retry_mod

    # 1. Timeout retry logging resolves symbol instead of printing self object
    class _FakeProvider:
        pass

    provider_instance = _FakeProvider()
    caplog.clear()
    with (
        caplog.at_level(logging.DEBUG),
        patch.object(retry_mod.time, "sleep"),
    ):
        retry_mod._sleep_transient_retry(
            0,
            2,
            1.0,
            2.0,
            provider_instance,
            is_testing=False,
            is_timeout=True,
            args=(provider_instance, "NVDA"),
        )
    assert "for NVDA after timeout" in caplog.text
    assert "_FakeProvider" not in caplog.text

    # 2. DDGS skips trailing sleep after final attempt
    sleep_calls: list[float] = []
    monkeypatch.setattr(ddgs_mod.time, "sleep", lambda s: sleep_calls.append(s))

    class _AlwaysFailSession:
        def news(self, **_kw):
            raise RuntimeError("ddgs down")

        def text(self, **_kw):
            raise RuntimeError("ddgs down")

    ddgs_mod.ddgs_news_search("AAPL", ddgs_session=_AlwaysFailSession(), backend="auto")
    # plans = [(normalized, timelimit, "auto")] when short == normalized -> 1 attempt -> 0 sleeps
    assert sleep_calls == []

    # Multi-plan news search with 2 plans -> only 1 sleep between attempts, not after final attempt
    sleep_calls.clear()
    ddgs_mod.ddgs_news_search("AAPL stock price today", ddgs_session=_AlwaysFailSession(), backend="bing")
    assert len(sleep_calls) >= 1
    # Check text search single-plan also sleeps 0 times
    sleep_calls.clear()
    ddgs_mod.ddgs_text_search("AAPL", timelimit=None, ddgs_session=_AlwaysFailSession(), backend="auto")
    assert sleep_calls == []

    # 3. YahooJPRealtimeScraper.stop() shuts down executor with wait=False
    scraper = YahooJPRealtimeScraper()
    mock_exec = MagicMock()
    scraper._executor = mock_exec
    scraper.stop()
    mock_exec.shutdown.assert_called_once_with(wait=False, cancel_futures=True)

    # 4. trend_sources worker checks expired cooperative deadline and breaks without HTTP calls
    fetched_urls: list[str] = []
    monkeypatch.setattr(
        trend_sources,
        "_request_json",
        lambda url, **_kw: fetched_urls.append(url) or {},
    )
    expired = time.monotonic() - 1.0
    res = trend_sources._run_with_collection_deadline(
        expired,
        trend_sources.collect_gdelt_items,
        ["AI stocks", "tech market"],
        "us",
    )
    assert res == []
    assert fetched_urls == []


# ---------------------------------------------------------------------------
# Unit 3: Routes & API Concurrency / Lock Ordering
# ---------------------------------------------------------------------------


def test_route02_observe_metrics_acquires_realtime_producer_locks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROUTE-02: /api/metrics must read tv_client.symbols and yahoojp_scraper.symbols under their locks."""
    from app import create_app
    from services.realtime_engine import realtime_market_engine

    class _TrackingLock:
        def __init__(self) -> None:
            self.entries = 0
            self.exits = 0

        def __enter__(self):
            self.entries += 1
            return self

        def __exit__(self, *_a):
            self.exits += 1
            return False

    tv_lock = _TrackingLock()
    jp_lock = _TrackingLock()

    monkeypatch.setattr(realtime_market_engine.tv_client, "lock", tv_lock, raising=False)
    monkeypatch.setattr(realtime_market_engine.yahoojp_scraper, "lock", jp_lock, raising=False)
    monkeypatch.setattr(
        realtime_market_engine,
        "get_market_snapshot",
        dict,
    )
    monkeypatch.setattr(
        realtime_market_engine,
        "get_pts_snapshot",
        dict,
    )

    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as client:
        resp = client.get(
            "/api/metrics",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            headers={"Origin": "http://localhost:5000"},
        )
    assert resp.status_code == 200
    assert tv_lock.entries >= 1 and tv_lock.entries == tv_lock.exits
    assert jp_lock.entries >= 1 and jp_lock.entries == jp_lock.exits


# ---------------------------------------------------------------------------
# Unit 4: Frontend, Templates & Chrome Extension
# ---------------------------------------------------------------------------


def test_fe01_screener_template_contains_all_predefined_sectors() -> None:
    """FE-01: Screener dropdown must cover every sector in sectors.PREDEFINED_SECTORS plus Other."""
    import re
    from pathlib import Path

    from sectors import PREDEFINED_SECTORS

    html = Path("templates/screener.html").read_text(encoding="utf-8")
    select_match = re.search(
        r'<select[^>]*id="screenerSector"[^>]*>([\s\S]*?)</select>',
        html,
    )
    assert select_match is not None
    option_values = set(re.findall(r'<option\s+value="([^"]+)"', select_match.group(1)))
    for sector_name in set(PREDEFINED_SECTORS.values()) | {"Other"}:
        assert sector_name in option_values, f"{sector_name} missing from #screenerSector"


def test_fe02_and_fe03_frontend_contracts_in_node() -> None:
    """FE-02 & FE-03: Verify polling sleep skip, news noise filter, TSE 130A fallback, idx FX strip, and AI portfolio case normalization."""
    from tests.test_support import run_node_script

    script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const ts = require("typescript");

function extractFn(source, name) {
  const sf = ts.createSourceFile("src.js", source, ts.ScriptTarget.Latest, true);
  let matched = null;
  function visit(node) {
    if (matched) return;
    if (ts.isFunctionDeclaration(node) && node.name && node.name.text === name) {
      matched = source.slice(node.getStart(sf), node.getEnd());
      return;
    }
    ts.forEachChild(node, visit);
  }
  visit(sf);
  if (!matched) throw new Error(`Missing ${name}`);
  return matched;
}

(async () => {
  const apiSrc = fs.readFileSync("static/js/api.js", "utf8");
  const aiDiveSrc = fs.readFileSync("static/js/experimental/ai-dive-controller.js", "utf8");
  const indexSrc = fs.readFileSync("static/js/index_main.js", "utf8");
  const aiPfSrc = fs.readFileSync("static/js/ai_portfolio.js", "utf8");

  // 1. chatPollingReply & requestStockAnalysis skip final trailing sleep
  let chatSleeps = 0;
  let analyzeSleeps = 0;
  const pollCtx = {
    CHAT_POLL_MAX_ATTEMPTS: 2,
    CHAT_POLL_INTERVAL_MS: 2000,
    ANALYZE_POLL_MAX_ATTEMPTS: 2,
    ANALYZE_POLL_INTERVAL_MS: 2000,
    apiFetch: async () => ({ response: { ok: true, status: 200 }, data: { fetching: true } }),
    abortableSleep: async (ms) => {
      if (ms === 2000) chatSleeps++;
    },
    Error,
  };
  vm.createContext(pollCtx);
  vm.runInContext(
    extractFn(apiSrc, "chatPollingReply") +
      "\nthis.chatPollingReply = chatPollingReply;",
    pollCtx,
  );
  await assert.rejects(() => pollCtx.chatPollingReply({}, {}), /タイムアウト/);
  assert.strictEqual(chatSleeps, 2, `Expected 2 sleeps across 3 attempts, got ${chatSleeps}`);

  const analyzeCtx = {
    ANALYZE_POLL_MAX_ATTEMPTS: 2,
    ANALYZE_POLL_INTERVAL_MS: 3000,
    HAS_MISTRAL_API_KEY: true,
    getStockByKey: () => ({ symbol: "AAPL", market: "us", name: "Apple", price: 190 }),
    createRequestToken: () => "req-token-1",
    apiFetch: async () => ({ response: { ok: true, status: 200 }, data: { fetching: true } }),
    abortableSleep: async () => {
      analyzeSleeps++;
    },
    Error,
    Math,
  };
  vm.createContext(analyzeCtx);
  vm.runInContext(
    extractFn(apiSrc, "requestStockAnalysis") +
      "\nthis.requestStockAnalysis = requestStockAnalysis;",
    analyzeCtx,
  );
  await assert.rejects(() => analyzeCtx.requestStockAnalysis("us:AAPL"), /タイムアウト/);
  assert.strictEqual(analyzeSleeps, 2, `Expected 2 sleeps across 3 attempts, got ${analyzeSleeps}`);

  // 2. AiDiveController polling loop has attempt < maxAttempts guard
  assert(
    aiDiveSrc.includes("if (attempt < maxAttempts) {\n            await this.waitForNewsRetry(2000, abortController.signal);\n          }") ||
      aiDiveSrc.includes("if (attempt < maxAttempts)"),
    "ai-dive-controller.js must guard trailing retry sleep",
  );

  // 3. _parseNewsItems filters noise lines inside bracket string-split fallback
  const noiseCtx = {};
  vm.createContext(noiseCtx);
  vm.runInContext(
    [
      extractFn(apiSrc, "_isMetadataLine"),
      extractFn(apiSrc, "_isNoiseLine"),
      extractFn(apiSrc, "_flattenStructuredItem"),
      extractFn(apiSrc, "_parseNewsItems"),
      "this._parseNewsItems = _parseNewsItems;",
    ].join("\n"),
    noiseCtx,
  );
  const parsedNews = noiseCtx._parseNewsItems(
    "['Good headline', 'source: spam', 'https://example.com', 'Second headline']",
  );
  assert.deepStrictEqual(Array.from(parsedNews), ["Good headline", "Second headline"]);

  // 4. sendChat JP fallback matches alphanumeric TSE ticker 130A
  assert(apiSrc.includes("/^[0-9][A-Z0-9]{3}$/.test(resolvedSymbol.toUpperCase())"));

  // 5. index_main.js portfolio save strips avg_fx_rate on idx even with USD currency
  assert(indexSrc.includes('const canTrackAvgFx = String(market || "").toLowerCase() === "us";'));

  // 6. ai_portfolio.js normalizes item.market case when looking up state.stocks
  const pfCtx = {
    state: { stocks: { jp: [{ symbol: "7203.T", price: 3000 }] } },
    normalizeSymbolForMarketClient: (sym, mkt) =>
      String(mkt || "").toLowerCase() === "jp" && /^[0-9][A-Z0-9]{3}$/.test(sym) ? `${sym}.T` : sym,
  };
  vm.createContext(pfCtx);
  vm.runInContext(
    [
      extractFn(aiPfSrc, "normalizePortfolioLookupSymbol"),
      extractFn(aiPfSrc, "findStockInState"),
      "this.findStockInState = findStockInState;",
    ].join("\n"),
    pfCtx,
  );
  assert.strictEqual(pfCtx.findStockInState("7203", "JP")?.price, 3000);
})();
"""
    run_node_script(script, context="2026-11-26 unit4 frontend contracts")
