"""Regression tests for 2026-08 autonomous review fixes."""

import json
from unittest.mock import patch

from app_state import app_state


class TestCopyToMyBoolRejection:
    def test_bool_weight_rejected(self, client):
        resp = client.post(
            "/api/ai-portfolio/copy-to-my",
            data=json.dumps(
                {
                    "items": [
                        {"symbol": "AAPL", "market": "us", "weight_pct": True, "target_price": 100}
                    ]
                }
            ),
            content_type="application/json",
            headers={"Origin": "http://127.0.0.1:5000"},
        )
        assert resp.status_code == 400

    def test_bool_target_price_rejected(self, client):
        resp = client.post(
            "/api/ai-portfolio/copy-to-my",
            data=json.dumps(
                {
                    "items": [
                        {"symbol": "AAPL", "market": "us", "weight_pct": 10, "target_price": False}
                    ]
                }
            ),
            content_type="application/json",
            headers={"Origin": "http://127.0.0.1:5000"},
        )
        assert resp.status_code == 400

    def test_numeric_weight_accepted(self, client):
        with app_state.market.user_stocks_lock:
            had_aapl = "AAPL" in app_state.market.user_us
            prev_aapl = app_state.market.user_us.get("AAPL")
        try:
            with (
                patch("routes.stocks.ai_portfolio.require_trusted_or_admin", return_value=(True, None)),
                patch("routes.stocks.ai_portfolio.save_user_stocks"),
                patch("routes.stocks.common._sync_realtime_symbol"),
                patch("app_bg.announce_current_market_state"),
                patch("app_bg.schedule_sync_all_stocks_now"),
            ):
                resp = client.post(
                    "/api/ai-portfolio/copy-to-my",
                    data=json.dumps(
                        {
                            "items": [
                                {
                                    "symbol": "AAPL",
                                    "market": "us",
                                    "weight_pct": 10,
                                    "target_price": 100,
                                }
                            ]
                        }
                    ),
                    content_type="application/json",
                    headers={"Origin": "http://127.0.0.1:5000"},
                )
            assert resp.status_code == 200
        finally:
            with app_state.market.user_stocks_lock:
                if had_aapl:
                    app_state.market.user_us["AAPL"] = prev_aapl
                else:
                    app_state.market.user_us.pop("AAPL", None)


class TestParseStockRequestTypeGuard:
    def test_int_symbol_rejected(self, client):
        with patch("routes.stocks.views.require_trusted_or_admin", return_value=(True, None)):
            resp = client.post(
                "/api/stocks/add",
                data=json.dumps({"symbol": 7203, "market": "jp", "name": "Toyota"}),
                content_type="application/json",
                headers={"Origin": "http://127.0.0.1:5000"},
            )
        assert resp.status_code == 400

    def test_int_name_rejected(self, client):
        with patch("routes.stocks.views.require_trusted_or_admin", return_value=(True, None)):
            resp = client.post(
                "/api/stocks/add",
                data=json.dumps({"symbol": "AAPL", "market": "us", "name": 12345}),
                content_type="application/json",
                headers={"Origin": "http://127.0.0.1:5000"},
            )
        assert resp.status_code == 400

    def test_bool_market_rejected(self, client):
        with patch("routes.stocks.views.require_trusted_or_admin", return_value=(True, None)):
            resp = client.post(
                "/api/stocks/add",
                data=json.dumps({"symbol": "AAPL", "market": True, "name": "Apple"}),
                content_type="application/json",
                headers={"Origin": "http://127.0.0.1:5000"},
            )
        assert resp.status_code == 400

    def test_string_symbol_still_works(self, client):
        with app_state.market.user_stocks_lock:
            app_state.market.user_us.pop("ZZZZ9999X", None)
        try:
            with (
                patch("routes.stocks.views.require_trusted_or_admin", return_value=(True, None)),
                patch("routes.stocks.views.save_user_stocks"),
                patch("routes.stocks.common._announce_watchlist_state"),
                patch("routes.stocks.common._sync_realtime_symbol"),
                patch("routes.stocks.views.schedule_sync_all_stocks_now"),
            ):
                resp = client.post(
                    "/api/stocks/add",
                    data=json.dumps({"symbol": "ZZZZ9999X", "market": "us", "name": "Test"}),
                    content_type="application/json",
                    headers={"Origin": "http://127.0.0.1:5000"},
                )
            assert resp.status_code == 200
        finally:
            with app_state.market.user_stocks_lock:
                app_state.market.user_us.pop("ZZZZ9999X", None)


class TestCredentialsPromptTypeGuard:
    def test_non_string_prompt_rejected(self, client):
        with patch("routes.system.credentials.require_trusted_or_admin", return_value=(True, None)):
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
        with (
            patch("routes.system.credentials.require_trusted_or_admin", return_value=(True, None)),
            patch("routes.api_system.save_api_credentials") as mock_save,
            patch(
                "routes.system.credentials._build_safe_credentials_response",
                return_value={"custom_ai_prompt": "hello world"},
            ),
        ):
            resp = client.post(
                "/api/credentials",
                data=json.dumps({"custom_ai_prompt": "hello world"}),
                content_type="application/json",
                headers={"Origin": "http://127.0.0.1:5000"},
            )
        assert resp.status_code == 200
        mock_save.assert_called_once()
        assert mock_save.call_args.kwargs["custom_ai_prompt"] == "hello world"
