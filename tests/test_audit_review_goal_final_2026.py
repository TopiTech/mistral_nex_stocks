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

