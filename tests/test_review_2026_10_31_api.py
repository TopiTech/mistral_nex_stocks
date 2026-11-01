"""Regression tests for the 2026-10-31 API-boundary review fixes (API-1..API-10).

Covers, per issue: the original trigger, edge/abnormal cases, and retained
valid behavior — without network access, skips, or weakened assertions.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from flask import Flask, g, jsonify
from pydantic import ValidationError
from werkzeug.exceptions import abort

from app import create_app
from app_state import app_state
from error_codes import ErrorCode
from error_handlers import register_error_handlers
from schemas.ai_portfolio import AIPortfolioCopyToMyRequest
from schemas.stocks import ScreenerQueryRequest
from services.ai_service import ProviderAPIError
from utils.stock_payload import error_response
from utils.storage import UserStocksPersistError

ORIGIN = {"Origin": "http://localhost:5000"}

_ENVELOPE_KEYS = {"ok", "error", "error_flag", "code", "error_code", "message", "details"}


@pytest.fixture(scope="module")
def app():
    application = create_app()
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def _restore_user_stocks():
    """Snapshot and restore the user watchlist containers around every test."""
    with app_state.market.user_stocks_lock:
        us = dict(app_state.market.user_us)
        jp = dict(app_state.market.user_jp)
        idx = dict(app_state.market.user_idx)
    yield
    with app_state.market.user_stocks_lock:
        (
            app_state.market.user_us,
            app_state.market.user_jp,
            app_state.market.user_idx,
        ) = us, jp, idx


@pytest.fixture
def _cleared_rate_limit_store():
    from route_helpers import (
        _rate_limit_distinct_token_counts,
        _rate_limit_lock,
        _rate_limit_store,
        _rate_limit_window_by_key,
    )

    def _clear() -> None:
        with _rate_limit_lock:
            _rate_limit_store.clear()
            _rate_limit_window_by_key.clear()
            _rate_limit_distinct_token_counts.clear()

    _clear()
    yield
    _clear()


# ---------------------------------------------------------------- API-1 -----


class TestAPI1WeightPctFalsySwallow:
    def test_empty_string_reports_numeric_error_not_range(self, client):
        """"weight_pct: '' must map to copy_bad_numeric, not the range error."""
        resp = client.post(
            "/api/ai-portfolio/copy-to-my",
            json={
                "items": [
                    {"symbol": "AAPL", "market": "us", "target_price": 100, "weight_pct": ""}
                ]
            },
            headers=ORIGIN,
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
        assert body["details"] == {"reason": "items[0] の数値が無効です"}

    @pytest.mark.parametrize("bad", ["bad", "abc", " "])
    def test_non_numeric_strings_still_report_numeric_error(self, client, bad):
        resp = client.post(
            "/api/ai-portfolio/copy-to-my",
            json={
                "items": [
                    {"symbol": "AAPL", "market": "us", "target_price": 100, "weight_pct": bad}
                ]
            },
            headers=ORIGIN,
        )
        assert resp.status_code == 400
        assert resp.get_json()["details"] == {"reason": "items[0] の数値が無効です"}

    @pytest.mark.parametrize("bad", [None, 0, 0.0])
    def test_none_and_zero_keep_range_error(self, client, bad):
        resp = client.post(
            "/api/ai-portfolio/copy-to-my",
            json={
                "items": [
                    {"symbol": "AAPL", "market": "us", "target_price": 100, "weight_pct": bad}
                ]
            },
            headers=ORIGIN,
        )
        assert resp.status_code == 400
        reason = resp.get_json()["details"]["reason"]
        assert "weight_pct は 0〜100 の範囲が必要です" in reason

    @pytest.mark.parametrize("weight", [20, 100])
    def test_valid_weights_still_validate(self, weight):
        req = AIPortfolioCopyToMyRequest.model_validate(
            {
                "items": [
                    {"symbol": "AAPL", "market": "us", "target_price": 100, "weight_pct": weight}
                ]
            }
        )
        assert req.items[0].weight_pct == float(weight)

    def test_zero_weight_schema_still_range_error(self):
        with pytest.raises(ValidationError) as exc_info:
            AIPortfolioCopyToMyRequest.model_validate(
                {
                    "items": [
                        {
                            "symbol": "AAPL",
                            "market": "us",
                            "target_price": 100,
                            "weight_pct": 0,
                        }
                    ]
                }
            )
        assert "copy_bad_weight_range" in str(exc_info.value)


# ---------------------------------------------------------------- API-2 -----


class TestAPI2RetryAfterEmptyBucket:
    def test_zero_max_requests_returns_429_with_retry_after(self, _cleared_rate_limit_store):
        """An empty bucket (max_requests=0) must yield 429 + Retry-After, not IndexError/500."""
        from route_helpers import rate_limit

        application = Flask(__name__)
        application.config["TESTING"] = True

        @application.route("/rl-zero")
        @rate_limit(max_requests=1, window_seconds=60)
        def rl_zero():
            return jsonify({"ok": True})

        client = application.test_client()
        with patch("route_helpers._resolve_rate_limit", return_value=(0, 60)):
            started = time.monotonic()
            resp = client.get("/rl-zero", environ_base={"REMOTE_ADDR": "10.77.0.1"})
            elapsed = time.monotonic() - started
        assert elapsed < 5.0, "rate limiter blocked instead of failing fast"
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
        assert int(resp.headers["Retry-After"]) == 60
        data = resp.get_json()
        assert data["error_code"] == int(ErrorCode.API_RATE_LIMITED)
        assert data["details"]["retry_after"] == 60

    def test_normal_bucket_retry_after_retained(self, _cleared_rate_limit_store):
        from route_helpers import rate_limit

        application = Flask(__name__)
        application.config["TESTING"] = True

        @application.route("/rl-normal")
        @rate_limit(max_requests=2, window_seconds=60)
        def rl_normal():
            return jsonify({"ok": True})

        client = application.test_client()
        env = {"REMOTE_ADDR": "10.77.0.2"}
        assert client.get("/rl-normal", environ_base=env).status_code == 200
        assert client.get("/rl-normal", environ_base=env).status_code == 200
        resp = client.get("/rl-normal", environ_base=env)
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
        retry_after = int(resp.headers["Retry-After"])
        assert 0 <= retry_after <= 60


# ---------------------------------------------------------------- API-3 -----


class TestAPI3NestedRateLimitLock:
    def test_polling_inflight_resolution_outside_global_lock(self, _cleared_rate_limit_store):
        import route_helpers
        from route_helpers import rate_limit

        observed: list[bool] = []
        application = Flask(__name__)
        application.config["TESTING"] = True

        @application.route("/api/chat", methods=["POST"])
        @rate_limit(max_requests=2, window_seconds=60, skip_polling_duplicates=True)
        def chat():
            return jsonify({"ok": True})

        def probe(token: str) -> bool:
            observed.append(route_helpers._rate_limit_lock.locked())
            return True

        client = application.test_client()
        env = {"REMOTE_ADDR": "10.77.0.3"}
        with patch.object(route_helpers, "_is_polling_token_inflight", side_effect=probe):
            for _ in range(3):  # max_requests=2 -> later polls take the skip path
                resp = client.post(
                    "/api/chat",
                    json={"request_token": "a1b2c3d4e5f60718293a4b5c6d7e8f90"},
                    environ_base=env,
                )
                assert resp.status_code == 200, resp.get_data(as_text=True)
        assert observed, "inflight resolution never ran"
        assert observed == [False] * len(observed), (
            f"_is_polling_token_inflight ran while _rate_limit_lock was held: {observed}"
        )

    def test_same_token_polls_still_bypass_quota(self, _cleared_rate_limit_store):
        """Retained behavior: repeated request_tokens must not hit 429."""
        from route_helpers import rate_limit

        application = Flask(__name__)
        application.config["TESTING"] = True

        @application.route("/api/chat", methods=["POST"])
        @rate_limit(max_requests=2, window_seconds=60, skip_polling_duplicates=True)
        def chat():
            return jsonify({"ok": True})

        client = application.test_client()
        env = {"REMOTE_ADDR": "10.77.0.4"}
        token = "ffffffffeeeedddcccbbbaaa99988877"
        for _ in range(10):
            resp = client.post("/api/chat", json={"request_token": token}, environ_base=env)
            assert resp.status_code == 200, resp.get_data(as_text=True)


# ---------------------------------------------------------------- API-4 -----


class TestAPI4UnmappedHttpStatusFallback:
    @staticmethod
    def _build_app() -> Flask:
        application = Flask(__name__)
        application.config["TESTING"] = True
        register_error_handlers(application)

        @application.route("/raise-415")
        def raise_415():
            abort(415)

        @application.route("/raise-422")
        def raise_422():
            abort(422)

        @application.route("/raise-503")
        def raise_503():
            abort(503)

        @application.route("/raise-404")
        def raise_404():
            abort(404)

        @application.route("/raise-403")
        def raise_403():
            abort(403)

        @application.route("/raise-400")
        def raise_400():
            abort(400)

        return application

    @pytest.mark.parametrize(
        ("path", "status", "expected_code"),
        [
            ("/raise-415", 415, ErrorCode.BAD_REQUEST),
            ("/raise-422", 422, ErrorCode.BAD_REQUEST),
            ("/raise-503", 503, ErrorCode.SYSTEM_ERROR),
            ("/raise-404", 404, ErrorCode.NOT_FOUND),
            ("/raise-403", 403, ErrorCode.FORBIDDEN),
            ("/raise-400", 400, ErrorCode.BAD_REQUEST),
        ],
    )
    def test_unmapped_status_gets_typed_error_code(self, path, status, expected_code):
        client = self._build_app().test_client()
        resp = client.get(path)
        assert resp.status_code == status
        data = resp.get_json()
        assert _ENVELOPE_KEYS.issubset(data.keys())
        assert data["error_code"] == int(expected_code)
        assert data["error_code"] != int(ErrorCode.UNKNOWN)
        assert data["code"] == str(expected_code)
        # Consistent details.reason presence across every status.
        assert "reason" in data["details"]
        assert data["details"]["reason"] is None
        assert data["ok"] is False
        assert data["error_flag"] is True

    def test_unmapped_status_does_not_leak_description(self):
        client = self._build_app().test_client()
        # The HTTPException *name* is the message (same as mapped handlers);
        # the Werkzeug *description* must never reach the client.
        data = client.get("/raise-415").get_json()
        assert data["message"] == "Unsupported Media Type"
        assert "requested resource" not in str(data).lower()
        data503 = client.get("/raise-503").get_json()
        assert "debugger" not in str(data503).lower()
        assert "server encountered" not in str(data503).lower()


# ---------------------------------------------------------------- API-5 -----


class TestAPI5StockDetailsFailureEnvelope:
    def test_failure_body_has_all_seven_envelope_keys(self, client):
        with app_state.yfinance_short_cache_lock:
            app_state.yfinance_short_cache["info_short_API5FAIL"] = {"failed": True}
        try:
            resp = client.get("/api/stock-details?symbol=API5FAIL&market=us")
            assert resp.status_code == 200
            data = resp.get_json()
            # Legacy UI contract retained.
            assert data["ok"] is False
            assert data["success"] is False
            assert data["failed"] is True
            assert data["symbol"] == "API5FAIL"
            assert data["error"] == "銘柄詳細情報の取得に失敗しました"
            assert data["sector"] is None
            assert data["industry"] is None
            assert data["market_cap"] is None
            assert data["pe_ratio"] is None
            # Additive envelope keys.
            assert _ENVELOPE_KEYS.issubset(data.keys())
            assert data["error_flag"] is True
            assert data["error_code"] == int(ErrorCode.FETCH_FAILED)
            assert data["code"] == str(ErrorCode.FETCH_FAILED)
            assert data["message"] == data["error"]
            assert isinstance(data["details"], dict)
        finally:
            with app_state.yfinance_short_cache_lock:
                app_state.yfinance_short_cache.pop("info_short_API5FAIL", None)

    def test_hit_branch_still_returns_ok_envelope(self, client):
        with app_state.yfinance_short_cache_lock:
            app_state.yfinance_short_cache["info_short_API5OK"] = {
                "sector": "Tech",
                "industry": "Software",
                "marketCap": 1,
                "trailingPE": 10,
            }
        try:
            resp = client.get("/api/stock-details?symbol=API5OK&market=us")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["ok"] is True
            assert data["sector"] == "Tech"
        finally:
            with app_state.yfinance_short_cache_lock:
                app_state.yfinance_short_cache.pop("info_short_API5OK", None)


# ---------------------------------------------------------------- API-6 -----


def _screener_error(raw: dict) -> tuple[dict, int]:
    application = Flask(__name__)
    application.config["TESTING"] = True
    with application.test_request_context():
        with pytest.raises(ValidationError) as exc_info:
            ScreenerQueryRequest.model_validate(raw)
        from route_helpers import _screener_query_error_from_validation

        resp, status = _screener_query_error_from_validation(exc_info.value)
        return resp.get_json(), status


class TestAPI6ScreenerLimitRangeVsType:
    @pytest.mark.parametrize("raw", [{"limit": "0"}, {"limit": "1000"}, {"limit": "-5"}])
    def test_out_of_range_limit_reports_range_error(self, raw):
        data, status = _screener_error(raw)
        assert status == 400
        assert data["error_code"] == int(ErrorCode.INVALID_INPUT)
        assert data["details"]["reason"] == "limit は1〜500の整数で指定してください"
        assert data["details"]["fields"] == ["limit"]

    @pytest.mark.parametrize("raw", [{"limit": "abc"}, {"limit": True}])
    def test_type_error_limit_keeps_integer_wording(self, raw):
        data, status = _screener_error(raw)
        assert status == 400
        assert data["details"]["reason"] == "limit は整数で指定してください"
        assert data["details"]["fields"] == ["limit"]

    def test_endpoint_range_error(self, client):
        resp = client.get("/api/screener?limit=0")
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["error_code"] == int(ErrorCode.INVALID_INPUT)
        assert "1〜500" in data["details"]["reason"]

    @pytest.mark.parametrize("value", ["1", "250", "500"])
    def test_valid_limits_still_accepted(self, value):
        assert ScreenerQueryRequest.model_validate({"limit": value}).limit == int(value)


# ---------------------------------------------------------------- API-7 -----


class TestAPI7WatchlistRollbackTOCTOU:
    def test_add_rollback_preserves_concurrent_write(self, client):
        symbol = "ZZAPI7A1"

        def failing_save_with_concurrent_write() -> None:
            with app_state.market.user_stocks_lock:
                app_state.market.user_us[symbol] = "Concurrent Name"
            raise UserStocksPersistError("disk full")

        with patch(
            "routes.stocks.views.save_user_stocks",
            side_effect=failing_save_with_concurrent_write,
        ):
            resp = client.post(
                "/api/stocks/add",
                json={"symbol": symbol, "market": "us", "name": "Orig Name"},
                headers=ORIGIN,
            )
        assert resp.status_code == 503
        assert resp.get_json()["ok"] is False
        with app_state.market.user_stocks_lock:
            assert app_state.market.user_us.get(symbol) == "Concurrent Name"

    def test_add_rollback_still_undoes_own_write(self, client):
        symbol = "ZZAPI7A2"
        with patch(
            "routes.stocks.views.save_user_stocks",
            side_effect=UserStocksPersistError("disk full"),
        ):
            resp = client.post(
                "/api/stocks/add",
                json={"symbol": symbol, "market": "us", "name": "Orig Name"},
                headers=ORIGIN,
            )
        assert resp.status_code == 503
        with app_state.market.user_stocks_lock:
            assert symbol not in app_state.market.user_us

    def test_delete_rollback_preserves_concurrent_readd(self, client):
        symbol = "ZZAPI7D1"
        with app_state.market.user_stocks_lock:
            app_state.market.user_us[symbol] = "Old Name"

        def failing_save_with_concurrent_readd() -> None:
            with app_state.market.user_stocks_lock:
                app_state.market.user_us[symbol] = "Readded By Concurrent"
            raise UserStocksPersistError("disk full")

        with patch(
            "routes.stocks.views.save_user_stocks",
            side_effect=failing_save_with_concurrent_readd,
        ):
            resp = client.post(
                "/api/stocks/delete",
                json={"symbol": symbol, "market": "us"},
                headers=ORIGIN,
            )
        assert resp.status_code == 503
        with app_state.market.user_stocks_lock:
            assert app_state.market.user_us.get(symbol) == "Readded By Concurrent"

    def test_delete_rollback_restores_when_untouched(self, client):
        symbol = "ZZAPI7D2"
        with app_state.market.user_stocks_lock:
            app_state.market.user_us[symbol] = "Old Name"
        with patch(
            "routes.stocks.views.save_user_stocks",
            side_effect=UserStocksPersistError("disk full"),
        ):
            resp = client.post(
                "/api/stocks/delete",
                json={"symbol": symbol, "market": "us"},
                headers=ORIGIN,
            )
        assert resp.status_code == 503
        with app_state.market.user_stocks_lock:
            assert app_state.market.user_us.get(symbol) == "Old Name"

    def test_portfolio_update_rollback_preserves_concurrent_write(self, client):
        symbol = "ZZAPI7P1"
        with app_state.market.user_stocks_lock:
            app_state.market.user_us[symbol] = {
                "name": "Port Co",
                "shares": 5,
                "avg_price": 10.0,
            }

        def failing_save_with_concurrent_write() -> None:
            with app_state.market.user_stocks_lock:
                app_state.market.user_us[symbol] = {
                    "name": "Port Co",
                    "shares": 999,
                    "avg_price": 1.0,
                }
            raise UserStocksPersistError("disk full")

        with patch(
            "routes.stocks.portfolio.save_user_stocks",
            side_effect=failing_save_with_concurrent_write,
        ):
            resp = client.post(
                "/api/stocks/portfolio",
                json={"market": "us", "symbol": symbol, "shares": 10, "avg_price": 2.0},
                headers=ORIGIN,
            )
        assert resp.status_code == 503
        with app_state.market.user_stocks_lock:
            assert app_state.market.user_us.get(symbol) == {
                "name": "Port Co",
                "shares": 999,
                "avg_price": 1.0,
            }

    def test_portfolio_update_rollback_restores_previous_when_untouched(self, client):
        symbol = "ZZAPI7P2"
        previous = {"name": "Port Co", "shares": 5, "avg_price": 10.0}
        with app_state.market.user_stocks_lock:
            app_state.market.user_us[symbol] = dict(previous)
        with patch(
            "routes.stocks.portfolio.save_user_stocks",
            side_effect=UserStocksPersistError("disk full"),
        ):
            resp = client.post(
                "/api/stocks/portfolio",
                json={"market": "us", "symbol": symbol, "shares": 10, "avg_price": 2.0},
                headers=ORIGIN,
            )
        assert resp.status_code == 503
        with app_state.market.user_stocks_lock:
            assert app_state.market.user_us.get(symbol) == previous

    def test_reset_rollback_preserves_concurrent_add(self, client):
        with app_state.market.user_stocks_lock:
            app_state.market.user_us["ZZAPI7R1"] = "Pre Reset"

        def failing_save_with_concurrent_add() -> None:
            with app_state.market.user_stocks_lock:
                app_state.market.user_us["ZZAPI7R2"] = "Added During Save"
            raise UserStocksPersistError("disk full")

        with patch(
            "routes.stocks.views.save_user_stocks",
            side_effect=failing_save_with_concurrent_add,
        ):
            resp = client.post("/api/stocks/reset", json={}, headers=ORIGIN)
        assert resp.status_code == 503
        with app_state.market.user_stocks_lock:
            assert app_state.market.user_us.get("ZZAPI7R1") == "Pre Reset"
            assert app_state.market.user_us.get("ZZAPI7R2") == "Added During Save"

    def test_copy_to_my_rollback_preserves_concurrent_write(self, client):
        symbol = "7751.T"
        payload = {
            "items": [
                {"symbol": symbol, "market": "jp", "target_price": 1000, "weight_pct": 20}
            ]
        }

        def failing_save_with_concurrent_write() -> None:
            with app_state.market.user_stocks_lock:
                app_state.market.user_jp[symbol] = {
                    "name": "Concurrent Holding",
                    "shares": 123,
                    "avg_price": 5.0,
                }
            raise RuntimeError("disk full")

        with patch(
            "routes.stocks.ai_portfolio.save_user_stocks",
            side_effect=failing_save_with_concurrent_write,
        ):
            resp = client.post("/api/ai-portfolio/copy-to-my", json=payload, headers=ORIGIN)
        assert resp.status_code == 503
        with app_state.market.user_stocks_lock:
            assert app_state.market.user_jp.get(symbol) == {
                "name": "Concurrent Holding",
                "shares": 123,
                "avg_price": 5.0,
            }

    def test_copy_to_my_rollback_removes_own_write_when_untouched(self, client):
        symbol = "7752.T"
        payload = {
            "items": [
                {"symbol": symbol, "market": "jp", "target_price": 1000, "weight_pct": 20}
            ]
        }
        with patch(
            "routes.stocks.ai_portfolio.save_user_stocks",
            side_effect=RuntimeError("disk full"),
        ):
            resp = client.post("/api/ai-portfolio/copy-to-my", json=payload, headers=ORIGIN)
        assert resp.status_code == 503
        with app_state.market.user_stocks_lock:
            assert symbol not in app_state.market.user_jp


# ---------------------------------------------------------------- API-8 -----


class TestAPI8PageConfigFallback:
    def test_setup_renders_typed_config_fallback_on_schema_failure(self, client):
        with patch("routes.pages.get_api_credential_state", return_value={}):
            resp = client.get("/setup")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert '"has_mistral_api_key": false' in html
        assert '"config_error": true' in html

    def test_setup_normal_render_still_works(self, client):
        resp = client.get("/setup")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert '"has_mistral_api_key"' in html
        assert '"config_error": true' not in html


# ---------------------------------------------------------------- API-9 -----


class TestAPI9CredentialsVerifySanitization:
    def test_openai_verify_failure_sanitizes_details_and_message(self, client):
        mock_res = {
            "ok": False,
            "error": "boom api_key=ABCDEFGH12345678 at https://upstream.internal/v1/models",
            "latency_ms": 5,
        }
        with patch(
            "services.openai_service.verify_openai_connection", return_value=mock_res
        ):
            resp = client.post(
                "/api/credentials/verify",
                json={
                    "target_provider": "openai_compatible",
                    "api_key": "sk-test-key-0123456789abcd",
                },
                headers=ORIGIN,
            )
        assert resp.status_code == 400
        data = resp.get_json()
        full = str(data)
        # Raw upstream text / credentials never reach the client.
        assert "ABCDEFGH12345678" not in full
        assert "[REDACTED]" in data["error"]
        # Standard sanitized details shape (no raw service dict).
        assert isinstance(data["details"], dict)
        assert data["details"]["reason"] == data["error"]
        assert "ok" not in data["details"]
        # Legacy compat keys retained.
        assert data["valid"] is False
        assert data["code"] == "INVALID_API_KEY"
        assert data["error_code"] == int(ErrorCode.INVALID_API_KEY)
        assert data["latency_ms"] == 5
        assert data["details"]["latency_ms"] == 5
        assert _ENVELOPE_KEYS.issubset(data.keys())

    def test_openai_verify_failure_without_error_uses_fallback(self, client):
        mock_res = {"ok": False, "latency_ms": 7}
        with patch(
            "services.openai_service.verify_openai_connection", return_value=mock_res
        ):
            resp = client.post(
                "/api/credentials/verify",
                json={
                    "target_provider": "openai_compatible",
                    "api_key": "whatever-key-0123456789",
                },
                headers=ORIGIN,
            )
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["error"] == "OpenAI互換APIキーの検証に失敗しました。設定と接続を確認してください。"
        assert data["valid"] is False
        assert data["latency_ms"] == 7


# --------------------------------------------------------------- API-10 -----


class TestAPI10NestedDetailsSanitization:
    def test_error_response_sanitizes_nested_dicts_and_lists(self):
        application = Flask(__name__)
        application.config["TESTING"] = True
        with application.test_request_context():
            resp, status = error_response(
                ErrorCode.API_SERVICE_ERROR,
                502,
                details={
                    "reason": "outer failure api_key=outer1234567890",
                    "provider_error": {
                        "message": "upstream failed api_key=sk-abcdef1234567890abcd",
                        "status_code": 503,
                        "retry": {"hint": "retry after bearer abcdef1234567890"},
                    },
                    "attempts": [{"detail": "password=hunter2secret9999"}],
                },
            )
        assert status == 502
        data = resp.get_json()
        full = str(data)
        assert "outer1234567890" not in full
        assert "sk-abcdef1234567890abcd" not in full
        assert "abcdef1234567890" not in full
        assert "hunter2secret9999" not in full
        # Numeric leaves and safe values pass through untouched.
        assert data["details"]["provider_error"]["status_code"] == 503
        assert data["details"]["reason"].startswith("outer failure [REDACTED]")

    def test_ai_portfolio_provider_error_details_sanitized(self):
        from routes.stocks.ai_portfolio import _ai_portfolio_error_response

        application = Flask(__name__)
        application.config["TESTING"] = True
        with application.app_context():
            with application.test_request_context():
                g.request_id = "api10-test"
                err = ProviderAPIError(
                    "upstream boom",
                    status_code=503,
                    details={
                        "provider_error": {
                            "message": "raw api_key=sk-abcdef1234567890abcd",
                            "status_code": 503,
                        }
                    },
                )
                resp, status = _ai_portfolio_error_response(err, g)
        assert status == 503
        data = resp.get_json()
        assert "sk-abcdef1234567890abcd" not in str(data)
        assert data["details"]["provider_error"]["status_code"] == 503

    def test_extra_fields_nested_values_sanitized(self):
        application = Flask(__name__)
        application.config["TESTING"] = True
        with application.test_request_context():
            resp, _ = error_response(
                ErrorCode.API_SERVICE_ERROR,
                502,
                extra_fields={"nested": {"hint": "token=abcdef1234567890"}},
            )
        data = resp.get_json()
        assert "abcdef1234567890" not in str(data)
        assert "[REDACTED]" in data["nested"]["hint"]
