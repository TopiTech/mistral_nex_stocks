"""
Tests for final comprehensive audit review fixes (2026).
"""

import json
import math
from unittest.mock import MagicMock

import numpy as np
import pandas as pd

from services.ai_tools import _tool_calculate_technical_levels, _tool_get_market_news
from services.embeddings_service import cosine_similarity
from services.stock_provider import sanitize_fundamental_dict
from utils.stock_payload import _finite_or_none


def test_cosine_similarity_edge_cases():
    # Standard orthogonal and identical vectors
    assert math.isclose(cosine_similarity([1.0, 0.0], [1.0, 0.0]), 1.0)
    assert math.isclose(cosine_similarity([1.0, 0.0], [0.0, 1.0]), 0.0)
    assert math.isclose(cosine_similarity([1.0, 0.0], [-1.0, 0.0]), -1.0)

    # Empty and mismatched lengths
    assert cosine_similarity([], []) == 0.0
    assert cosine_similarity([1.0], [1.0, 2.0]) == 0.0

    # Zero vectors
    assert cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0
    assert cosine_similarity([0.0, 0.0], [0.0, 0.0]) == 0.0

    # NaN / Inf in vector components must return 0.0 and NEVER propagate NaN
    nan_vec = [float("nan"), 1.0]
    inf_vec = [float("inf"), 1.0]
    valid_vec = [1.0, 1.0]

    res_nan = cosine_similarity(nan_vec, valid_vec)
    assert res_nan == 0.0
    assert math.isfinite(res_nan)

    res_inf = cosine_similarity(inf_vec, valid_vec)
    assert res_inf == 0.0
    assert math.isfinite(res_inf)

    # Clamping guarantee: must never exceed [-1.0, 1.0]
    res_clamp = cosine_similarity([1.000000000000001, 1.0], [1.000000000000001, 1.0])
    assert -1.0 <= res_clamp <= 1.0

    # Booleans in vectors must return 0.0 and never coerce to 1.0 or 0.0
    assert cosine_similarity([True, 1.0], [1.0, 1.0]) == 0.0
    assert cosine_similarity([1.0, False], [1.0, 1.0]) == 0.0
    assert cosine_similarity([True, False], [True, False]) == 0.0


def test_finite_or_none_rejects_booleans():
    # Standard finite numbers
    assert _finite_or_none(123.45) == 123.45
    assert _finite_or_none("456.78") == 456.78
    assert _finite_or_none(0) == 0.0

    # Booleans MUST be rejected as None, never coerced to 1.0 or 0.0
    assert _finite_or_none(True) is None
    assert _finite_or_none(False) is None

    # NaN / Inf / invalid values
    assert _finite_or_none(float("nan")) is None
    assert _finite_or_none(float("inf")) is None
    assert _finite_or_none(float("-inf")) is None
    assert _finite_or_none("invalid") is None
    assert _finite_or_none(None) is None

    # Negative bounds
    assert _finite_or_none(-10.5, allow_negative=False) is None
    assert _finite_or_none(-10.5, allow_negative=True) == -10.5


def test_normalization_number_helpers_reject_booleans():
    from utils.normalization import _fmt, _fmt_vol, normalize_optional_number

    # normalize_optional_number
    assert normalize_optional_number(123.45) == 123.45
    assert normalize_optional_number(True) is None
    assert normalize_optional_number(False) is None
    assert normalize_optional_number(None) is None
    assert normalize_optional_number(float("nan")) is None
    assert normalize_optional_number(pd.NA) is None

    # _fmt
    assert _fmt(123.456) == 123.46
    assert _fmt(True) is None
    assert _fmt(False) is None
    assert _fmt(None) is None
    assert _fmt(float("nan")) is None
    assert _fmt(float("inf")) is None
    assert _fmt(pd.NA) is None

    # _fmt_vol
    assert _fmt_vol(12345.6) == 12345
    assert _fmt_vol(True) is None
    assert _fmt_vol(False) is None
    assert _fmt_vol(None) is None
    assert _fmt_vol(float("nan")) is None
    assert _fmt_vol(float("inf")) is None
    assert _fmt_vol(pd.NA) is None


