"""
Regression tests for R3 / R4 / R10 root-cause fixes.

Covers:
  R3 (ROUTE-1 + SVC-1): /api/ai-technical-lines must NOT expose internal
      Mistral SDK / service error strings to the client. Both the service
      layer (generate_ai_technical_lines) and the route layer normalize the
      error to a fixed message; the detailed message stays in server logs.
  R4 (ROUTE-3): /api/credentials GET now requires a trusted Origin and the
      response only contains explicitly enumerated (schema-defined) fields.
  R10 (ROUTE-4): /api/screener total now reflects the returned count (max 150)
      while totalFiltered carries the pre-truncation filtered count.
"""

import os
from unittest.mock import patch

from app import create_app
from error_codes import ErrorCode


# ===========================================================================
# R3: AI technical lines internal error message normalization
# ===========================================================================
class TestR3AiTechnicalLinesErrorNormalization:
    def _post(self, client, **overrides):
        payload = {
            "symbol": "AAPL",
            "market": "us",
            "period": "1mo",
            "history_data": [{"date": "2026-01-01", "close": 150.0}],
        }
        payload.update(overrides)
        return client.post(
            "/api/ai-technical-lines",
            json=payload,
            headers={"Origin": "http://127.0.0.1:5000"},
        )

    def test_service_returns_fixed_message_on_error_dict(self):
        """generate_ai_technical_lines must not embed the SDK error dict/string."""
        from services.ai_service import generate_ai_technical_lines

        internal_error = {
            "error": {
                "message": "Mistral SDK internal failure: endpoint /v1/chat/completions "
                "status=503 trace=abc123",
                "status_code": 503,
            }
        }
        with patch("services.ai_service.call_mistral_chat", return_value=internal_error):
            res = generate_ai_technical_lines(
                "dummy_key", "AAPL", "us", "1mo",
                [{"date": "2026-01-01", "close": 150.0}],
            )
        assert isinstance(res, dict)
        assert "error" in res
        assert res["error"] == "AIテクニカル線の生成に失敗しました"
        # The internal SDK message must never leak into the returned value.
        assert "Mistral SDK internal failure" not in str(res)
        assert "status=503" not in str(res)

    def test_service_returns_fixed_message_on_exception(self):
        """generate_ai_technical_lines must not embed str(exc) on exception."""
        from services.ai_service import generate_ai_technical_lines

        with patch(
            "services.ai_service.call_mistral_chat",
            side_effect=RuntimeError("Mistral SDK internal failure: trace=xyz"),
        ):
            res = generate_ai_technical_lines(
                "dummy_key", "AAPL", "us", "1mo",
                [{"date": "2026-01-01", "close": 150.0}],
            )
        assert isinstance(res, dict)
        assert "error" in res
        assert res["error"] == "AIテクニカル線の生成に失敗しました"
        assert "trace=xyz" not in str(res)

    def test_endpoint_returns_fixed_message_on_error(self):
        """The route must not expose the internal error string in details."""
        app = create_app(skip_bootstrap=True)
        app.config["WTF_CSRF_ENABLED"] = False
        with app.test_client() as client:
            with (
                patch("routes.api_analysis.require_trusted_or_admin", return_value=(True, None)),
                patch(
                    "routes.api_analysis.generate_ai_technical_lines",
                    return_value={"error": "Mistral SDK internal failure: status=503"},
                ),
                patch("routes.api_analysis.extract_api_key", return_value="mock_api_key"),
            ):
                resp = self._post(client)
                assert resp.status_code == 500
                data = resp.get_json()
                assert data.get("ok") is False
                assert data.get("error_code") == ErrorCode.INTERNAL_SERVER_ERROR.value
                assert data.get("details", {}).get("reason") == "AIテクニカル線の生成に失敗しました"
                # No internal string anywhere in the response body.
                assert "Mistral SDK internal failure" not in str(data)
                assert "status=503" not in str(data.get("details", {}))

    def test_endpoint_returns_fixed_message_on_dict_error(self):
        """The route must also normalize dict-form errors (call_mistral_chat shape)."""
        app = create_app(skip_bootstrap=True)
        app.config["WTF_CSRF_ENABLED"] = False
        with app.test_client() as client:
            with (
                patch("routes.api_analysis.require_trusted_or_admin", return_value=(True, None)),
                patch(
                    "routes.api_analysis.generate_ai_technical_lines",
                    return_value={
                        "error": {
                            "message": "Mistral SDK internal failure: status=503",
                            "status_code": 503,
                        }
                    },
                ),
                patch("routes.api_analysis.extract_api_key", return_value="mock_api_key"),
            ):
                resp = self._post(client)
                assert resp.status_code == 500
                data = resp.get_json()
                assert data.get("details", {}).get("reason") == "AIテクニカル線の生成に失敗しました"
                assert "Mistral SDK internal failure" not in str(data)


