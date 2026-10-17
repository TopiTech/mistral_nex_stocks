"""tests/test_openai_analyze_and_error_handling.py
Tests for OpenAI-compatible API structured stock analysis parsing and reinforced error handling.
Verifies that:
1. English / loose output from OpenAI-compatible models is reliably normalized to Japanese standard format.
2. Target price strings ($150, 150円, etc.) are cleaned to numeric floats.
3. Provider errors return proper HTTP error status codes (401, 429, 502, 503, 504) instead of HTTP 200.
4. Cached error results also return proper HTTP error status instead of HTTP 200.
5. JSON repair with LLM uses json_object when OpenAI provider is active.
"""

import json
from unittest.mock import patch

import httpx
import pytest

from app import create_app
from credential_manager import (
    PROVIDER_MISTRAL,
    PROVIDER_OPENAI,
    clear_api_credentials,
    save_api_credentials,
)
from services.ai_service import _repair_json_with_llm
from utils.validators import (
    _clean_numeric_price,
    _normalize_analysis_aliases,
    safe_parse_analysis_result,
    validate_analysis_result,
)


@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    return app


@pytest.fixture
def client(app):
    with app.test_client() as client:
        yield client


class TestOpenAiNormalization:
    """Tests for normalizing OpenAI-compatible model outputs."""

    def test_clean_numeric_price(self):
        assert _clean_numeric_price(150) == 150.0
        assert _clean_numeric_price(150.75) == 150.75
        assert _clean_numeric_price("$150.50") == 150.50
        assert _clean_numeric_price("¥15,000") == 15000.0
        assert _clean_numeric_price("150.00 USD") == 150.0
        assert _clean_numeric_price("約 200 円") == 200.0
        assert _clean_numeric_price(None) is None
        assert _clean_numeric_price(True) is None
        assert _clean_numeric_price("N/A") is None

    def test_normalize_analysis_aliases_english_values(self):
        raw_output = {
            "summary": "AI半導体需要の拡大が業績を力強く牽引している。",
            "recommendation": "BUY",
            "sentiment": "BULLISH",
            "confidence": "HIGH",
            "target_price": "$185.50",
            "upside_3m": "+15%",
            "catalysts": "新世代GPUの投入\nクラウド事業者の設備投資増",
            "risks": "米中貿易摩擦\n競合他社の台頭",
        }
        normalized = _normalize_analysis_aliases(raw_output)

        assert normalized["analysis_summary"] == "AI半導体需要の拡大が業績を力強く牽引している。"
        assert normalized["recommendation"] == "買い"
        assert normalized["sentiment"] == "強気"
        assert normalized["confidence"] == "高"
        assert normalized["target_price_3m"] == 185.50
        assert isinstance(normalized["key_catalysts"], list)
        assert len(normalized["key_catalysts"]) == 2
        assert "新世代GPUの投入" in normalized["key_catalysts"]
        assert isinstance(normalized["risk_factors"], list)
        assert len(normalized["risk_factors"]) == 2

    def test_validate_analysis_result_with_string_target_price(self):
        result = {
            "analysis_summary": "良好な業績",
            "recommendation": "買い",
            "sentiment": "強気",
            "target_price_3m": "$250.00",
        }
        valid, reason = validate_analysis_result(result)
        assert valid is True
        assert reason == ""

    def test_safe_parse_analysis_result_with_openai_dict_response(self):
        openai_response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": json.dumps({
                            "recommendation": "Strong Buy",
                            "sentiment": "Positive",
                            "confidence": "Mid",
                            "target_price_3m": "320.00",
                            "upside_3m": "+20%",
                            "analysis_summary": "収益性が大幅に改善しており中長期で成長期待。",
                            "key_catalysts": ["新製品投入", "営業利益率向上"],
                            "risk_factors": ["為替リスク"],
                        }),
                    }
                }
            ]
        }
        parsed = safe_parse_analysis_result(openai_response, "dummy_key")
        assert parsed["parsed"] is True
        assert parsed["recommendation"] == "強い買い"
        assert parsed["sentiment"] == "強気"
        assert parsed["confidence"] == "中"
        assert parsed["target_price_3m"] == 320.0


