"""Market news tool must match multi-word queries instead of only verbatim phrases.

``_tool_get_market_news`` used to test ``query.lower() in title/snippet`` as one
contiguous substring. Any ordinary search phrase ("トヨタ 決算", "Apple earnings",
a bare ticker code) therefore matched nothing while the collector did return
headlines, and the tool answered with a well-formed ``count: 0`` that a model
presents as "no news available". The cases below pin the token-based contract:
individual query words may be spread across the headline and its snippet, and a
genuinely unmatched query must say so rather than fabricate relevance.
"""

from unittest.mock import MagicMock, patch

import pytest

from services.ai_tools import _tool_get_market_news


def _items(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [
        {"title": title, "snippet": snippet, "source": "Reuters"}
        for title, snippet in pairs
    ]


def _call(query: str, items: list[dict[str, str]], **args) -> dict:
    with patch("trend_sources.collect_market_news_items_fast", return_value=items):
        return _tool_get_market_news({"query": query, **args})


def test_multi_word_japanese_query_matches_spread_tokens():
    items = _items(
        ("トヨタ自動車、通期決算を上方修正", "市場予想を上回る"),
        ("無関係な見出し", "別の話題"),
    )

    result = _call("トヨタ 決算", items)

    assert result["count"] == 1
    assert result["matched_query"] is True
    assert result["news"][0]["title"] == "トヨタ自動車、通期決算を上方修正"
    assert "note" not in result


def test_multi_word_english_query_matches_title_and_snippet_separately():
    items = _items(("Apple beats estimates", "Analysts expected an earnings surprise"))

    result = _call("Apple earnings", items)

    assert result["count"] == 1
    assert result["matched_query"] is True


def test_single_token_match_is_case_insensitive():
    items = _items(("NVDA hits record high", "Data center demand"))

    result = _call("nvda", items)

    assert result["count"] == 1
    assert result["matched_query"] is True


def test_unmatched_query_reports_no_match_instead_of_fabricating_candidates():
    items = _items(
        ("金利据え置きを決定", "中央銀行の発表"),
        ("原油価格が反発", "需給の引き締まり"),
    )

    result = _call("7203", items)

    assert result["count"] == 0
    assert result["news"] == []
    assert result["matched_query"] is False
    assert result["checked_count"] == 2
    assert "見出しはありませんでした" in result["note"]
    # No unrelated headline may be substituted for a missing match.
    assert all("原油" not in entry["title"] for entry in result["news"])


def test_empty_headline_list_reports_zero_without_note():
    result = _call("トヨタ 決算", [])

    assert result["count"] == 0
    assert result["matched_query"] is False
    assert "checked_count" not in result
    assert "note" not in result


def test_limit_is_applied_after_token_matching():
    items = _items(*[(f"決算発表 {idx}", "トヨタ関連") for idx in range(6)])

    result = _call("トヨタ 決算", items, limit=2)

    assert result["count"] == 2
    assert result["matched_query"] is True


def test_snippet_only_match_is_accepted():
    items = _items(("市場概況", "トヨタの決算が話題"))

    result = _call("トヨタ 決算", items)

    assert result["count"] == 1
    assert result["matched_query"] is True


@pytest.mark.parametrize("query", ["  ", "\u3000\t"])
def test_blank_query_is_rejected(query: str):
    result = _call(query, _items(("見出し", "本文")))

    assert result == {"error": "query is required"}


def test_object_items_are_supported():
    item = MagicMock()
    item.title = "トヨタ自動車が決算を発表"
    item.snippet = "通期見通しを上方修正"
    item.source = "Nikkei"

    with patch("trend_sources.collect_market_news_items_fast", return_value=[item]):
        result = _tool_get_market_news({"query": "トヨタ 決算"})

    assert result["count"] == 1
    assert result["news"][0]["source"] == "Nikkei"


def test_collector_failure_keeps_error_contract():
    with patch(
        "trend_sources.collect_market_news_items_fast",
        side_effect=RuntimeError("boom"),
    ):
        result = _tool_get_market_news({"query": "トヨタ 決算"})

    assert result["query"] == "トヨタ 決算"
    assert result["news"] == []
    assert result["error"] == "ニュース情報の取得に失敗しました"
