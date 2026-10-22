"""
tests/test_code_review_goal_audit_2026_10_v7.py

Regression tests for comprehensive code review audit 2026-10 v7:
1. AIPortfolioResponseSchema and AiPortfolioItemSchema / AiPortfolioResponseSchema symmetry.
2. schemas.__all__ export completeness for AIPortfolioResponseSchema & aliases.
3. utils.validators AIPortfolioItemSchema & AIPortfolioResponseSchema exports.
4. routes/api_system credentials validation for openai_api_key (min length, no whitespace, whitespace stripping).
5. routes/api_system credentials verify unified error format (code, error_code, valid, error, message).
6. AIPortfolioCopyToMyRequest schema validation constraints and duplicate detection.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

import schemas
import utils.validators
from app import create_app
from error_codes import ErrorCode
from schemas import (
    AIPortfolioCopyToMyRequest,
    AIPortfolioItemSchema,
    AiPortfolioItemSchema,
    AIPortfolioResponseSchema,
    AiPortfolioResponseSchema,
)


def test_ai_portfolio_response_schema_validation_and_aliases() -> None:
    """Verify AIPortfolioResponseSchema parses valid payloads and enforces constraints."""
    valid_payload: dict[str, Any] = {
        "title": "AI Growth Portfolio",
        "description": "High growth US & JP tech portfolio",
        "risk_level": "中〜高リスク",
        "expected_return": "10-15%",
        "commentary": "Solid fundamentals with strong momentum.",
        "items": [
            {
                "symbol": "AAPL",
                "name": "Apple Inc.",
                "market": "us",
                "weight_pct": 50.0,
                "target_price": 250.0,
                "rationale": "Ecosystem lock-in and services expansion.",
                "risk_level": "mid",
            },
            {
                "symbol": "7203",
                "name": "Toyota Motor",
                "market": "jp",
                "weight_pct": 50.0,
                "target_price": 3200.0,
                "rationale": "Global hybrid leadership.",
                "risk_level": "low",
            },
        ],
    }

    resp = AIPortfolioResponseSchema.model_validate(valid_payload)
    assert resp.title == "AI Growth Portfolio"
    assert len(resp.items) == 2
    assert resp.items[0].symbol == "AAPL"
    assert resp.items[1].symbol == "7203"

    # Aliases in schemas
    assert AiPortfolioItemSchema is AIPortfolioItemSchema
    assert AiPortfolioResponseSchema is AIPortfolioResponseSchema

    # Exports in utils.validators
    assert hasattr(utils.validators, "AIPortfolioItemSchema")
    assert hasattr(utils.validators, "AIPortfolioResponseSchema")

    # Exports in schemas.__all__
    assert "AIPortfolioResponseSchema" in schemas.__all__
    assert "AiPortfolioItemSchema" in schemas.__all__
    assert "AiPortfolioResponseSchema" in schemas.__all__

    # Empty items rejection
    invalid_empty = dict(valid_payload)
    invalid_empty["items"] = []
    with pytest.raises(ValidationError):
        AIPortfolioResponseSchema.model_validate(invalid_empty)


def test_ai_portfolio_copy_to_my_request_schema() -> None:
    """Verify AIPortfolioCopyToMyRequest schema enforces unique items and total weight constraints."""
    valid_items = [
        {"symbol": "MSFT", "market": "us", "weight_pct": 40.0, "target_price": 450.0},
        {"symbol": "NVDA", "market": "us", "weight_pct": 60.0, "target_price": 140.0},
    ]
    req = AIPortfolioCopyToMyRequest.model_validate({"items": valid_items})
    assert len(req.items) == 2

    # Reject duplicate symbols
    dup_items = [
        {"symbol": "MSFT", "market": "us", "weight_pct": 40.0},
        {"symbol": "msft", "market": "us", "weight_pct": 30.0},
    ]
    with pytest.raises(ValidationError) as exc_info:
        AIPortfolioCopyToMyRequest.model_validate({"items": dup_items})
    assert "Duplicate stock" in str(exc_info.value)

    # Reject weight exceeding 100.5%
    overweight_items = [
        {"symbol": "MSFT", "market": "us", "weight_pct": 60.0},
        {"symbol": "AAPL", "market": "us", "weight_pct": 50.0},
    ]
    with pytest.raises(ValidationError) as exc_info2:
        AIPortfolioCopyToMyRequest.model_validate({"items": overweight_items})
    assert "exceeds 100%" in str(exc_info2.value)


def test_openai_api_key_validation_in_credentials_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify POST /api/credentials strips and validates openai_api_key."""
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False

    saved_keys: dict[str, Any] = {}

    def _mock_save(**kwargs: Any) -> None:
        saved_keys.update(kwargs)

    monkeypatch.setattr("routes.api_system.save_api_credentials", _mock_save)

    with app.test_client() as client:
        # 1. Reject too short key (< 8 chars)
        res = client.post(
            "/api/credentials",
            json={"openai_api_key": "short"},
            headers={"Origin": "http://localhost:5000"},
        )
        assert res.status_code == 400
        data = res.get_json()
        assert data["error_code"] == ErrorCode.UNSAFE_INPUT.value
        assert "openai_api_key" in data.get("details", {}).get("fields", [])

        # 2. Reject key with spaces inside
        res_space = client.post(
            "/api/credentials",
            json={"openai_api_key": "sk-key with space 1234"},
            headers={"Origin": "http://localhost:5000"},
        )
        assert res_space.status_code == 400
        data_space = res_space.get_json()
        assert data_space["error_code"] == ErrorCode.UNSAFE_INPUT.value

        # 3. Accept valid key and strip leading/trailing whitespace
        valid_key = "  sk-test-valid-openai-key-2026-xyz  "
        res_valid = client.post(
            "/api/credentials",
            json={"openai_api_key": valid_key},
            headers={"Origin": "http://localhost:5000"},
        )
        assert res_valid.status_code == 200
        assert saved_keys.get("openai_api_key") == valid_key.strip()


def test_api_credentials_verify_normalized_error_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify /api/credentials/verify returns normalized error contract (code, error_code, valid=False)."""
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False

    monkeypatch.setattr("credential_manager.get_mistral_api_key", lambda: None)

    with app.test_client() as client:
        # POST with no key configured and no key passed
        res = client.post(
            "/api/credentials/verify",
            json={},
            headers={"Origin": "http://localhost:5000"},
        )
        assert res.status_code == 400
        data = res.get_json()
        assert data["ok"] is False
        assert data["valid"] is False
        assert data["code"] == "INVALID_API_KEY"
        assert data["error_code"] == ErrorCode.INVALID_API_KEY.value
        assert "error" in data
        assert data["message"] == data["error"]
