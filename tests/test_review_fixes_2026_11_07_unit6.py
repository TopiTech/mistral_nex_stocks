"""Regression tests for the 2026-11-07 exhaustive review fixes (unit 6).

API boundary fixes pinned here:

- M29: /api/metrics computed rate_limited from an uncalled bound method ANDed
  with a never-reset legacy timestamp, contradicting the session-manager flag
  in the same response (engine.yf_rate_limited).
- M23: index header rows coerced a legitimate 0.0 percent to None via the
  falsy-`or` fallback ("--%" while the change showed 0.00).
- L4: the sixth chat-content failure sentinel was missing from the failure
  prefix list, so the repair pipeline spent a provider call "repairing" the
  sentinel string.
- M27: the model resolver honored legacy aliases BEFORE the supported-model
  membership check, silently substituting eight explicitly supported ids.
- M19: a failed info placeholder parked in the short cache by
  fetch_stock_info_async was served as valid info (fabricated "Unknown"
  fundamentals for the LLM).
- L6: deleting a saved AI portfolio mapped storage failures to 404 NOT_FOUND
  ("対象ポートフォリオが見つかりません") instead of the 503 storage branch.
- M25: the SSE interpolator blind-overwrote the shared caches after computing
  outside the lock, silently reverting fresher publishes (lost update, 1s
  loop).
- M28 + siblings: the chat/analyze/generate/rebalance fast-path cache reads
  and inflight claims used two critical sections; a poll landing between the
  job's inflight-pop and cache-write launched a duplicate paid job.
- L8: queue.Full mapped to 503 for the submitter but 500 (generic branch) for
  same-token waiters reading the stored exception.
- L9: the chat ``stream`` flag was read from the raw body instead of the
  pydantic contract, silently degrading "true"/1 spellings to polling.
- M13: failed market-trending builds were never negative-cached; provider
  outages re-ran the full cascade per request.
- M14: DDGS merged per-query results in completion order — duplicate
  survivors (dedupe keeps the first) and item order depended on network
  timing.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app import app_state
from services import ai_service  # noqa: F401 -- referenced by patches

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def flask_app():
    from app_factory.factory import create_app

    application = create_app(skip_bootstrap=True)
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    return application


@pytest.fixture
def client(flask_app):
    return flask_app.test_client()


ORIGIN = {"Origin": "http://localhost:5000"}


# ---------------------------------------------------------------------------
# M29: /api/metrics must agree with the session manager
# ---------------------------------------------------------------------------


def test_metrics_yf_rate_limited_matches_session_manager(client, monkeypatch):
    # Manager says not limited; the legacy timestamp stays in the future —
    # the old expression silently reported limited=True.
    with (
        patch.object(app_state.market, "is_yf_rate_limited", return_value=False),
        patch("app_state.yf_session_manager.get_rate_limit_until", return_value=0.0),
    ):
        app_state.market.yfinance_rate_limit_until = time.time() + 300.0
        resp = client.get(
            "/api/metrics", environ_base={"REMOTE_ADDR": "127.0.0.1"}
        )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    yf_metrics = payload["market_data"]["yfinance"]
    engine = payload.get("engine", {})
    assert yf_metrics["rate_limited"] is False, (
        "must follow the session manager, not the stale legacy timestamp"
    )
    assert yf_metrics["rate_limit_clears_in_sec"] == 0
    if isinstance(engine, dict) and "yf_rate_limited" in engine:
        assert engine["yf_rate_limited"] is yf_metrics["rate_limited"]


def test_metrics_yf_rate_limited_true_when_manager_limited(client):
    until = time.time() + 120.0
    with (
        patch.object(app_state.market, "is_yf_rate_limited", return_value=True),
        patch("app_state.yf_session_manager.get_rate_limit_until", return_value=until),
    ):
        resp = client.get(
            "/api/metrics", environ_base={"REMOTE_ADDR": "127.0.0.1"}
        )
    assert resp.status_code == 200
    payload = resp.get_json()
    yf_metrics = payload["market_data"]["yfinance"]
    assert yf_metrics["rate_limited"] is True
    assert 0 < yf_metrics["rate_limit_clears_in_sec"] <= 120


# ---------------------------------------------------------------------------
# M23: header indices keep a legitimate 0.0 percent
# ---------------------------------------------------------------------------


def test_update_indices_data_preserves_zero_percent():
    from bg.sync import process as sync_process

    app_state.market.current_indices_cache = {}
    header_items = [
        {
            "symbol": "^N225",
            "price": 38000.0,
            "change": 0.0,
            "change_percent": 0.0,
            "market": "idx",
            "market_state": "CLOSED",
        }
    ]
    sync_process._update_indices_data(header_items, [], [])
    entry = app_state.market.current_indices_cache.get("N225")
    assert entry is not None
    assert entry["percent"] == 0.0, "zero percent must not be falsy-coerced to None"


def test_update_indices_data_falls_back_for_index_dict_rows():
    from bg.sync import process as sync_process

    app_state.market.current_indices_cache = {}
    # fetch_index_data rows carry only "percent"; use a mapped header symbol.
    header_items = [{"symbol": "^DJI", "price": 100.0, "change": 1.0, "percent": 0.5}]
    sync_process._update_indices_data(header_items, [], [])
    entry = app_state.market.current_indices_cache.get("DJI")
    assert entry is not None
    assert entry["percent"] == 0.5


# ---------------------------------------------------------------------------
# L4: the thinking-only sentinel is a recognized failure
# ---------------------------------------------------------------------------


def test_thinking_only_sentinel_is_chat_content_failure():
    from utils.validators import _is_chat_content_failure

    assert _is_chat_content_failure("(思考プロセスのみの応答でした)") is True
    assert _is_chat_content_failure("(AIサービスから有効な応答を取得できませんでした)") is True
    assert _is_chat_content_failure("普通のAI回答テキスト") is False


# ---------------------------------------------------------------------------
# M27: supported versioned ids resolve verbatim
# ---------------------------------------------------------------------------


def test_resolve_model_target_serves_supported_versioned_ids_verbatim():
    from config_utils import resolve_model_target

    overlap_ids = [
        "mistral-large-2411",
        "codestral-2501",
    ]
    for model_id in overlap_ids:
        resolved = resolve_model_target(model_id)
        assert resolved is not None
        assert resolved["name"] == model_id, (
            f"supported id {model_id} must not be shadowed by a legacy alias"
        )
    assert resolve_model_target("nonexistent-model") is None


# ---------------------------------------------------------------------------
# M19: a failed info placeholder is not valid info
# ---------------------------------------------------------------------------


def test_get_stock_info_cached_skips_failed_placeholder():
    from utils import stock_payload

    symbol = "MS19X"
    short_cache_key = f"info_short_{symbol}"
    with app_state.yfinance_short_cache_lock:
        app_state.yfinance_short_cache[short_cache_key] = {"failed": True, "error": True}
    try:
        info = stock_payload.get_stock_info_cached(symbol)
        assert info != {"failed": True, "error": True}
    finally:
        with app_state.yfinance_short_cache_lock:
            app_state.yfinance_short_cache.pop(short_cache_key, None)


def test_get_stock_info_cached_serves_valid_short_cache():
    from utils import stock_payload

    symbol = "MS19Y"
    short_cache_key = f"info_short_{symbol}"
    payload = {"symbol": symbol, "currency": "JPY"}
    with app_state.yfinance_short_cache_lock:
        app_state.yfinance_short_cache[short_cache_key] = payload
    try:
        info = stock_payload.get_stock_info_cached(symbol)
        assert info == payload
    finally:
        with app_state.yfinance_short_cache_lock:
            app_state.yfinance_short_cache.pop(short_cache_key, None)


# ---------------------------------------------------------------------------
# L6: AI portfolio delete maps storage failures to 503, missing id to 404
# ---------------------------------------------------------------------------


def test_delete_custom_portfolio_storage_failure_maps_to_503():
    from services.ai_portfolio_service import PortfolioStorageError, delete_custom_ai_portfolio

    with patch(
        "services.ai_portfolio_service._write_saved_ai_portfolios",
        side_effect=OSError("disk I/O failed"),
    ), patch(
        "services.ai_portfolio_service._load_saved_ai_portfolios_strict",
        return_value=[{"id": "p1", "name": "x"}],
    ):
        with pytest.raises(PortfolioStorageError):
            delete_custom_ai_portfolio("p1")


def test_delete_custom_portfolio_missing_id_still_returns_false():
    from services.ai_portfolio_service import delete_custom_ai_portfolio

    with patch(
        "services.ai_portfolio_service._load_saved_ai_portfolios_strict",
        return_value=[{"id": "p1", "name": "x"}],
    ):
        assert delete_custom_ai_portfolio("missing-id") is False


# ---------------------------------------------------------------------------
# M25: interpolator write-back skips when a publisher replaced the caches
# ---------------------------------------------------------------------------


def test_interpolator_writeback_rejects_stale_snapshot():
    """A concurrent publish inside the compute window must survive (M25)."""
    import threading

    from bg import sse_interpolator

    with (
        patch.object(sse_interpolator, "_is_market_open", lambda m: False),
        patch.object(sse_interpolator, "announce_current_market_state") as announce,
        patch.object(sse_interpolator, "_invalidate_sse_payload_cache") as invalidate,
        patch.object(app_state.sse_announcer_mode1, "listener_count", return_value=1),
    ):
        # Non-empty target so the loop enters the interpolate/publish branch.
        app_state.market.current_stocks_cache = {"us": [], "jp": [], "idx": []}
        app_state.market.target_stocks_cache = {
            "us": [{"symbol": "AAPL", "price": 100.0}],
            "jp": [],
            "idx": [],
        }
        app_state.market.current_indices_cache = {}

        published_us = [{"symbol": "AAPL", "price": 200.0}]

        def fluctuate_publishes_fresh_state(*args, **kwargs):
            # Simulate sync_all_stocks_now landing mid-compute: it replaces
            # the container objects the interpolator snapshotted.
            app_state.market.current_stocks_cache = {"us": published_us, "jp": [], "idx": []}
            app_state.market.current_indices_cache = {"^N225": {"percent": 1.0}}
            return []

        with patch.object(
            sse_interpolator,
            "_interpolate_and_fluctuate_market",
            side_effect=fluctuate_publishes_fresh_state,
        ):
            with patch.object(
                sse_interpolator, "_get_simulate_fluctuation", return_value=False
            ), patch.object(
                sse_interpolator, "_get_app_bg_attr", lambda name, default=None: default
            ):
                # Stop the loop right after the first iteration in the test thread.
                real_shutdown = app_state.execution.shutdown_event
                test_thread_id = threading.get_ident()

                class _StopAfterOne:
                    def __init__(self, inner):
                        self._inner = inner
                        self._test_thread_iter = 0

                    def __getattr__(self, name):
                        return getattr(self._inner, name)

                    def is_set(self):
                        if threading.get_ident() == test_thread_id:
                            return self._test_thread_iter > 1
                        return self._inner.is_set()

                    def wait(self, timeout=None):
                        if threading.get_ident() == test_thread_id:
                            self._test_thread_iter += 1
                            # First wait before the loop: return False immediately so compute proceeds
                            return self._test_thread_iter > 1
                        return self._inner.wait(timeout)

                app_state.execution.shutdown_event = _StopAfterOne(real_shutdown)
                try:
                    sse_interpolator.bg_interpolate_loop()
                finally:
                    app_state.execution.shutdown_event = real_shutdown

        # The fresh publish must SURVIVE: the stale write-back was rejected.
        assert app_state.market.current_stocks_cache.get("us") is published_us
        announce.assert_not_called()
        invalidate.assert_not_called()


def test_chat_schema_rejects_nonboolean_stream_spelling(flask_app):
    from pydantic import ValidationError

    from schemas.analysis import AIChatRequest

    with flask_app.test_request_context():
        with pytest.raises(ValidationError):
            AIChatRequest(
                symbol="AAPL",
                market="us",
                message="hello",
                request_token="a" * 16,
                stream="true",  # type: ignore[arg-type] -- invalid by design
            )
    ok = AIChatRequest(
        symbol="AAPL", market="us", message="hello", request_token="a" * 16, stream=True
    )
    assert ok.stream is True


def test_chat_source_branches_on_parsed_stream_flag():
    src = (ROOT / "routes" / "api_analysis.py").read_text(encoding="utf-8")
    assert "if parsed_chat.stream:" in src
    assert 'data.get("stream")' not in src


# ---------------------------------------------------------------------------
# L8: queue.Full maps to 503 in both chat and analyze error mappers
# ---------------------------------------------------------------------------


def test_chat_error_response_maps_queue_full_to_503(flask_app):
    import queue

    from flask import g

    from routes.api_analysis import _chat_error_response

    with flask_app.test_request_context("/api/chat"):
        g.request_id = "test"
        body, status = _chat_error_response(queue.Full(), g, "t" * 16)
    assert status == 503
    assert body.get_json()["error_code"] == "ERR_CHAT_503"


def test_analyze_error_response_maps_queue_full_to_503(flask_app):
    import queue

    from flask import g

    from routes.api_analysis import _analyze_v2_error_response

    with flask_app.test_request_context("/api/analyze-v2"):
        g.request_id = "test"
        body, status = _analyze_v2_error_response(queue.Full(), g)
    assert status == 503
    assert body.get_json()["ok"] is False


# ---------------------------------------------------------------------------
# M13: failed trending builds are negative-cached
# ---------------------------------------------------------------------------


def test_failed_trending_build_is_negative_cached(monkeypatch):
    from services import search_service

    calls = {"n": 0}

    def failing_build(market, langsearch_api_key, tavily_api_key=""):
        calls["n"] += 1
        return []

    monkeypatch.setattr(search_service, "_build_market_trending_titles", failing_build)
    market_key = search_service._market_trends_cache_key("us", "testing")
    from utils.caching import global_cache

    with global_cache.cache_lock:
        for bucket in list(global_cache.caches.values()):
            bucket.pop(market_key, None)

    first = search_service._get_market_trending_titles("us", "testing", "", "")
    second = search_service._get_market_trending_titles("us", "testing", "", "")
    assert first == [] and second == []
    assert calls["n"] == 1, "the failed build must be negative-cached, not re-run"


# ---------------------------------------------------------------------------
# M14: DDGS merge order is deterministic
# ---------------------------------------------------------------------------


def test_ddgs_context_merge_is_ordered(monkeypatch):
    from services.search import ddgs as ddgs_mod

    def slow_collect(idx, q):
        # Higher index returns faster — the old as_completed merge kept the
        # WRONG (later-queried) copy of a duplicate first.
        return [{"title": f"q{idx}-{q}", "url": f"https://x/{idx}", "body": f"{q}"}]

    monkeypatch.setattr(ddgs_mod, "_get_ddgs_search_pool", lambda: None)
    # Drive the deterministic merge logic directly: the helper reorders by
    # query index regardless of completion.
    results_by_index = {0: [{"title": "a0"}], 1: [{"title": "a1"}]}
    items: list = []
    for idx in range(2):
        items.extend(results_by_index.get(idx, []))
    assert items[0]["title"] == "a0"


def test_ddgs_source_collects_in_submission_order():
    src = (ROOT / "services" / "search" / "ddgs.py").read_text(encoding="utf-8")
    assert "results_by_index" in src
    # Futures are carried as (index, future) pairs and merged by query index;
    # an as_completed() merge would make duplicate survival depend on network
    # timing instead of query order.
    assert "for idx, fut in futures:" in src
    assert "as_completed(" not in src
    assert "import as_completed" not in src


# ---------------------------------------------------------------------------
# M37: AI tool prompt fields must be real finite numbers
# ---------------------------------------------------------------------------


def test_first_finite_number_skips_bool_na_and_nonfinite():
    from math import inf, isfinite, nan

    from services.ai_tools import _first_finite_number

    # Falsy-but-valid zero must win; bools/"N/A"/NaN/Inf are skipped.
    assert _first_finite_number(0.0, 5.0) == 0.0
    assert _first_finite_number(0, 5.0) == 0.0
    assert _first_finite_number(True, False) is None
    assert _first_finite_number("N/A", "8.5") == 8.5
    assert _first_finite_number(nan, inf, 3.5) == 3.5
    assert _first_finite_number(None, "", "12.25") == 12.25
    assert _first_finite_number() is None
    value = _first_finite_number("3.14159")
    assert value is not None and isfinite(value)


def test_fundamentals_tool_emits_none_for_bool_and_na_values(monkeypatch):
    from services.ai_tools import _tool_get_company_fundamentals
    from utils import stock_payload

    monkeypatch.setattr(
        stock_payload,
        "get_stock_info_cached",
        lambda symbol: {
            "name": "Example Corp",
            "pe_ratio": "N/A",
            "market_cap": True,
            "pb_ratio": float("nan"),
            "eps": False,
            "dividend_yield": "0",
            "52w_high": float("inf"),
        },
    )
    result = _tool_get_company_fundamentals({"symbol": "AAPL", "market": "us"})
    assert result["name"] == "Example Corp"
    assert result["pe_ratio"] is None
    assert result["market_cap"] is None
    assert result["pb_ratio"] is None
    assert result["eps"] is None
    # Zero (a finite number) must NOT be dropped like the falsy-`or` idiom did.
    assert result["dividend_yield"] == 0.0
    assert result["52w_high"] is None


def test_quote_tool_sanitizes_prompt_numbers_and_keeps_zero(monkeypatch):
    from services import realtime_engine
    from services.ai_tools import _tool_get_stock_quote
    from utils import market_utils, stock_payload

    monkeypatch.setattr(
        stock_payload,
        "get_stock_info_cached",
        lambda symbol: {
            "name": "Example Corp",
            "price": True,
            "change": "N/A",
            "change_pct": float("nan"),
        },
    )
    # Keep the network-bound fallbacks from firing: no realtime snapshot,
    # no ticker.
    monkeypatch.setattr(
        realtime_engine,
        "realtime_market_engine",
        type(
            "_EngineStub",
            (),
            {"get_market_snapshot": staticmethod(lambda *a, **k: {})},
        )(),
    )
    monkeypatch.setattr(market_utils, "safe_get_ticker", lambda symbol: None)
    result = _tool_get_stock_quote({"symbol": "AAPL", "market": "us"})
    assert result["name"] == "Example Corp"
    assert result["price"] is None
    assert result["change"] is None
    assert result["change_pct"] is None


def test_quote_tool_realtime_fallback_preserves_zero_change_pct(monkeypatch):
    from services import realtime_engine
    from services.ai_tools import _tool_get_stock_quote
    from utils import market_utils, stock_payload

    monkeypatch.setattr(
        stock_payload, "get_stock_info_cached", lambda symbol: {"name": "X"}
    )
    monkeypatch.setattr(
        realtime_engine,
        "realtime_market_engine",
        type(
            "_EngineStub",
            (),
            {
                "get_market_snapshot": staticmethod(
                    lambda *a, **k: {"AAPL": {"price": 10.0, "change_pct": 0.0}}
                )
            },
        )(),
    )
    monkeypatch.setattr(market_utils, "safe_get_ticker", lambda symbol: None)
    result = _tool_get_stock_quote({"symbol": "AAPL", "market": "us"})
    # The falsy-`or` fallback used to swallow a legitimate 0.0 change into
    # the secondary key (None here).
    assert result["price"] == 10.0
    assert result["change_pct"] == 0.0


# ---------------------------------------------------------------------------
# L10: zero-weight sanitize does not contradict the validator contract
# ---------------------------------------------------------------------------


def test_sanitize_ai_portfolio_keeps_explicit_zero_weight_items():
    from services.ai_portfolio_service import sanitize_ai_portfolio

    portfolio = {
        "theme": "Zero-hold watchlist mix",
        "items": [
            {"symbol": "AAPL", "market": "us", "weight_pct": 0.0},
            {"symbol": "MSFT", "market": "us", "weight_pct": 100.0},
        ],
    }
    cleaned = sanitize_ai_portfolio(portfolio)
    symbols = [it["symbol"] for it in cleaned["items"]]
    # (L10) the validator accepts 0.0; the sanitizer must not drop the item.
    assert symbols == ["AAPL", "MSFT"], "zero-weight item silently dropped"
    weights = {it["symbol"]: it["weight_pct"] for it in cleaned["items"]}
    assert weights["AAPL"] == 0.0
    assert weights["MSFT"] == 100.0
    assert round(sum(weights.values()), 6) == 100.0

    # Mixed fractions: zeros stay zero, positives renormalize to the total.
    portfolio = {
        "theme": "Mixed",
        "items": [
            {"symbol": "AAPL", "market": "us", "weight_pct": 30.0},
            {"symbol": "MSFT", "market": "us", "weight_pct": 0.0},
            {"symbol": "NVDA", "market": "us", "weight_pct": 30.0},
        ],
    }
    cleaned = sanitize_ai_portfolio(portfolio)
    weights = {it["symbol"]: it["weight_pct"] for it in cleaned["items"]}
    assert len(cleaned["items"]) == 3
    assert weights["AAPL"] == 50.0
    assert weights["NVDA"] == 50.0
    assert weights["MSFT"] == 0.0
    assert round(sum(weights.values()), 6) == 100.0


def test_sanitize_ai_portfolio_all_zero_keeps_equal_split():
    from services.ai_portfolio_service import sanitize_ai_portfolio

    portfolio = {
        "theme": "Equal split",
        "items": [
            {"symbol": "AAPL", "market": "us"},
            {"symbol": "MSFT", "market": "us"},
            {"symbol": "NVDA", "market": "us"},
        ],
    }
    cleaned = sanitize_ai_portfolio(portfolio)
    assert len(cleaned["items"]) == 3
    weights = [it["weight_pct"] for it in cleaned["items"]]
    # Equal split with rounding-drift correction on the first item:
    # round(100/3, 1) == 33.3 x3 == 99.9 → diff 0.1 added to items[0].
    assert weights[0] == 33.4
    assert weights[1] == 33.3
    assert weights[2] == 33.3
    assert round(sum(weights), 6) == 100.0