# ===========================================================================
# R4: /api/credentials GET origin check + schema-validated response
# ===========================================================================
class TestR4CredentialsGetOriginAndSchema:
    def test_get_without_origin_is_allowed(self):
        """GET /api/credentials without an Origin header is allowed (same-origin
        browser GETs do not send Origin; loopback is enforced by the gate)."""
        app = create_app(skip_bootstrap=True)
        app.config["WTF_CSRF_ENABLED"] = False
        with app.test_client() as client:
            resp = client.get("/api/credentials")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data.get("ok") is True

    def test_get_with_untrusted_origin_is_rejected(self):
        """GET /api/credentials with an untrusted Origin must be rejected (403)."""
        app = create_app(skip_bootstrap=True)
        app.config["WTF_CSRF_ENABLED"] = False
        with app.test_client() as client:
            resp = client.get(
                "/api/credentials",
                headers={"Origin": "http://evil.example.com"},
            )
            assert resp.status_code == 403

    def test_get_with_trusted_origin_succeeds(self):
        """GET /api/credentials with a trusted Origin succeeds and returns ok."""
        app = create_app(skip_bootstrap=True)
        app.config["WTF_CSRF_ENABLED"] = False
        with app.test_client() as client:
            resp = client.get(
                "/api/credentials",
                headers={"Origin": "http://localhost:5000"},
            )
            assert resp.status_code == 200
            data = resp.get_json()
            assert data.get("ok") is True
            assert "has_mistral_api_key" in data
            assert "custom_ai_prompt" in data

    def test_get_with_public_origin_and_valid_admin_token_in_remote_mode_succeeds(self):
        """In remote mode the admin token authenticates; the local Origin
        allow-list must not be applied (mirrors require_trusted_or_admin)."""
        app = create_app(skip_bootstrap=True)
        app.config["WTF_CSRF_ENABLED"] = False
        with app.test_client() as client:
            env = {
                "MNS_ALLOW_REMOTE_API": "1",
                "MNS_PROXY_FIX": "1",
                "MNS_ADMIN_TOKEN": "super-secret-admin-token-32chars!!",
            }
            with patch.dict(os.environ, env, clear=False):
                resp = client.get(
                    "/api/credentials",
                    headers={
                        "Host": "dashboard.example",
                        "Origin": "https://dashboard.example",
                        "X-MNS-Admin-Token": "super-secret-admin-token-32chars!!",
                    },
                )
                assert resp.status_code == 200
                data = resp.get_json()
                assert data.get("ok") is True

    def test_get_response_only_contains_allowed_fields(self):
        """GET response must not include schema-undefined internal fields."""
        app = create_app(skip_bootstrap=True)
        app.config["WTF_CSRF_ENABLED"] = False
        with app.test_client() as client:
            # Simulate a future internal-only field leaking from the state fn.
            with patch(
                "routes.api_system.get_api_credential_state",
                return_value={
                    "has_mistral_api_key": False,
                    "has_langsearch_api_key": False,
                    "has_tavily_api_key": False,
                    "has_alphavantage_api_key": False,
                    "mistral_model": "mistral-small-2603",
                    "is_ai_technical_lines_eligible": False,
                    "credentials_ephemeral": False,
                    "credentials_ephemeral_keys": [],
                    "credentials_ephemeral_warning": None,
                    "mistral_api_key_min_length": 32,
                    "langsearch_api_key_min_length": 20,
                    "tavily_api_key_min_length": 5,
                    "internal_secret_metadata": "should-not-leak",
                },
            ):
                resp = client.get(
                    "/api/credentials",
                    headers={"Origin": "http://localhost:5000"},
                )
                assert resp.status_code == 200
                data = resp.get_json()
                assert "internal_secret_metadata" not in data
                # Allowed fields are still present (backward compatibility).
                assert data.get("has_mistral_api_key") is False
                assert data.get("mistral_model") == "mistral-small-2603"
                assert data.get("credentials_ephemeral") is False
                assert data.get("credentials_ephemeral_keys") == []
                assert data.get("credentials_ephemeral_warning") is None
                assert data.get("mistral_api_key_min_length") == 32
                assert "custom_ai_prompt" in data


# ===========================================================================
# R10: /api/screener total vs stocks count consistency
# ===========================================================================
class TestR10ScreenerTotalConsistency:
    def _make_payload(self, symbol, price):
        return {
            "symbol": symbol,
            "name": f"{symbol} Corp",
            "price": price,
            "change_percent": 1.0,
            "change": 1.0,
            "market_cap": price * 1_000_000,
            "volume": 100_000,
            "high": price + 1.0,
            "low": price - 1.0,
            "sector": "Technology",
        }

    def test_total_capped_at_150_with_total_filtered(self):
        """When >150 stocks match, total == 150 and totalFiltered == full count."""
        app = create_app(skip_bootstrap=True)
        app.config["WTF_CSRF_ENABLED"] = False
        with app.test_client() as client:
            many = [self._make_payload(f"STK{i:03d}", float(i)) for i in range(200)]
            with (
                patch(
                    "routes.api_stocks._resolve_stocks_for_response",
                    return_value={"us": many, "jp": []},
                ),
                # Isolate from POPULAR_US / query enrichment rows.
                patch("routes.api_stocks.build_popular_symbol_items", return_value=[]),
            ):
                resp = client.get("/api/screener?market=us")
                assert resp.status_code == 200
                data = resp.get_json()
                assert data.get("ok") is True
                assert len(data["stocks"]) == 150
                assert data["total"] == 150
                assert data["totalFiltered"] == 200

    def test_total_matches_stocks_when_under_limit(self):
        """When <=150 stocks match, total == totalFiltered == len(stocks)."""
        app = create_app(skip_bootstrap=True)
        app.config["WTF_CSRF_ENABLED"] = False
        with app.test_client() as client:
            few = [self._make_payload(f"STK{i:03d}", float(i)) for i in range(5)]
            with (
                patch(
                    "routes.api_stocks._resolve_stocks_for_response",
                    return_value={"us": few, "jp": []},
                ),
                patch("routes.api_stocks.build_popular_symbol_items", return_value=[]),
            ):
                resp = client.get("/api/screener?market=us")
                assert resp.status_code == 200
                data = resp.get_json()
                assert data.get("ok") is True
                assert len(data["stocks"]) == 5
                assert data["total"] == 5
                assert data["totalFiltered"] == 5