"""HTTP contract tests for the canonical portfolio request schema.

U1 moved ``POST /api/stocks/portfolio`` onto
``schemas.stocks.PortfolioUpdateRequest`` and deleted the duplicate
``PortfolioInputSchema``/``parse_non_negative_float`` validation in the route.
These tests pin the observable contract so a future schema edit cannot silently
change an error code, message or status code.
"""

from __future__ import annotations

import json

import pytest

from app import app
from app_state import app_state
from constants import (
    PORTFOLIO_AVG_FX_RATE_MAX,
    PORTFOLIO_SHARES_MAX,
    PORTFOLIO_TOTAL_VALUE_MAX,
)
from error_codes import ErrorCode
from schemas.stocks import PortfolioUpdateRequest


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def reset_portfolio_state():
    with app_state.market.user_stocks_lock:
        app_state.market.user_us.clear()
        app_state.market.user_jp.clear()
        app_state.market.user_idx.clear()


def _post(client, payload):
    return client.post(
        "/api/stocks/portfolio",
        data=json.dumps(payload),
        content_type="application/json",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        headers={"Origin": "http://localhost:5000"},
    )


def _error(resp):
    body = resp.get_json()
    assert body is not None
    # The unified error envelope must not shrink or grow.
    assert set(body) == {
        "ok",
        "error",
        "error_flag",
        "code",
        "error_code",
        "message",
        "details",
    }
    assert body["ok"] is False
    assert body["error_flag"] is True
    assert body["code"] == str(body["error_code"])
    assert body["error"] == body["message"]
    return body


def test_missing_shares_keeps_missing_required_field(client):
    resp = _post(client, {"symbol": "AAPL", "market": "us", "avg_price": 100.0})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MISSING_REQUIRED_FIELD
    assert body["details"] == {"fields": ["shares"]}


