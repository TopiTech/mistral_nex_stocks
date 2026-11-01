# tests/test_code_review_audit_2026_11_v1.py
"""Verification tests for code review audit fixes (November 2026).

Covers:
1. routes.stocks.common._json_safe handling of set and frozenset collections.
2. services.ai_service and services.openai_service tool value json-safety with sets.
3. utils.validators.StockPayloadDict type definition parity with build_stock_payload.
4. routes.stocks.views.api_screener parameter validation contract after dead-code cleanup.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from routes.stocks.common import _json_safe
from services.ai_service import call_mistral_chat_with_tools
from services.openai_service import call_openai_chat_with_tools
from utils.validators import StockPayloadDict


def test_json_safe_set_and_frozenset_serialization() -> None:
    """Verify that sets and frozensets are serialized as sorted lists recursively."""
    input_data: dict[str, Any] = {
        "tags": {"zebra", "apple", "mango"},
        "frozen": frozenset([3, 1, 2]),
        "nested": {
            "inner_set": {float("nan"), 10.5, float("inf")},
        },
    }

    result = _json_safe(input_data)

    # Sets should be converted to sorted lists
    assert result["tags"] == ["apple", "mango", "zebra"]
    assert result["frozen"] == [1, 2, 3]

    # Non-finite floats in set should be converted to None, and sorted
    inner_list = result["nested"]["inner_set"]
    assert isinstance(inner_list, list)
    assert None in inner_list
    assert 10.5 in inner_list
    assert len(inner_list) == 3


def test_ai_service_tool_value_json_safe_set() -> None:
    """Verify ai_service._json_safe_tool_value handles set and frozenset correctly."""
    first_response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "get_stock_quote",
                                "arguments": '{"symbol": "AAPL"}',
                            },
                        }
                    ],
                }
            }
        ]
    }
    final_response = {
        "choices": [{"message": {"role": "assistant", "content": "Analysis complete"}}]
    }

    tool_result_with_sets = {
        "symbol": "AAPL",
        "categories": {"tech", "finance"},
        "tags": frozenset(["us", "mega_cap"]),
    }

    with (
        patch(
            "services.ai_service.call_mistral_chat",
            side_effect=[first_response, final_response],
        ) as mock_chat,
        patch(
            "services.ai_tools.execute_mistral_tool_call",
            return_value=tool_result_with_sets,
        ),
    ):
        result = call_mistral_chat_with_tools(
            "test-key", [{"role": "user", "content": "Investigate AAPL"}]
        )

    assert "choices" in result
    assert result["choices"][0]["message"]["content"] == "Analysis complete"

    # Verify that the tool message injected into the second call prompt parsed cleanly as JSON
    second_call_messages = mock_chat.call_args_list[1][0][1]
    tool_message = next(m for m in second_call_messages if m.get("role") == "tool")
    parsed_tool_content = json.loads(tool_message["content"])
    assert parsed_tool_content["categories"] == ["finance", "tech"]
    assert parsed_tool_content["tags"] == ["mega_cap", "us"]


def test_openai_service_tool_value_json_safe_set() -> None:
    """Verify openai_service._json_safe_tool_value handles set and frozenset correctly."""
    first_response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {
                                "name": "get_stock_quote",
                                "arguments": '{"symbol": "MSFT"}',
                            },
                        }
                    ],
                }
            }
        ]
    }
    final_response = {"choices": [{"message": {"role": "assistant", "content": "OpenAI done"}}]}

    tool_result_with_sets = {
        "symbol": "MSFT",
        "related": {"GOOGL", "AAPL"},
    }

    with (
        patch(
            "services.openai_service.call_openai_chat",
            side_effect=[first_response, final_response],
        ) as mock_chat,
        patch(
            "services.openai_service.execute_mistral_tool_call",
            return_value=tool_result_with_sets,
        ),
    ):
        result = call_openai_chat_with_tools(
            "test-key", [{"role": "user", "content": "Investigate MSFT"}]
        )

    assert "choices" in result
    assert result["choices"][0]["message"]["content"] == "OpenAI done"

    second_call_messages = mock_chat.call_args_list[1][0][1]
    tool_message = next(m for m in second_call_messages if m.get("role") == "tool")
    parsed_tool_content = json.loads(tool_message["content"])
    assert parsed_tool_content["related"] == ["AAPL", "GOOGL"]


def test_stock_payload_dict_conformance() -> None:
    """Verify that StockPayloadDict accepts all standard fields emitted by build_stock_payload."""
    payload: StockPayloadDict = {
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "price": 150.0,
        "change": 1.5,
        "change_pct": 1.01,
        "change_percent": 1.01,
        "volume": 1000000,
        "market_cap": 2500000000000.0,
        "pe_ratio": 28.5,
        "high_52w": 180.0,
        "low_52w": 120.0,
        "sector": "Technology",
        "industry": "Consumer Electronics",
        "currency": "USD",
        "market": "us",
        "is_open": True,
        "sparkline": [148.0, 149.0, 150.0],
        "tv_symbol": "NASDAQ:AAPL",
        "market_state": "REGULAR",
        "previous_close": 148.5,
        "chart_data": [{"time": 1000, "close": 150.0}],
        "ohlc_data": [{"time": 1000, "open": 149.0, "high": 151.0, "low": 148.5, "close": 150.0}],
        "high": 151.0,
        "low": 148.5,
        "open": 149.0,
        "exchange": "NASDAQ",
        "snapshot_ts_ms": 1700000000000,
        "shares": 10.0,
        "avg_price": 140.0,
        "avg_fx_rate": 150.0,
        "portfolio_value": 1500.0,
        "portfolio_pl": 100.0,
    }

    assert payload["change_percent"] == 1.01
    assert payload["market_state"] == "REGULAR"
    assert payload["previous_close"] == 148.5


def test_api_screener_validation_contract(client) -> None:
    """Verify that api_screener correctly validates parameters using Pydantic schema."""
    # Invalid min_price should return 400
    res = client.get("/api/screener?min_price=invalid")
    assert res.status_code == 400
    data = res.get_json()
    assert data["ok"] is False
    assert data["error_code"] == 1005

    # Non-finite float should return 400
    res_inf = client.get("/api/screener?min_price=inf")
    assert res_inf.status_code == 400
    data_inf = res_inf.get_json()
    assert data_inf["ok"] is False

    # Valid screener request should return 200
    res_ok = client.get("/api/screener?market=all&limit=10")
    assert res_ok.status_code == 200
    data_ok = res_ok.get_json()
    assert data_ok["ok"] is True
    assert "stocks" in data_ok
