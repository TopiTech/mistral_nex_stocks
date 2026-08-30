"""
Unit tests for Mistral Native Tool Calling engine (services/ai_tools.py).
"""

from unittest.mock import patch

from services.ai_service import call_mistral_chat_with_tools
from services.ai_tools import MISTRAL_FINANCIAL_TOOLS, execute_mistral_tool_call


def test_mistral_financial_tools_schema():
    """Verify tool schemas meet Mistral API function calling format."""
    assert isinstance(MISTRAL_FINANCIAL_TOOLS, list)
    assert len(MISTRAL_FINANCIAL_TOOLS) == 4

    tool_names = [t["function"]["name"] for t in MISTRAL_FINANCIAL_TOOLS]
    assert "get_stock_quote" in tool_names
    assert "get_company_fundamentals" in tool_names
    assert "get_market_news" in tool_names
    assert "calculate_technical_levels" in tool_names

    for t in MISTRAL_FINANCIAL_TOOLS:
        assert t["type"] == "function"
        assert "description" in t["function"]
        assert "parameters" in t["function"]
        assert t["function"]["parameters"]["type"] == "object"


@patch("utils.stock_payload.get_stock_info_cached")
def test_execute_get_stock_quote(mock_get_info):
    """Test get_stock_quote tool execution."""
    mock_get_info.return_value = {
        "name": "NVIDIA Corporation",
        "price": 128.50,
        "change": 3.20,
        "change_pct": 2.55,
        "volume": 45000000,
        "high": 130.00,
        "low": 125.10,
        "open": 126.00,
        "currency": "USD",
    }

    res = execute_mistral_tool_call("get_stock_quote", {"symbol": "NVDA", "market": "us"})
    assert res["symbol"] == "NVDA"
    assert res["price"] == 128.50
    assert res["currency"] == "USD"


@patch("utils.stock_payload.get_stock_info_cached")
def test_execute_get_company_fundamentals(mock_get_info):
    """Test get_company_fundamentals tool execution."""
    mock_get_info.return_value = {
        "name": "Microsoft Corporation",
        "sector": "Technology",
        "industry": "Software - Infrastructure",
        "market_cap": 3100000000000,
        "pe_ratio": 34.5,
        "dividend_yield": 0.75,
        "eps": 11.8,
    }

    res = execute_mistral_tool_call("get_company_fundamentals", {"symbol": "MSFT", "market": "us"})
    assert res["symbol"] == "MSFT"
    assert res["sector"] == "Technology"
    assert res["pe_ratio"] == 34.5


@patch("trend_sources.collect_market_news_items_fast")
def test_execute_get_market_news(mock_collect):
    """Test get_market_news tool execution."""
    mock_collect.return_value = [
        {"title": "半導体市場が好調", "snippet": "AI需要が牽引", "source": "Nikkei"},
    ]

    res = execute_mistral_tool_call("get_market_news", {"query": "半導体", "limit": 3})
    assert res["query"] == "半導体"
    assert res["count"] == 1
    assert res["news"][0]["title"] == "半導体市場が好調"


def test_execute_unknown_tool():
    """Test unknown tool name returns error dict safely."""
    res = execute_mistral_tool_call("unknown_tool_name", {})
    assert res == {"error": "未対応のツールです"}


@patch("utils.stock_payload.get_stock_info_cached")
def test_tool_error_does_not_expose_provider_diagnostics(mock_get_info):
    """Tool exceptions are logged, not supplied to the model as result content."""
    mock_get_info.side_effect = RuntimeError("provider trace=private-789 api_key=should-not-leak")

    res = execute_mistral_tool_call("get_stock_quote", {"symbol": "AAPL"})

    assert res["symbol"] == "AAPL"
    assert res["error"] == "株価情報の取得に失敗しました"
    assert "private-789" not in str(res)
    assert "should-not-leak" not in str(res)


def test_tool_dispatch_error_does_not_expose_diagnostics():
    """Invalid tool arguments must return the same generic failure envelope."""
    res = execute_mistral_tool_call("get_market_news", {"query": "AAPL", "limit": "not-an-int"})

    assert res == {"error": "ツールの実行に失敗しました"}


def test_tool_loop_does_not_reflect_unhandled_tool_error():
    """An unexpected tool failure must stay out of the model's next prompt."""
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
    final_response = {"choices": [{"message": {"role": "assistant", "content": "Unavailable"}}]}

    with patch(
        "services.ai_service.call_mistral_chat",
        side_effect=[first_response, final_response],
    ) as mock_chat, patch(
        "services.ai_tools.execute_mistral_tool_call",
        side_effect=RuntimeError("private-tool-trace"),
    ):
        call_mistral_chat_with_tools(
            "test-key", [{"role": "user", "content": "AAPL を調べて"}]
        )

    tool_content = mock_chat.call_args_list[1][0][1][-1]["content"]
    assert "private-tool-trace" not in tool_content
    assert "ツールの実行に失敗しました" in tool_content
