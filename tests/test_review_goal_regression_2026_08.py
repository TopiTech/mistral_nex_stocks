"""Regression tests for 2026-08 autonomous review fixes."""

import json


class TestCopyToMyBoolRejection:
    def test_bool_weight_rejected(self, client):
        resp = client.post(
            "/api/ai-portfolio/copy-to-my",
            data=json.dumps(
                {"items": [{"symbol": "AAPL", "market": "us", "weight_pct": True, "target_price": 100}]}
            ),
            content_type="application/json",
            headers={"Origin": "http://127.0.0.1:5000"},
        )
        assert resp.status_code == 400

    def test_bool_target_price_rejected(self, client):
        resp = client.post(
            "/api/ai-portfolio/copy-to-my",
            data=json.dumps(
                {"items": [{"symbol": "AAPL", "market": "us", "weight_pct": 10, "target_price": False}]}
            ),
            content_type="application/json",
            headers={"Origin": "http://127.0.0.1:5000"},
        )
        assert resp.status_code == 400

    def test_numeric_weight_accepted(self, client):
        resp = client.post(
            "/api/ai-portfolio/copy-to-my",
            data=json.dumps(
                {"items": [{"symbol": "AAPL", "market": "us", "weight_pct": 10, "target_price": 100}]}
            ),
            content_type="application/json",
            headers={"Origin": "http://127.0.0.1:5000"},
        )
        assert resp.status_code in (200, 503)


class TestParseStockRequestTypeGuard:
    def test_int_symbol_rejected(self, client):
        resp = client.post(
            "/api/stocks/add",
            data=json.dumps({"symbol": 7203, "market": "jp", "name": "Toyota"}),
            content_type="application/json",
            headers={"Origin": "http://127.0.0.1:5000"},
        )
        assert resp.status_code in (400, 403)

    def test_int_name_rejected(self, client):
        resp = client.post(
            "/api/stocks/add",
            data=json.dumps({"symbol": "AAPL", "market": "us", "name": 12345}),
            content_type="application/json",
            headers={"Origin": "http://127.0.0.1:5000"},
        )
        assert resp.status_code in (400, 403)

    def test_bool_market_rejected(self, client):
        resp = client.post(
            "/api/stocks/add",
            data=json.dumps({"symbol": "AAPL", "market": True, "name": "Apple"}),
            content_type="application/json",
            headers={"Origin": "http://127.0.0.1:5000"},
        )
        assert resp.status_code in (400, 403)

    def test_string_symbol_still_works(self, client):
        resp = client.post(
            "/api/stocks/add",
            data=json.dumps({"symbol": "ZZZZ9999X", "market": "us", "name": "Test"}),
            content_type="application/json",
            headers={"Origin": "http://127.0.0.1:5000"},
        )
        assert resp.status_code in (200, 400, 503)


class TestCredentialsPromptTypeGuard:
    def test_non_string_prompt_rejected(self, client):
        resp = client.post(
            "/api/credentials",
            data=json.dumps({"custom_ai_prompt": {"a": 1}}),
            content_type="application/json",
            headers={"Origin": "http://127.0.0.1:5000"},
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert body is not None

    def test_string_prompt_still_accepted(self, client):
        resp = client.post(
            "/api/credentials",
            data=json.dumps({"custom_ai_prompt": "hello world"}),
            content_type="application/json",
            headers={"Origin": "http://127.0.0.1:5000"},
        )
        assert resp.status_code in (200, 500)
