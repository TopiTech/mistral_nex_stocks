"""HTTP contract tests for the canonical technical-lines schema (U1/tl slice).

``POST /api/ai-technical-lines`` now validates only through
``schemas.AITechnicalLinesRequest`` (via
``route_helpers._parse_technical_lines_request``). These tests pin the
historic error envelopes (code, message, fields, status) and the
normalization the old inline checks produced, so a future schema edit
cannot silently change the contract. Cases cover: malformed/empty bodies,
per-field types, market/period/history shapes, cross-field precedence, and
the valid path (normalization, server-side fetch fallback, 200 through
mocked providers).
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app import app
from error_codes import ErrorCode
from schemas.analysis import AITechnicalLinesRequest


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _provider_identity():
    with (
        patch(
            "routes.api_analysis.get_model_name", return_value="mistral-medium-2604"
        ),
        patch(
            "routes.api_analysis.extract_api_key",
            return_value="test-api-key-12345678901234567890",
        ),
    ):
        yield


def _post(client, payload=None, raw=None):
    data = raw if raw is not None else json.dumps(payload)
    return client.post(
        "/api/ai-technical-lines",
        data=data,
        content_type="application/json",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        headers={"Origin": "http://localhost:5000"},
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
    return body


@contextmanager
def _mock_generate(result=None, fetch_ret=None):
    """Stack the provider mocks the valid path needs."""
    if result is None:
        result = {"summary": "Uptrend", "trend_bias": "up", "lines": []}
    with (
        patch("routes.api_analysis.generate_ai_technical_lines") as mock_gen,
        patch("routes.api_analysis.fetch_stock") as mock_fetch,
    ):
        mock_gen.return_value = result
        mock_fetch.return_value = fetch_ret
        yield mock_gen, mock_fetch


_MALFORMED = {"reason": "JSONデータが正しく送信されませんでした"}


# ---------------------------------------------------------------------------
# Malformed / empty bodies (the body gate stays in the route)
# ---------------------------------------------------------------------------


def test_malformed_json(client):
    resp = _post(client, raw="{bad json")
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.MALFORMED_INPUT)
    assert body["details"] == _MALFORMED


def test_empty_object_body(client):
    resp = _post(client, {})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.MALFORMED_INPUT)
    assert body["details"] == _MALFORMED


def test_array_body(client):
    resp = _post(client, raw="[1, 2]")
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.MALFORMED_INPUT)
    assert body["details"] == _MALFORMED


# ---------------------------------------------------------------------------
# Symbol / market errors
# ---------------------------------------------------------------------------


def test_missing_symbol(client):
    resp = _post(client, {"market": "us", "period": "3mo"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.MISSING_REQUIRED_FIELD)
    assert body["details"] == {"fields": ["symbol"]}


def test_null_symbol(client):
    resp = _post(client, {"symbol": None, "market": "us"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.MISSING_REQUIRED_FIELD)
    assert body["details"] == {"fields": ["symbol"]}


def test_empty_symbol(client):
    resp = _post(client, {"symbol": "", "market": "us"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.MISSING_REQUIRED_FIELD)
    assert body["details"] == {"fields": ["symbol"]}


def test_symbol_not_string(client):
    resp = _post(client, {"symbol": 1234, "market": "us"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_SYMBOL)
    assert body["details"] == {"fields": ["symbol"]}


def test_market_not_string(client):
    resp = _post(client, {"symbol": "AAPL", "market": 123})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_MARKET)
    assert body["details"] == {}


def test_unknown_market(client):
    resp = _post(client, {"symbol": "AAPL", "market": "not-a-market"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_MARKET)
    assert body["details"] == {}


def test_bad_symbol_shape(client):
    resp = _post(
        client, {"symbol": "../not-a-ticker/with spaces", "market": "us"}
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_SYMBOL)
    assert body["details"] == {}


# ---------------------------------------------------------------------------
# Period / history_data errors
# ---------------------------------------------------------------------------


def test_invalid_period(client):
    resp = _post(client, {"symbol": "AAPL", "market": "us", "period": "not-a-period"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_PERIOD)
    assert body["details"] == {
        "reason": "periodは指定された期間のいずれかである必要があります"
    }


def test_period_not_string(client):
    resp = _post(client, {"symbol": "AAPL", "period": 5})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_PERIOD)
    assert body["details"] == {
        "reason": "periodは指定された期間のいずれかである必要があります"
    }


def test_period_null(client):
    resp = _post(client, {"symbol": "AAPL", "period": None})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_PERIOD)
    assert body["details"] == {
        "reason": "periodは指定された期間のいずれかである必要があります"
    }


def test_history_not_list(client):
    resp = _post(
        client, {"symbol": "AAPL", "period": "3mo", "history_data": {"o": 1}}
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"] == {
        "reason": "history_data must be a list",
        "fields": ["history_data"],
    }


def test_history_too_many(client):
    big = [{"date": "2026-01-01", "price": 1.0}] * 5001
    resp = _post(
        client, {"symbol": "AAPL", "period": "3mo", "history_data": big}
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"] == {
        "reason": "history_data has too many points (max 5000)",
        "fields": ["history_data"],
    }


def test_history_entry_not_object(client):
    resp = _post(
        client,
        {"symbol": "AAPL", "period": "3mo", "history_data": [{"close": 1}, "bad"]},
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"] == {
        "reason": "history_data entries must be objects",
        "fields": ["history_data"],
    }


# ---------------------------------------------------------------------------
# Precedence: the first historic check wins
# ---------------------------------------------------------------------------


def test_missing_symbol_beats_bad_period(client):
    resp = _post(client, {"market": "us", "period": "nope"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.MISSING_REQUIRED_FIELD)
    assert body["details"] == {"fields": ["symbol"]}


def test_bad_symbol_beats_bad_period(client):
    resp = _post(client, {"symbol": "!!!", "period": "nope"})
    assert resp.status_code == 400
    assert _error(resp)["error_code"] == int(ErrorCode.INVALID_SYMBOL)


def test_unknown_market_beats_bad_period(client):
    resp = _post(client, {"symbol": "AAPL", "market": "xx", "period": "nope"})
    assert resp.status_code == 400
    assert _error(resp)["error_code"] == int(ErrorCode.INVALID_MARKET)


def test_symbol_type_beats_market_type(client):
    resp = _post(client, {"symbol": 1, "market": 2})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_SYMBOL)
    assert body["details"] == {"fields": ["symbol"]}


def test_unknown_market_beats_bad_history(client):
    resp = _post(
        client, {"symbol": "AAPL", "market": "xx", "history_data": {"o": 1}}
    )
    assert resp.status_code == 400
    assert _error(resp)["error_code"] == int(ErrorCode.INVALID_MARKET)


def test_bad_period_beats_bad_history(client):
    resp = _post(
        client, {"symbol": "AAPL", "period": "nope", "history_data": {"o": 1}}
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_PERIOD)


def test_empty_symbol_beats_bad_period(client):
    resp = _post(client, {"symbol": "   ", "period": "nope"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.MISSING_REQUIRED_FIELD)
    assert body["details"] == {"fields": ["symbol"]}


# ---------------------------------------------------------------------------
# Valid path: normalization, defaults, fetch fallback, 200
# ---------------------------------------------------------------------------


def test_valid_path_normalizes_and_generates(client):
    history = [{"date": "2026-01-01", "close": 150.0}]
    with _mock_generate() as (mock_gen, mock_fetch):
        resp = _post(
            client,
            {
                "symbol": "aapl",
                "market": "us",
                "period": "1MO",
                "history_data": history,
            },
        )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["summary"] == "Uptrend"
    mock_gen.assert_called_once()
    assert mock_gen.call_args[0][1:5] == ("AAPL", "us", "1mo", history)
    mock_fetch.assert_not_called()


def test_valid_path_jp_qualification(client):
    with _mock_generate() as (mock_gen, _):
        resp = _post(
            client,
            {
                "symbol": "7203",
                "market": "jp",
                "history_data": [{"close": 2500}],
            },
        )
    assert resp.status_code == 200
    assert mock_gen.call_args[0][1:4] == ("7203.T", "jp", "3mo")


def test_valid_path_defaults(client):
    with _mock_generate() as (mock_gen, _):
        resp = _post(
            client, {"symbol": "AAPL", "history_data": [{"close": 1}]}
        )
    assert resp.status_code == 200
    assert mock_gen.call_args[0][1:4] == ("AAPL", "us", "3mo")


def test_valid_path_null_market_defaults_to_us(client):
    with _mock_generate() as (mock_gen, _):
        resp = _post(
            client,
            {"symbol": "AAPL", "market": None, "history_data": [{"close": 1}]},
        )
    assert resp.status_code == 200
    assert mock_gen.call_args[0][2] == "us"


def test_valid_path_null_history_triggers_fetch_fallback(client):
    ohlc = [{"time": 1700000000, "close": 102.0}]
    with _mock_generate(fetch_ret={"ohlc_data": ohlc}) as (mock_gen, mock_fetch):
        resp = _post(client, {"symbol": "AAPL", "history_data": None})
    assert resp.status_code == 200
    mock_fetch.assert_called_once_with("AAPL", None, "us")
    assert mock_gen.call_args[0][4] == ohlc


def test_valid_path_empty_history_falls_back_to_chart_data(client):
    chart = [{"price": 150.0}]
    with _mock_generate(fetch_ret={"chart_data": chart}) as (mock_gen, _):
        resp = _post(client, {"symbol": "AAPL", "history_data": []})
    assert resp.status_code == 200
    assert mock_gen.call_args[0][4] == chart


def test_schema_normalizes_programmatic_input():
    req = AITechnicalLinesRequest(
        symbol="msft",
        market="us",
        period="1mo",
        history_data=[{"close": 400}],
    )
    assert req.symbol == "MSFT"
    assert req.period == "1mo"
    assert req.history_data == [{"close": 400}]