def test_blank_shares_is_missing_not_invalid(client):
    resp = _post(
        client,
        {"symbol": "AAPL", "market": "us", "shares": "  ", "avg_price": 100.0},
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MISSING_REQUIRED_FIELD
    assert body["details"] == {"fields": ["shares"]}


def test_missing_required_field_wins_over_a_bad_sibling_value(client):
    # The old route checked presence before parsing either number.
    resp = _post(
        client,
        {"symbol": "AAPL", "market": "us", "shares": "abc"},
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MISSING_REQUIRED_FIELD
    assert body["details"] == {"fields": ["avg_price"]}


def test_identity_errors_still_win_over_numeric_errors(client):
    resp = _post(client, {"symbol": "../x", "market": "us"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_SYMBOL


def test_wrong_type_message_is_unchanged(client):
    resp = _post(
        client,
        {"symbol": "AAPL", "market": "us", "shares": "abc", "avg_price": 100.0},
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_INPUT
    assert body["details"]["reason"] == "shares must be a number"


def test_boolean_is_not_a_number(client):
    resp = _post(
        client,
        {"symbol": "AAPL", "market": "us", "shares": True, "avg_price": 100.0},
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_INPUT
    assert body["details"]["reason"] == "shares must be a number"


def test_non_finite_price_is_rejected(client):
    # json.dumps would emit ``Infinity`` which Flask's parser accepts; send raw.
    resp = client.post(
        "/api/stocks/portfolio",
        data='{"symbol":"AAPL","market":"us","shares":1,"avg_price":Infinity}',
        content_type="application/json",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        headers={"Origin": "http://localhost:5000"},
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_INPUT
    assert body["details"]["reason"] == "avg_price must be finite"


def test_over_limit_shares_message_is_unchanged(client):
    resp = _post(
        client,
        {
            "symbol": "AAPL",
            "market": "us",
            "shares": PORTFOLIO_SHARES_MAX + 1,
            "avg_price": 1.0,
        },
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_INPUT
    assert body["details"]["reason"] == f"shares must be <= {PORTFOLIO_SHARES_MAX}"


def test_total_value_limit_uses_the_japanese_message(client):
    resp = _post(
        client,
        {
            "symbol": "AAPL",
            "market": "us",
            "shares": PORTFOLIO_SHARES_MAX,
            "avg_price": PORTFOLIO_SHARES_MAX,
        },
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_INPUT
    assert body["details"]["reason"] == (
        f"ポートフォリオ総額は{PORTFOLIO_TOTAL_VALUE_MAX:,}以下である必要があります"
    )


def test_zero_fx_rate_message_is_unchanged(client):
    resp = _post(
        client,
        {
            "symbol": "AAPL",
            "market": "us",
            "shares": 1.0,
            "avg_price": 100.0,
            "avg_fx_rate": 0,
        },
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_INPUT
    assert body["details"]["reason"] == "avg_fx_rateは正の数値である必要があります"


def test_fx_rate_upper_bound_message_is_unchanged(client):
    resp = _post(
        client,
        {
            "symbol": "AAPL",
            "market": "us",
            "shares": 1.0,
            "avg_price": 100.0,
            "avg_fx_rate": PORTFOLIO_AVG_FX_RATE_MAX + 1,
        },
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_INPUT
    assert body["details"]["reason"] == f"avg_fx_rate must be <= {PORTFOLIO_AVG_FX_RATE_MAX}"


def test_malformed_json_body_is_rejected(client):
    resp = client.post(
        "/api/stocks/portfolio",
        data="{not json",
        content_type="application/json",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        headers={"Origin": "http://localhost:5000"},
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MALFORMED_INPUT
    assert body["details"]["reason"] == "JSON形式が不正です"


def test_json_array_body_is_malformed(client):
    resp = client.post(
        "/api/stocks/portfolio",
        data="[]",
        content_type="application/json",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        headers={"Origin": "http://localhost:5000"},
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MALFORMED_INPUT


def test_invalid_symbol_characters_keep_invalid_symbol_code(client):
    resp = _post(
        client,
        {"symbol": "../etc/passwd", "market": "us", "shares": 1, "avg_price": 1},
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_SYMBOL


def test_blank_symbol_is_missing(client):
    resp = _post(
        client, {"symbol": "   ", "market": "us", "shares": 1, "avg_price": 1}
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MISSING_REQUIRED_FIELD
    assert body["details"] == {"fields": ["symbol"]}


def test_over_long_symbol_is_invalid_not_missing(client):
    # ``is_valid_symbol`` caps symbols at 15 chars; the schema's Field cap is
    # only a coarse guard.  This must stay INVALID_SYMBOL like the old route.
    resp = _post(
        client,
        {"symbol": "A" * 21, "market": "us", "shares": 1, "avg_price": 1},
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_SYMBOL


def test_non_string_market_is_invalid_market(client):
    resp = _post(
        client, {"symbol": "AAPL", "market": 5, "shares": 1, "avg_price": 1}
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_MARKET


def test_over_limit_avg_price_message_is_unchanged(client):
    resp = _post(
        client,
        {
            "symbol": "AAPL",
            "market": "us",
            "shares": 1,
            "avg_price": 1_000_000_001,
        },
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_INPUT
    assert body["details"]["reason"] == "avg_price must be <= 1000000000"


def test_missing_market_keeps_the_existing_precedence(client):
    # Production has always reported the missing symbol field when the market
    # cannot be normalized, because the symbol is blanked before the market
    # check.  The migration must not "fix" that into INVALID_MARKET.
    resp = _post(client, {"symbol": "AAPL", "shares": 1, "avg_price": 1})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MISSING_REQUIRED_FIELD
    assert body["details"] == {"fields": ["symbol"]}


def test_schema_normalizes_jp_symbol_and_accepts_numeric_strings():
    payload = PortfolioUpdateRequest.model_validate(
        {"symbol": "7203", "market": "JP", "shares": "10.5", "avg_price": "2000"}
    )
    assert payload.symbol == "7203.T"
    assert payload.market == "jp"
    assert payload.shares == 10.5
    assert payload.avg_price == 2000.0


def test_schema_rejects_non_finite_values_directly():
    with pytest.raises(ValueError):
        PortfolioUpdateRequest.model_validate(
            {"symbol": "AAPL", "market": "us", "shares": float("nan"), "avg_price": 1}
        )
    with pytest.raises(ValueError):
        PortfolioUpdateRequest.model_validate(
            {"symbol": "AAPL", "market": "us", "shares": 1, "avg_price": float("inf")}
        )
