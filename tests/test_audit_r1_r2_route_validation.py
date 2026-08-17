"""Unit and integration tests for R1 and R2 input validation and null-safety fixes."""

import json
from unittest.mock import patch

import pytest

from app import create_app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MNS_DATA_DIR", "tests_runtime_data")
    monkeypatch.setenv("MNS_DISABLE_LOCAL_RATE_LIMIT", "1")
    app = create_app(config_override={"TESTING": True, "WTF_CSRF_ENABLED": False}, skip_bootstrap=True)
    with app.test_client() as c:
        yield c


def test_ai_portfolio_generate_null_and_type_validation(client):
    headers = {"Origin": "http://localhost:5000", "Content-Type": "application/json"}

    # 1. missing theme
    resp = client.post("/api/ai-portfolio/generate", json={}, headers=headers)
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False

    # 2. theme is None / null
    resp = client.post("/api/ai-portfolio/generate", json={"theme": None}, headers=headers)
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False

    # 3. theme is non-string (int)
    resp = client.post("/api/ai-portfolio/generate", json={"theme": 123}, headers=headers)
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False

    # 4. theme is empty string
    resp = client.post("/api/ai-portfolio/generate", json={"theme": "   "}, headers=headers)
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False

    # 5. valid preset theme generates successfully (with mocked service)
    mock_res = {
        "id": "tech",
        "title": "Tech Portfolio",
        "description": "Tech description",
        "items": [],
    }
    with patch("routes.api_stocks.generate_ai_portfolio_by_theme", return_value=mock_res):
        resp = client.post("/api/ai-portfolio/generate", json={"theme": "tech"}, headers=headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["portfolio"]["id"] == "tech"


def test_ai_portfolio_rebalance_type_validation(client):
    headers = {"Origin": "http://localhost:5000", "Content-Type": "application/json"}

    # 1. non-string theme
    resp = client.post("/api/ai-portfolio/rebalance", json={"theme": 123}, headers=headers)
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False

    # 2. valid theme
    mock_res = {
        "id": "tech",
        "title": "Tech Portfolio",
        "description": "Tech description",
        "items": [],
    }
    with patch("routes.api_stocks.generate_ai_portfolio_by_theme", return_value=mock_res):
        resp = client.post("/api/ai-portfolio/rebalance", json={"theme": "tech"}, headers=headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True


def test_ai_portfolio_delete_null_and_type_validation(client):
    headers = {"Origin": "http://localhost:5000", "Content-Type": "application/json"}

    # 1. missing id
    resp = client.delete("/api/ai-portfolio/custom", json={}, headers=headers)
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False

    # 2. id is None / null
    resp = client.delete("/api/ai-portfolio/custom", json={"id": None}, headers=headers)
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False

    # 3. id is non-string
    resp = client.delete("/api/ai-portfolio/custom", json={"id": 12345}, headers=headers)
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False

    # 4. id is empty string
    resp = client.delete("/api/ai-portfolio/custom", json={"id": "   "}, headers=headers)
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False

    # 5. valid deletion
    with patch("routes.api_stocks.delete_custom_ai_portfolio", return_value=True):
        resp = client.delete("/api/ai-portfolio/custom", json={"id": "custom-123"}, headers=headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["id"] == "custom-123"

    # 6. not found deletion
    with patch("routes.api_stocks.delete_custom_ai_portfolio", return_value=False):
        resp = client.delete("/api/ai-portfolio/custom", json={"id": "missing-123"}, headers=headers)
        assert resp.status_code == 404
        data = resp.get_json()
        assert data["ok"] is False


def test_ai_technical_lines_input_validation(client):
    headers = {"Origin": "http://localhost:5000", "Content-Type": "application/json"}

    # Mock model and api_key
    with patch("routes.api_analysis.get_model_name", return_value="mistral-large-2512"), \
         patch("routes.api_analysis.extract_api_key", return_value="fake-api-key-123456789012345678901234"):

        # 1. Non-string symbol
        resp = client.post("/api/ai-technical-lines", json={"symbol": 1234, "market": "us"}, headers=headers)
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["ok"] is False

        # 2. Non-string market
        resp = client.post("/api/ai-technical-lines", json={"symbol": "AAPL", "market": 123}, headers=headers)
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["ok"] is False

        # 3. Invalid history_data entry types (e.g. list of integers/strings)
        resp = client.post(
            "/api/ai-technical-lines",
            json={"symbol": "AAPL", "market": "us", "history_data": [1, 2, "bad"]},
            headers=headers,
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["ok"] is False
        assert "history_data entries must be objects" in json.dumps(data)

        # 4. Valid history_data entries (list of dicts)
        mock_result = {
            "summary": "Bullish",
            "trend_bias": "up",
            "lines": [],
        }
        with patch("routes.api_analysis.generate_ai_technical_lines", return_value=mock_result):
            resp = client.post(
                "/api/ai-technical-lines",
                json={"symbol": "AAPL", "market": "us", "history_data": [{"date": "2026-01-01", "close": 150.0}]},
                headers=headers,
            )
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["ok"] is True
            assert data["summary"] == "Bullish"


def test_api_chat_and_analyze_v2_input_validation(client):
    headers = {"Origin": "http://localhost:5000", "Content-Type": "application/json"}

    with patch("routes.api_analysis.extract_api_key", return_value="fake-api-key-123456789012345678901234"):
        # Chat: non-string symbol
        resp = client.post(
            "/api/chat",
            json={"symbol": 1234, "message": "hello", "request_token": "a" * 32},
            headers=headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False

        # Chat: non-string market
        resp = client.post(
            "/api/chat",
            json={"symbol": "AAPL", "market": 1234, "message": "hello", "request_token": "a" * 32},
            headers=headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False

        # Analyze v2: non-string symbol
        resp = client.post(
            "/api/analyze-v2",
            json={"symbol": 1234, "request_token": "a" * 32},
            headers=headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False

        # Analyze v2: non-string market
        resp = client.post(
            "/api/analyze-v2",
            json={"symbol": "AAPL", "market": 1234, "request_token": "a" * 32},
            headers=headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False

        # Analyze v2: non-string name
        resp = client.post(
            "/api/analyze-v2",
            json={"symbol": "AAPL", "market": "us", "name": 1234, "request_token": "a" * 32},
            headers=headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False
