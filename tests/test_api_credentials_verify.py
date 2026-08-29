"""
Unit & Route tests for POST /api/credentials/verify and credential state endpoints.
"""

from unittest.mock import MagicMock, patch

import pytest

from app import create_app


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as client:
        yield client


def test_api_credentials_verify_missing_key(client):
    """Test POST /api/credentials/verify with empty key returns 400."""
    res = client.post(
        "/api/credentials/verify",
        json={"mistral_api_key": ""},
        headers={"Origin": "http://localhost:5000"},
    )
    assert res.status_code == 400
    data = res.get_json()
    assert data["ok"] is False


@patch("mistral_compat.Mistral")
def test_api_credentials_verify_free_tier(mock_mistral_cls, client):
    """Test POST /api/credentials/verify with free tier model list."""
    mock_instance = MagicMock()
    mock_mistral_cls.return_value = mock_instance

    mock_m1 = MagicMock(id="mistral-small-2603")
    mock_m2 = MagicMock(id="ministral-8b-latest")
    mock_m3 = MagicMock(id="codestral-latest")
    mock_instance.models.list.return_value = MagicMock(data=[mock_m1, mock_m2, mock_m3])

    res = client.post(
        "/api/credentials/verify",
        json={"mistral_api_key": "test-valid-key"},
        headers={"Origin": "http://localhost:5000"},
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert data["tier"] == "free"
    assert data["is_free_tier"] is True
    assert data["recommended_model"] == "mistral-small-2603"


@patch("mistral_compat.Mistral")
def test_api_credentials_verify_paid_tier(mock_mistral_cls, client):
    """Test POST /api/credentials/verify with paid tier model list containing large."""
    mock_instance = MagicMock()
    mock_mistral_cls.return_value = mock_instance

    mock_m1 = MagicMock(id="mistral-small-2603")
    mock_m2 = MagicMock(id="mistral-medium-2604")
    mock_m3 = MagicMock(id="mistral-large-2512")
    mock_instance.models.list.return_value = MagicMock(data=[mock_m1, mock_m2, mock_m3])

    res = client.post(
        "/api/credentials/verify",
        json={"mistral_api_key": "test-paid-key"},
        headers={"Origin": "http://localhost:5000"},
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert data["tier"] == "paid"
    assert data["is_free_tier"] is False
    assert data["recommended_model"] == "mistral-medium-2604"


def test_credentials_get_includes_tier_fields(client):
    """Test GET /api/credentials response includes is_free_tier_model and model_tier."""
    res = client.get("/api/credentials", headers={"Origin": "http://localhost:5000"})
    assert res.status_code == 200
    data = res.get_json()
    assert "is_free_tier_model" in data
    assert "model_tier" in data
    assert "available_models" in data
    assert len(data["available_models"]) > 0
    # Check that model objects in available_models have tier property
    first_model = data["available_models"][0]
    assert "tier" in first_model
    assert "tier_label" in first_model
