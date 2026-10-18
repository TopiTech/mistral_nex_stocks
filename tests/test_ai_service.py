"""Tests for services/ai_service.py."""

from unittest.mock import MagicMock, patch

from services.ai_service import generate_ai_technical_lines


def test_generate_ai_technical_lines_circuit_breaker_mistral(monkeypatch):
    monkeypatch.setattr("services.ai_service.is_openai_provider", lambda: False)

    mock_is_open = MagicMock(side_effect=lambda name: name == "mistral")
    monkeypatch.setattr("services.ai_service.app_state.market.is_circuit_open", mock_is_open)

    dummy_ohlc = [{"x": 1700000000000, "o": 100, "h": 105, "l": 95, "c": 102}]
    res = generate_ai_technical_lines("key", "AAPL", "us", "1mo", dummy_ohlc)
    assert "error" in res
    assert "Mistral API" in res["error"]
    assert "サーキットブレーカー" in res["error"]
    mock_is_open.assert_called_with("mistral")


def test_generate_ai_technical_lines_circuit_breaker_openai(monkeypatch):
    monkeypatch.setattr("services.ai_service.is_openai_provider", lambda: True)

    mock_is_open = MagicMock(side_effect=lambda name: name == "openai_compatible")
    monkeypatch.setattr("services.ai_service.app_state.market.is_circuit_open", mock_is_open)

    dummy_ohlc = [{"x": 1700000000000, "o": 100, "h": 105, "l": 95, "c": 102}]
    res = generate_ai_technical_lines("key", "AAPL", "us", "1mo", dummy_ohlc)
    assert "error" in res
    assert "OpenAI Compatible API" in res["error"]
    assert "サーキットブレーカー" in res["error"]
    mock_is_open.assert_called_with("openai_compatible")


def test_generate_ai_technical_lines_circuit_closed(monkeypatch):
    monkeypatch.setattr("services.ai_service.is_openai_provider", lambda: False)

    mock_is_open = MagicMock(return_value=False)
    monkeypatch.setattr("services.ai_service.app_state.market.is_circuit_open", mock_is_open)

    with patch("services.ai_service.call_mistral_chat") as mock_chat:
        mock_chat.return_value = {
            "choices": [
                {
                    "message": {
                        "parsed": {
                            "summary": "Bullish trend",
                            "trend_bias": "Bullish",
                            "lines": [],
                        }
                    }
                }
            ]
        }
        dummy_ohlc = [{"x": 1700000000000, "o": 100, "h": 105, "l": 95, "c": 102}]
        res = generate_ai_technical_lines("key", "AAPL", "us", "1mo", dummy_ohlc)
        assert res.get("summary") == "Bullish trend"
        assert res.get("trend_bias") == "Bullish"
