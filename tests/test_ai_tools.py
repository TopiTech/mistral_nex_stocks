"""Tests for services/ai_tools.py."""

from unittest.mock import MagicMock

from services.ai_tools import _tool_get_market_news


def test_tool_get_market_news_invalid_limit(monkeypatch):
    import trend_sources

    # Mock collect_market_news_items_fast to return dummy items
    dummy_item = MagicMock()
    dummy_item.title = "AI stocks news update"
    dummy_item.snippet = "Market is reacting to AI developments"
    dummy_item.source = "Reuters"

    monkeypatch.setattr(
        trend_sources,
        "collect_market_news_items_fast",
        lambda market: [dummy_item] * 10,
    )

    # Test cases for invalid / non-integer limits:
    for invalid_limit in ["max", "all", {}, [], object(), "invalid"]:
        res = _tool_get_market_news({"query": "AI stocks", "limit": invalid_limit})
        assert "news" in res, f"Failed for limit={invalid_limit}: {res}"
        # Default fallback limit is 5
        assert res["count"] <= 5

    # Test cases for None limit (defaults to 5)
    res_none = _tool_get_market_news({"query": "AI stocks", "limit": None})
    assert "news" in res_none
    assert res_none["count"] <= 5

    # Test cases for numeric strings and bounds
    res_str = _tool_get_market_news({"query": "AI stocks", "limit": "3"})
    assert "news" in res_str
    assert res_str["count"] == 3

    # Clamped limits (min 1, max 10)
    res_zero = _tool_get_market_news({"query": "AI stocks", "limit": 0})
    assert "news" in res_zero
    assert res_zero["count"] == 1

    res_huge = _tool_get_market_news({"query": "AI stocks", "limit": 999})
    assert "news" in res_huge
    assert res_huge["count"] <= 10
