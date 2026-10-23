"""HTTP contract tests for canonical stock-mutation schemas (U1/A-03 slice).

``POST /api/stocks/add``, ``POST|DELETE /api/stocks/delete`` and
``POST /api/stocks/add_ext`` now validate only through
``schemas.stocks.StockAddRequest / StockDeleteRequest / StockAddExtRequest``
(via ``route_helpers._parse_stock_*_request``). These tests pin the historic
error envelope so a future schema edit cannot silently change an error code,
message, or status code, and lock the unified success envelope (A-06).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app import app
from app_state import app_state
from constants import MAX_STOCK_NAME_LENGTH
from error_codes import ErrorCode
from schemas.stocks import StockAddExtRequest, StockAddRequest, StockDeleteRequest


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def reset_watchlist_state():
    with app_state.market.user_stocks_lock:
        app_state.market.user_us.clear()
        app_state.market.user_jp.clear()
        app_state.market.user_idx.clear()
    yield
    with app_state.market.user_stocks_lock:
        app_state.market.user_us.clear()
        app_state.market.user_jp.clear()
        app_state.market.user_idx.clear()


def _post(client, path, payload, **kwargs):
    base = {
        "environ_base": {"REMOTE_ADDR": "127.0.0.1"},
        "headers": {"Origin": "http://localhost:5000"},
    }
    base["headers"].update(kwargs.pop("headers", {}))
    base.update(kwargs)
    return client.post(
        path,
        data=json.dumps(payload),
        content_type="application/json",
        **base,
    )


def _error(resp):
    body = resp.get_json()
    assert body is not None
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


def test_add_missing_symbol_is_missing_required_field(client):
    with patch("utils.storage.save_user_stocks"):
        resp = _post(client, "/api/stocks/add", {"name": "Apple", "market": "us"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MISSING_REQUIRED_FIELD
    assert body["details"] == {"fields": ["symbol"]}


def test_add_blank_symbol_is_missing_not_invalid(client):
    with patch("utils.storage.save_user_stocks"):
        resp = _post(client, "/api/stocks/add", {"symbol": "   ", "name": "x", "market": "us"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MISSING_REQUIRED_FIELD
    assert body["details"] == {"fields": ["symbol"]}


def test_add_missing_market_reports_missing_symbol_first(client):
    # Historic precedence: the symbol is blanked when the market cannot be
    # normalized, so the missing-symbol error wins over INVALID_MARKET.
    with patch("utils.storage.save_user_stocks"):
        resp = _post(client, "/api/stocks/add", {"symbol": "AAPL", "name": "Apple"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MISSING_REQUIRED_FIELD
    assert body["details"] == {"fields": ["symbol"]}


def test_add_invalid_market_value_reports_missing_symbol_first(client):
    # Historic precedence (same as portfolio): an unnormalizable market blanks
    # the symbol before the market check, so MISSING symbol wins. Preserved
    # byte-identical; do not "fix" into INVALID_MARKET in this unit.
    with patch("utils.storage.save_user_stocks"):
        resp = _post(
            client,
            "/api/stocks/add",
            {"symbol": "AAPL", "name": "Apple", "market": "xx"},
        )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MISSING_REQUIRED_FIELD
    assert body["details"] == {"fields": ["symbol"]}


def test_add_non_string_market_is_invalid_market(client):
    with patch("utils.storage.save_user_stocks"):
        resp = _post(
            client, "/api/stocks/add", {"symbol": "AAPL", "name": "Apple", "market": 5}
        )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_MARKET


def test_add_non_string_symbol_is_invalid_symbol(client):
    with patch("utils.storage.save_user_stocks"):
        resp = _post(
            client, "/api/stocks/add", {"symbol": 123, "name": "Apple", "market": "us"}
        )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_SYMBOL


def test_add_non_string_name_keeps_legacy_message(client):
    with patch("utils.storage.save_user_stocks"):
        resp = _post(
            client, "/api/stocks/add", {"symbol": "AAPL", "name": 123, "market": "us"}
        )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_INPUT
    assert body["details"] == {"reason": "name must be a string"}


def test_add_missing_name_is_missing_required_field(client):
    with patch("utils.storage.save_user_stocks"):
        resp = _post(client, "/api/stocks/add", {"symbol": "AAPL", "market": "us"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MISSING_REQUIRED_FIELD
    assert body["details"] == {"fields": ["name"]}


def test_add_long_name_keeps_japanese_unsafe_input(client):
    with patch("utils.storage.save_user_stocks"):
        resp = _post(
            client,
            "/api/stocks/add",
            {"symbol": "AAPL", "name": "x" * (MAX_STOCK_NAME_LENGTH + 1), "market": "us"},
        )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.UNSAFE_INPUT
    assert body["details"] == {
        "reason": f"nameは{MAX_STOCK_NAME_LENGTH}文字以下である必要があります"
    }


def test_add_invalid_symbol_characters(client):
    with patch("utils.storage.save_user_stocks"):
        resp = _post(
            client,
            "/api/stocks/add",
            {"symbol": "AAPL!", "name": "Apple", "market": "us"},
        )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_SYMBOL


def test_add_path_traversal_symbol_is_invalid(client):
    with patch("utils.storage.save_user_stocks"):
        resp = _post(
            client,
            "/api/stocks/add",
            {"symbol": "../x", "name": "Apple", "market": "us"},
        )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_SYMBOL


def test_add_malformed_json_is_rejected(client):
    with patch("utils.storage.save_user_stocks"):
        resp = client.post(
            "/api/stocks/add",
            data="{not json",
            content_type="application/json",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            headers={"Origin": "http://localhost:5000"},
        )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MALFORMED_INPUT


def test_add_success_envelope_has_both_keys(client):
    import uuid

    symbol = f"T{uuid.uuid4().hex[:6].upper()}"
    with patch("utils.storage.save_user_stocks"):
        resp = _post(
            client,
            "/api/stocks/add",
            {"symbol": symbol, "name": "Test", "market": "us"},
        )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {"ok": True, "success": True}


def test_delete_missing_symbol_is_missing_required_field(client):
    with patch("utils.storage.save_user_stocks"):
        resp = _post(client, "/api/stocks/delete", {"market": "us"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MISSING_REQUIRED_FIELD
    assert body["details"] == {"fields": ["symbol"]}


def test_delete_success_envelope_has_both_keys(client):
    import uuid

    symbol = f"T{uuid.uuid4().hex[:6].upper()}"
    with (
        patch("utils.storage.save_user_stocks"),
        app_state.market.user_stocks_lock,
    ):
        app_state.market.user_us[symbol] = "Test"
    with patch("utils.storage.save_user_stocks"):
        resp = _post(client, "/api/stocks/delete", {"symbol": symbol, "market": "us"})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "success": True}


def test_add_ext_missing_name_still_accepted_with_display_fallback(client):
    from route_helpers import _parse_stock_add_ext_request

    parsed, error = _parse_stock_add_ext_request({"symbol": "AAPL", "market": "us"})
    assert error is None
    assert parsed is not None
    assert parsed["symbol"] == "AAPL"
    assert parsed["market"] == "us"
    assert parsed["name"] == ""


def test_schema_normalization_matches_historic_ingress():
    assert StockAddRequest.model_validate(
        {"symbol": "aapl", "name": "Apple", "market": "us"}
    ).symbol == "AAPL"
    assert StockAddRequest.model_validate(
        {"symbol": "7203", "name": "Toyota", "market": "jp"}
    ).symbol == "7203.T"
    assert StockDeleteRequest.model_validate(
        {"symbol": "7203", "market": "jp"}
    ).symbol == "7203.T"
    ext = StockAddExtRequest.model_validate({"symbol": "7203.t"})
    assert ext.symbol == "7203.T"
    assert ext.market == "us"
    assert ext.name is None
    # Fullwidth ingress is NFKC-normalized before persistence.
    assert (
        StockAddRequest.model_validate(
            {"symbol": "ＡＡＰＬ", "name": "Apple", "market": "us"}
        ).symbol
        == "AAPL"
    )
