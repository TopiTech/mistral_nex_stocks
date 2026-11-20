"""Residual review regressions (2026-11-20).

Covers two remaining defects found while re-reviewing the API boundary:

* The technical-line normalizer rejected the model-emitted ``trendline``
  spelling and silently relabelled it as ``support`` (green support line),
  even though the frontend already renders ``trendline`` as a blue trend line.
* ``sanitize_provider_error_info`` claimed to expose only scalar diagnostics
  but copied any value under an allowlisted key, including nested dicts/lists.
"""

from unittest.mock import patch

from services.ai_service import generate_ai_technical_lines
from utils.validators import sanitize_provider_error_info


def _dummy_ohlc() -> list[dict[str, float]]:
    return [
        {"x": 1700000000000 + i * 86400000, "o": 100 + i, "h": 105 + i, "l": 99 + i, "c": 102 + i}
        for i in range(5)
    ]


def _parsed_response(lines: list[dict]) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "parsed": {
                        "summary": "AI Technical Analysis",
                        "trend_bias": "Bullish",
                        "lines": lines,
                    }
                }
            }
        ]
    }


def test_technical_lines_maps_trendline_alias_to_trend(monkeypatch):
    """``trendline`` must survive as the canonical blue ``trend`` type."""
    monkeypatch.setattr("services.ai_service.is_openai_provider", lambda: False)
    monkeypatch.setattr(
        "services.ai_service.app_state.market.is_circuit_open", lambda _name: False
    )

    response = _parsed_response(
        [
            {
                "id": "line_1",
                "type": "trendline",
                "label": "上昇トレンドライン",
                "start_price": 100.0,
                "end_price": 120.0,
            },
            {
                "id": "line_2",
                "type": "trend_line",
                "label": "下降トレンドライン",
                "start_price": 130.0,
                "end_price": 110.0,
            },
            {
                "id": "line_3",
                "type": "pivot",
                "label": "未対応の種類",
                "start_price": 90.0,
                "end_price": 95.0,
            },
        ]
    )

    with patch("services.ai_service.call_mistral_chat", return_value=response):
        result = generate_ai_technical_lines("key", "AAPL", "us", "1mo", _dummy_ohlc())

    assert [line["type"] for line in result["lines"]] == ["trend", "trend", "support"]
    # The default color must follow the canonical type, not the rejected spelling.
    assert result["lines"][0]["color"] == "#3399ff"
    assert result["lines"][1]["color"] == "#3399ff"
    # Unknown types keep the pre-existing safe fallback.
    assert result["lines"][2]["color"] == "#00ff88"


def test_provider_error_info_drops_non_scalar_diagnostics():
    sanitized = sanitize_provider_error_info(
        {
            "message": "upstream rate limit",
            "status_code": 429,
            "code": {"echo": {"api_key": "sk-leak-value"}},
            "type": ["request", "dump"],
            "retry_after": "12",
        }
    )

    assert sanitized["message"] == "upstream rate limit"
    assert sanitized["status_code"] == 429
    assert sanitized["retry_after"] == "12"
    assert "code" not in sanitized
    assert "type" not in sanitized
    assert "sk-leak-value" not in str(sanitized)


def test_provider_error_info_sanitizes_and_caps_string_diagnostics():
    sanitized = sanitize_provider_error_info(
        {
            "message": "boom",
            "code": "api_key=sk-" + "a" * 40,
            "type": "x" * 500,
        }
    )

    # Sensitive scalar diagnostics are dropped entirely.
    assert "code" not in sanitized
    # Non-sensitive strings are retained but bounded.
    assert sanitized["type"] == "x" * 200