def test_json_safe_pandas_na():
    from routes.stocks.common import _json_safe

    # pd.NA and pd.NaT must be safely converted to None for json serialization
    data = {
        "na": pd.NA,
        "nat": pd.NaT,
        "valid": 123.45,
        "bool": True,
        "nested": {"na": pd.NA, "list": [1.0, pd.NA, float("nan")]},
    }
    safe_data = _json_safe(data)
    assert safe_data["na"] is None
    assert safe_data["nat"] is None
    assert safe_data["valid"] == 123.45
    assert safe_data["bool"] is True
    assert safe_data["nested"]["na"] is None
    assert safe_data["nested"]["list"] == [1.0, None, None]

    # json.dumps must succeed without error
    encoded = json.dumps(safe_data)
    assert "null" in encoded


def test_sanitize_fundamental_dict_pandas_na():
    raw_data = {
        "valid_float": 12.34,
        "valid_str": "tech",
        "nan_float": float("nan"),
        "inf_float": float("inf"),
        "pd_na": pd.NA,
        "pd_nat": pd.NaT,
        "np_nan": np.nan,
        "bool_val": True,
        "none_val": None,
        "clean_list": [1.0, 2.0],
        "dirty_list": [1.0, pd.NA, float("nan")],
    }

    clean = sanitize_fundamental_dict(raw_data)

    assert "valid_float" in clean
    assert clean["valid_float"] == 12.34
    assert "valid_str" in clean
    assert clean["valid_str"] == "tech"
    assert clean["clean_list"] == [1.0, 2.0]

    # Dropped / filtered keys
    assert "nan_float" not in clean
    assert "inf_float" not in clean
    assert "pd_na" not in clean
    assert "pd_nat" not in clean
    assert "np_nan" not in clean
    assert "bool_val" not in clean
    assert "none_val" not in clean

    # Ensure clean output is strictly JSON serializable without allowing NaN
    dumped = json.dumps(clean, allow_nan=False)
    loaded = json.loads(dumped)
    assert loaded["valid_float"] == 12.34


def test_tool_get_market_news_limit_parsing(monkeypatch):
    import trend_sources

    # Mock collect_market_news_items_fast to return dummy items
    monkeypatch.setattr(
        trend_sources,
        "collect_market_news_items_fast",
        lambda market: [{"title": "AI stocks surge", "link": "http://example.com", "source": "src"}],
    )

    # Valid int limit
    res1 = _tool_get_market_news({"query": "AI stocks", "limit": 3})
    assert "news" in res1
    assert res1["count"] == 1

    # String limit
    res2 = _tool_get_market_news({"query": "AI stocks", "limit": "2"})
    assert "news" in res2

    # None limit defaults to 5
    res3 = _tool_get_market_news({"query": "AI stocks", "limit": None})
    assert "news" in res3


def test_tool_calculate_technical_levels_trend_bias(monkeypatch):
    dates = pd.date_range("2026-01-01", periods=60, freq="D")
    # Upward trending series
    closes = [100.0 + i for i in range(60)]
    df = pd.DataFrame(
        {
            "Open": closes,
            "High": [c + 1.0 for c in closes],
            "Low": [c - 1.0 for c in closes],
            "Close": closes,
            "Volume": [1000] * 60,
        },
        index=dates,
    )

    mock_ticker = MagicMock()
    mock_ticker.history.return_value = df

    import utils.market_utils as mu

    monkeypatch.setattr(mu, "safe_get_ticker", lambda symbol: mock_ticker)

    res = _tool_calculate_technical_levels({"symbol": "NVDA", "period": "3mo"})
    assert res["symbol"] == "NVDA"
    assert res["trend_bias"] == "Bullish"
    assert res["sma_20"] is not None
    assert res["sma_50"] is not None