class TestAnalyzeV2ErrorHandling:
    """Tests that /api/analyze-v2 returns appropriate HTTP error codes instead of 200."""

    def setup_method(self):
        save_api_credentials(
            ai_provider=PROVIDER_OPENAI,
            openai_base_url="https://api.openai.com/v1",
            openai_model="gpt-4o-mini",
            openai_api_key="sk-test-valid-key-12345",
        )

    def teardown_method(self):
        clear_api_credentials()
        save_api_credentials(ai_provider=PROVIDER_MISTRAL)

    def test_provider_error_returns_status_code_not_200(self, client):
        """When OpenAI provider returns rate-limit or upstream error, /api/analyze-v2 must return non-200."""
        provider_err_resp = {
            "error": {
                "message": "Rate limit reached for requests",
                "status_code": 429,
                "provider": "openai_compatible",
            }
        }

        with (
            patch("routes.api_analysis.fetch_stock", return_value={"price": 100.0, "chart_data": []}),
            patch("routes.api_analysis.get_stock_info_cached", return_value={}),
            patch("routes.api_analysis.collect_symbol_research_context", return_value=""),
            patch("routes.api_analysis.call_mistral_chat", return_value=provider_err_resp),
        ):
            res = client.post(
                "/api/analyze-v2",
                json={
                    "symbol": "AAPL",
                    "market": "us",
                    "request_token": "token-test-1234567890-abcdefg",
                },
                headers={"Origin": "http://localhost:5000"},
            )

        assert res.status_code == 429
        data = res.get_json()
        assert "error" in data
        assert "Rate limit" in data.get("details", {}).get("reason", "")

    def test_provider_500_error_returns_502(self, client):
        """When provider returns 500/502, /api/analyze-v2 must return 500 or 502 instead of 200."""
        provider_err_resp = {
            "error": {
                "message": "Internal Server Error from upstream",
                "status_code": 502,
                "provider": "openai_compatible",
            }
        }

        with (
            patch("routes.api_analysis.fetch_stock", return_value={"price": 100.0, "chart_data": []}),
            patch("routes.api_analysis.get_stock_info_cached", return_value={}),
            patch("routes.api_analysis.collect_symbol_research_context", return_value=""),
            patch("routes.api_analysis.call_mistral_chat", return_value=provider_err_resp),
        ):
            res = client.post(
                "/api/analyze-v2",
                json={
                    "symbol": "AAPL",
                    "market": "us",
                    "request_token": "token-test-1234567890-502error",
                },
                headers={"Origin": "http://localhost:5000"},
            )

        assert res.status_code == 502
        data = res.get_json()
        assert "error" in data

    def test_network_connect_error_returns_503(self, client):
        """When connection to OpenAI-compatible server fails, /api/analyze-v2 must return 503 instead of 200."""
        with (
            patch("routes.api_analysis.fetch_stock", return_value={"price": 100.0, "chart_data": []}),
            patch("routes.api_analysis.get_stock_info_cached", return_value={}),
            patch("routes.api_analysis.collect_symbol_research_context", return_value=""),
            patch("routes.api_analysis.call_mistral_chat", side_effect=httpx.ConnectError("Connection refused")),
        ):
            res = client.post(
                "/api/analyze-v2",
                json={
                    "symbol": "AAPL",
                    "market": "us",
                    "request_token": "token-test-1234567890-neterr",
                },
                headers={"Origin": "http://localhost:5000"},
            )

        assert res.status_code == 503
        data = res.get_json()
        assert "error" in data

    def test_cached_error_result_returns_non_200(self, client):
        """When error result is cached, re-polling must return proper error HTTP status, not 200."""
        from routes.api_analysis import analyze_result_cache

        test_token = "token-test-cached-error-token-12345"
        session_scope = "test_conversation_scope_12345"
        cache_key = f"analyze:{session_scope}:{test_token}"

        error_result = {
            "parsed": False,
            "provider_error": {
                "message": "Model overloaded",
                "status_code": 503,
            },
        }
        analyze_result_cache[cache_key] = (100.0, error_result, None)

        with client.session_transaction() as sess:
            sess["mns_analysis_conversation"] = session_scope

        res = client.post(
            "/api/analyze-v2",
            json={
                "symbol": "AAPL",
                "market": "us",
                "request_token": test_token,
            },
            headers={"Origin": "http://localhost:5000"},
        )

        assert res.status_code == 503
        data = res.get_json()
        assert "error" in data
        assert "Model overloaded" in data.get("details", {}).get("reason", "")


class TestJsonRepairWithOpenAi:
    """Tests for _repair_json_with_llm under OpenAI provider."""

    def test_repair_json_with_llm_uses_json_object_for_openai(self):
        with (
            patch("services.ai_service.is_openai_provider", return_value=True),
            patch("services.ai_service.call_mistral_chat") as mock_chat,
        ):
            mock_chat.return_value = {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"test_field": "val"}),
                        }
                    }
                ]
            }

            _repair_json_with_llm(
                api_key="sk-test",
                raw_content="malformed json",
                schema_name="test_schema",
                schema={"type": "object", "properties": {"test_field": {"type": "string"}}},
                required_fields=["test_field"],
                max_tokens=500,
                cache_key_override="test_cache_key",
                fallback={},
            )

            assert mock_chat.called
            _, kwargs = mock_chat.call_args
            assert kwargs.get("response_format") == {"type": "json_object"}