def test_generate_ai_technical_lines_validation(monkeypatch):
    # Mock chat parse response with valid and invalid line items
    raw_response = {
        "summary": "テクニカル分析サマリー",
        "trend_bias": "Bullish",
        "lines": [
            {
                "id": "line_1",
                "type": "support",
                "label": "有効な支持線",
                "start_price": 100.0,
                "end_price": 105.0,
                "color": "#00ff88",
                "style": "solid",
            },
            {
                "id": "line_invalid_zero",
                "type": "resistance",
                "label": "ゼロ価格",
                "start_price": 0.0,
                "end_price": 100.0,
            },
            {
                "id": "line_invalid_negative",
                "type": "resistance",
                "label": "負の価格",
                "start_price": -50.0,
                "end_price": 100.0,
            },
            {
                "id": "line_invalid_bool",
                "type": "resistance",
                "label": "ブール値価格",
                "start_price": True,
                "end_price": 100.0,
            },
            {
                "id": "line_invalid_nan",
                "type": "resistance",
                "label": "NaN価格",
                "start_price": float("nan"),
                "end_price": 100.0,
            },
        ],
    }

    sample_history = [
        {"x": 1700000000000, "o": 100.0, "h": 105.0, "l": 98.0, "c": 102.0}
    ]

    import services.ai_service as ais

    monkeypatch.setattr(ais, "call_mistral_chat", lambda *args, **kwargs: raw_response)

    result = ais.generate_ai_technical_lines(
        "mock_key_01234567890123456789012345678901",
        "NVDA",
        "us",
        "3mo",
        sample_history,
    )
    assert "lines" in result
    # Only the valid line should be retained
    assert len(result["lines"]) == 1
    assert result["lines"][0]["id"] == "line_1"
    assert result["lines"][0]["start_price"] == 100.0
    assert result["lines"][0]["end_price"] == 105.0


def test_merge_quote_into_history_timezones():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from services.stock_provider import YFinanceProvider

    provider = YFinanceProvider()

    # Tokyo morning: 2026-06-15 09:30 JST is 2026-06-15 00:30 UTC = 2026-06-14 20:30 EDT
    tokyo_dt = datetime(2026, 6, 15, 9, 30, tzinfo=ZoneInfo("Asia/Tokyo"))
    market_time_sec = int(tokyo_dt.timestamp())

    df = pd.DataFrame(
        {"Close": [38000.0], "Open": [38000.0], "High": [38100.0], "Low": [37900.0], "Volume": [1000000]},
        index=pd.DatetimeIndex(["2026-06-12"]),
    )

    quote = {
        "regularMarketPrice": 38500.0,
        "regularMarketTime": market_time_sec,
        "regularMarketVolume": 500000,
        "regularMarketOpen": 38200.0,
        "regularMarketDayHigh": 38600.0,
        "regularMarketDayLow": 38100.0,
    }

    # For Japanese index ^N225, date must be 2026-06-15 (JST), NOT 2026-06-14 (EDT)
    merged_n225 = provider._merge_quote_into_history(df, quote, "^N225")
    assert not merged_n225.empty
    assert merged_n225.index[-1].strftime("%Y-%m-%d") == "2026-06-15"

    # For bare JP digit symbol 7203, date must be 2026-06-15 (JST)
    merged_7203 = provider._merge_quote_into_history(df, quote, "7203")
    assert merged_7203.index[-1].strftime("%Y-%m-%d") == "2026-06-15"

    # For US symbol AAPL, 2026-06-15 00:30 UTC is 2026-06-14 (EDT)
    merged_aapl = provider._merge_quote_into_history(df, quote, "AAPL")
    assert merged_aapl.index[-1].strftime("%Y-%m-%d") == "2026-06-14"


def test_ai_portfolio_rebalance_and_cache_invalidation():
    from unittest.mock import patch

    from app import app
    from routes.api_stocks import ai_portfolio_fetch_lock, ai_portfolio_result_cache

    orig_csrf = app.config.get("WTF_CSRF_ENABLED")
    app.config["WTF_CSRF_ENABLED"] = False
    try:
        sample_portfolio = {
            "id": "custom-theme-test-1",
            "title": "Test Portfolio",
            "theme": "ai_robotics_theme",
            "risk": "mid",
            "expected_return": "10-15%",
            "commentary": "Solid AI portfolio",
            "items": [
                {"symbol": "NVDA", "market": "us", "weight_pct": 50.0, "target_price": 150.0},
                {"symbol": "6758.T", "market": "jp", "weight_pct": 50.0, "target_price": 13000.0},
            ],
        }

        with (
            patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)),
            patch("routes.stocks.ai_portfolio.require_trusted_or_admin", return_value=(True, None)),
            patch("services.ai_portfolio_service.generate_ai_portfolio_by_theme", return_value=sample_portfolio),
            patch("routes.api_stocks.generate_ai_portfolio_by_theme", return_value=sample_portfolio),
            patch("routes.stocks.ai_portfolio.generate_ai_portfolio_by_theme", return_value=sample_portfolio),
            patch("services.ai_portfolio_service.save_custom_ai_portfolio", return_value=True),
            patch("routes.api_stocks.save_custom_ai_portfolio", return_value=True),
            patch("routes.stocks.ai_portfolio.save_custom_ai_portfolio", return_value=True),
            patch("services.ai_portfolio_service.delete_custom_ai_portfolio", return_value=True),
            patch("routes.api_stocks.delete_custom_ai_portfolio", return_value=True),
            patch("routes.stocks.ai_portfolio.delete_custom_ai_portfolio", return_value=True),
            patch("route_helpers._submit_in_app_context", lambda executor, fn, app=None: fn()),
            patch("routes.api_stocks._submit_in_app_context", lambda executor, fn, app=None: fn()),
        ):
            client = app.test_client()

            # 1. Rebalance updates the generate cache key with the newly rebalanced portfolio
            res = client.post("/api/ai-portfolio/rebalance", json={"theme": "ai_robotics_theme"})
            assert res.status_code == 200
            data = res.get_json()
            assert data["ok"] is True
            with ai_portfolio_fetch_lock:
                gen_keys = [k for k in ai_portfolio_result_cache if "generate:" in k and "ai_robotics_theme" in k]
                assert len(gen_keys) >= 1
                cached_data = ai_portfolio_result_cache[gen_keys[0]][1]
                assert cached_data["title"] == "Test Portfolio"

            # 2. Save invalidates cached entries for that theme/id
            res_save = client.post("/api/ai-portfolio/save", json={"portfolio": sample_portfolio})
            assert res_save.status_code == 200
            with ai_portfolio_fetch_lock:
                matching_keys = [k for k in ai_portfolio_result_cache if "ai_robotics_theme" in k or "custom-theme-test-1" in k]
                assert len(matching_keys) == 0

            # 3. Populate a dummy cache entry for delete invalidation test
            with ai_portfolio_fetch_lock:
                ai_portfolio_result_cache["generate:default:custom-theme-test-1"] = (100.0, sample_portfolio, None)

            # 4. Delete invalidates cached entries for that id
            res_del = client.delete("/api/ai-portfolio/custom", json={"id": "custom-theme-test-1"})
            assert res_del.status_code == 200
            with ai_portfolio_fetch_lock:
                matching_del = [k for k in ai_portfolio_result_cache if "custom-theme-test-1" in k]
                assert len(matching_del) == 0
    finally:
        app.config["WTF_CSRF_ENABLED"] = orig_csrf


def test_js_formatters_defend_against_null_and_boolean():
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        return

    root = Path(__file__).resolve().parent.parent
    script = r'''
const fs = require("fs");
const vm = require("vm");

const ctx = {
  APP_CONFIG: { has_mistral_api_key: false },
  document: {
    documentElement: {},
    addEventListener: () => {},
    removeEventListener: () => {},
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => ({ setAttribute: () => {}, addEventListener: () => {} }),
  },
  addEventListener: () => {},
  removeEventListener: () => {},
  localStorage: { getItem: () => null, setItem: () => {} },
  getComputedStyle: () => ({ getPropertyValue: () => "" }),
};
ctx.window = ctx;
ctx.global = ctx;
vm.createContext(ctx);

vm.runInContext(fs.readFileSync("static/js/state.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("static/js/experimental/data-adapter.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("static/js/chart.js", "utf8"), ctx);

const da = ctx.ObservatoryDataAdapter;

// Test data-adapter formatPrice
if (da.formatPrice(null, "jp") !== "--") throw new Error("da.formatPrice(null) != '--'");
if (da.formatPrice(true, "jp") !== "--") throw new Error("da.formatPrice(true) != '--'");
if (da.formatPrice(false, "jp") !== "--") throw new Error("da.formatPrice(false) != '--'");
if (da.formatPrice("", "jp") !== "--") throw new Error("da.formatPrice('') != '--'");
if (da.formatPrice(150, "jp") !== "¥150") throw new Error("da.formatPrice(150) != '¥150'");

// Test data-adapter formatMarketCap
if (da.formatMarketCap(null, "jp") !== "--") throw new Error("da.formatMarketCap(null) != '--'");
if (da.formatMarketCap(true, "jp") !== "--") throw new Error("da.formatMarketCap(true) != '--'");
if (da.formatMarketCap(false, "jp") !== "--") throw new Error("da.formatMarketCap(false) != '--'");

// Test chart.js formatPrice
if (ctx.formatPrice(null, "jp") !== "¥--") throw new Error("chart.formatPrice(null) != '¥--'");
if (ctx.formatPrice(true, "jp") !== "¥--") throw new Error("chart.formatPrice(true) != '¥--'");
if (ctx.formatPrice(false, "jp") !== "¥--") throw new Error("chart.formatPrice(false) != '¥--'");
if (ctx.formatPrice("", "jp") !== "¥--") throw new Error("chart.formatPrice('') != '¥--'");
if (ctx.formatPrice(2500, "jp") !== "¥2,500") throw new Error("chart.formatPrice(2500) != '¥2,500'");

// Test calculateHeikinAshi with price-only / close-only series (must not produce NaN)
const priceOnlyData = [
  { price: 100, date: "2026-01-01" },
  { price: 105, date: "2026-01-02" },
  { price: 102, date: "2026-01-03" },
];
const haRes = ctx.calculateHeikinAshi(priceOnlyData);
if (haRes.length !== 3) throw new Error("haRes length != 3");
for (const candle of haRes) {
  if (isNaN(candle.o) || isNaN(candle.h) || isNaN(candle.l) || isNaN(candle.c)) {
    throw new Error("calculateHeikinAshi produced NaN for price-only data: " + JSON.stringify(candle));
  }
}

// Test calculateBollingerBands, calculateRSI, calculateMACD
const sampleSeries = Array.from({ length: 30 }, (_, i) => ({ price: 100 + i }));
const bb = ctx.calculateBollingerBands(sampleSeries, 20);
if (!bb.middle || bb.middle.length !== 30) throw new Error("bb.middle length != 30");
if (isNaN(bb.middle[25]) || isNaN(bb.upper[25]) || isNaN(bb.lower[25])) {
  throw new Error("calculateBollingerBands produced NaN");
}

const rsi = ctx.calculateRSI(sampleSeries, 14);
if (!rsi || rsi.length !== 30) throw new Error("rsi length != 30");
if (isNaN(rsi[25])) throw new Error("calculateRSI produced NaN");

const macd = ctx.calculateMACD(sampleSeries, 12, 26, 9);
if (!macd.macdLine || macd.macdLine.length !== 30) throw new Error("macd length != 30");

// Test ui.js formatDrawerPrice
vm.runInContext(fs.readFileSync("static/js/ui.js", "utf8"), ctx);
if (ctx.formatDrawerPrice({ price: true }) !== "--") throw new Error("formatDrawerPrice(true) != '--'");
if (ctx.formatDrawerPrice({ price: false }) !== "--") throw new Error("formatDrawerPrice(false) != '--'");
if (ctx.formatDrawerPrice({ price: null }) !== "--") throw new Error("formatDrawerPrice(null) != '--'");
// Test screener formatCurrency & formatMarketCap logic
const screenerCode = fs.readFileSync("static/js/screener.js", "utf8");
if (!screenerCode.includes('typeof val === "boolean"')) {
  throw new Error("screener.js lacks boolean guard");
}
if (!screenerCode.includes("e.isComposing || e.keyCode === 229")) {
  throw new Error("screener.js lacks IME guard");
}

// Test heatmap formatNumber & formatCompact logic
const heatmapCode = fs.readFileSync("static/js/heatmap.js", "utf8");
if (!heatmapCode.includes('typeof value === "boolean"')) {
  throw new Error("heatmap.js lacks boolean guard");
}

// Test utils.js & ui.js modal keydown IME guards
const utilsCode = fs.readFileSync("static/js/utils.js", "utf8");
if (!utilsCode.includes("event.isComposing || event.keyCode === 229")) {
  throw new Error("utils.js lacks IME guard in modal._keydownHandler");
}

const uiCode = fs.readFileSync("static/js/ui.js", "utf8");
if (!uiCode.includes("e.isComposing || e.keyCode === 229")) {
  throw new Error("ui.js lacks IME guard");
}

process.stdout.write("ok");
'''
    res = subprocess.run(
        [node, "-"],
        cwd=root,
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert res.returncode == 0, res.stderr
    assert "ok" in res.stdout


