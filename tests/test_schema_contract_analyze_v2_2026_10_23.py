"""HTTP contract tests for the canonical analyze-v2 schema (U1/analyze-v2 slice).

``POST /api/analyze-v2`` now validates only through
``schemas.AIAnalyzeV2Request`` (via
``route_helpers._parse_analyze_v2_request``). These tests pin the historic
error envelopes (code, message, fields, status) and the normalization the
old inline checks produced, so a future schema edit cannot silently change
the contract. Cases cover: malformed JSON, per-field types, name length,
chart_data shape/count, price shape/range (bool, string, inf, negative),
market/symbol presence and shape, token shape, cross-field precedence, and
the valid path (normalization + 200 through mocked providers).
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app import app
from constants import MAX_STOCK_NAME_LENGTH
from error_codes import ErrorCode
from schemas.analysis import AIAnalyzeV2Request


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _dummy_api_key():
    with patch("routes.api_analysis.extract_api_key", return_value="dummy-key"):
        yield


def _tok(tag: str) -> str:
    return f"a2-{tag}-" + "z" * 20


def _post(client, payload=None, raw=None):
    data = raw if raw is not None else json.dumps(payload)
    return client.post(
        "/api/analyze-v2",
        data=data,
        content_type="application/json",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        headers={
            "Origin": "http://localhost:5000",
            "Authorization": "Bearer dummy-key",
        },
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


def _llm_result(summary="Strong growth"):
    return {
        "choices": [
            {
                "message": {
                    "content": {
                        "recommendation": "買い",
                        "sentiment": "強気",
                        "target_price_3m": 180.0,
                        "upside_3m": "+20%",
                        "confidence": "高",
                        "analysis_summary": summary,
                        "key_catalysts": ["AI demand surge"],
                        "risk_factors": ["Competition"],
                        "technical_analysis": "Bullish trend",
                        "fundamental_analysis": "Solid fundamentals",
                        "latest_news_impact": "Positive earnings report",
                    }
                }
            }
        ]
    }


@contextmanager
def _noop_context(fetch_ret=None):
    """Stack the provider mocks the valid path needs (mirrors test_core_logic)."""
    if fetch_ret is None:
        fetch_ret = {"price": 150.0, "chart_data": [{"price": 150.0}]}
    with (
        patch("routes.api_analysis.get_stock_info_cached") as mock_info,
        patch("routes.api_analysis.collect_symbol_research_context") as mock_collect,
        patch("routes.api_analysis.fetch_stock") as mock_fetch,
        patch("routes.api_analysis.call_mistral_chat") as mock_llm,
    ):
        mock_info.return_value = {
            "sector": "Technology",
            "industry": "Consumer Electronics",
            "currency": "USD",
        }
        mock_collect.return_value = "dummy research context"
        mock_fetch.return_value = fetch_ret
        mock_llm.return_value = _llm_result()
        yield mock_info, mock_collect, mock_fetch, mock_llm


# ---------------------------------------------------------------------------
# Malformed body
# ---------------------------------------------------------------------------


def test_malformed_json(client):
    resp = _post(client, raw="{bad json")
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.MALFORMED_INPUT)
    assert body["details"] == {"reason": "JSON形式が不正です"}


# ---------------------------------------------------------------------------
# Per-field type / shape errors (route order: symbol, market, name, chart, price)
# ---------------------------------------------------------------------------


def test_symbol_not_string(client):
    resp = _post(client, {"symbol": 123, "request_token": _tok("symtype")})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_SYMBOL)
    assert body["details"] == {}


def test_market_not_string(client):
    resp = _post(
        client, {"symbol": "AAPL", "market": 123, "request_token": _tok("mktype")}
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_MARKET)
    assert body["details"] == {}


def test_name_not_string(client):
    resp = _post(
        client, {"symbol": "AAPL", "name": 123, "request_token": _tok("nametype")}
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"] == {"reason": "name must be a string"}


def test_name_too_long(client):
    resp = _post(
        client,
        {
            "symbol": "AAPL",
            "name": "x" * (MAX_STOCK_NAME_LENGTH + 1),
            "request_token": _tok("namelen"),
        },
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.UNSAFE_INPUT)
    assert body["details"] == {
        "reason": f"nameは{MAX_STOCK_NAME_LENGTH}文字以下である必要があります"
    }


def test_chart_data_not_list(client):
    resp = _post(
        client,
        {"symbol": "AAPL", "chart_data": "x", "request_token": _tok("charttype")},
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"] == {
        "reason": "chart_data must be a list",
        "fields": ["chart_data"],
    }


def test_chart_data_too_many(client):
    resp = _post(
        client,
        {
            "symbol": "AAPL",
            "chart_data": [{"close": 1}] * 5001,
            "request_token": _tok("chartmany"),
        },
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"] == {
        "reason": "chart_data has too many points (max 5000)",
        "fields": ["chart_data"],
    }


def test_chart_data_entry_not_object(client):
    resp = _post(
        client,
        {
            "symbol": "AAPL",
            "chart_data": [{"close": 1}, 5],
            "request_token": _tok("chartentry"),
        },
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"] == {
        "reason": "chart_data entries must be objects",
        "fields": ["chart_data"],
    }


def test_price_bool_rejected(client):
    resp = _post(
        client, {"symbol": "AAPL", "price": True, "request_token": _tok("pricebool")}
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"] == {
        "reason": "price must be a finite number",
        "fields": ["price"],
    }


def test_price_string_rejected(client):
    resp = _post(
        client, {"symbol": "AAPL", "price": "123", "request_token": _tok("pricestr")}
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"] == {
        "reason": "price must be a finite number",
        "fields": ["price"],
    }


def test_price_negative(client):
    resp = _post(
        client, {"symbol": "AAPL", "price": -1, "request_token": _tok("priceneg")}
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"] == {
        "reason": "price must be a finite non-negative number",
        "fields": ["price"],
    }


def test_price_infinite(client):
    raw = json.dumps({"symbol": "AAPL", "price": float("inf")})
    # Inject the token without re-serializing the non-finite float.
    raw = raw[:-1] + f', "request_token": "{_tok("priceinf")}"}}'
    resp = _post(client, raw=raw)
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"] == {
        "reason": "price must be a finite non-negative number",
        "fields": ["price"],
    }


# ---------------------------------------------------------------------------
# Presence / shape / token errors
# ---------------------------------------------------------------------------


def test_unknown_market(client):
    resp = _post(
        client, {"symbol": "AAPL", "market": "xx", "request_token": _tok("mktxx")}
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_MARKET)
    assert body["details"] == {}


def test_missing_symbol(client):
    resp = _post(client, {"market": "us", "request_token": _tok("nosym")})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.MISSING_REQUIRED_FIELD)
    assert body["details"] == {"fields": ["symbol"]}


def test_bad_symbol_shape(client):
    resp = _post(client, {"symbol": "!!!", "request_token": _tok("badsym")})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_SYMBOL)
    assert body["details"] == {}


def test_missing_token(client):
    resp = _post(client, {"symbol": "AAPL"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"] == {
        "reason": "request_token must be a 16-128 character URL-safe token"
    }


def test_bad_token(client):
    resp = _post(client, {"symbol": "AAPL", "request_token": "short"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"] == {
        "reason": "request_token must be a 16-128 character URL-safe token"
    }


# ---------------------------------------------------------------------------
# Precedence: the first historic check wins
# ---------------------------------------------------------------------------


def test_symbol_type_beats_market_type(client):
    resp = _post(client, {"symbol": 1, "market": 2, "request_token": _tok("p1")})
    assert resp.status_code == 400
    assert _error(resp)["error_code"] == int(ErrorCode.INVALID_SYMBOL)


def test_name_length_beats_unknown_market(client):
    resp = _post(
        client,
        {
            "symbol": "AAPL",
            "market": "xx",
            "name": "y" * (MAX_STOCK_NAME_LENGTH + 1),
            "request_token": _tok("p2"),
        },
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.UNSAFE_INPUT)
    assert "文字以下である必要があります" in body["details"]["reason"]


def test_unknown_market_beats_bad_symbol(client):
    resp = _post(
        client, {"symbol": "!!!", "market": "xx", "request_token": _tok("p3")}
    )
    assert resp.status_code == 400
    assert _error(resp)["error_code"] == int(ErrorCode.INVALID_MARKET)


def test_price_range_beats_unknown_market(client):
    resp = _post(
        client, {"symbol": "AAPL", "market": "xx", "price": -5, "request_token": _tok("p4")}
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"]["reason"] == "price must be a finite non-negative number"


def test_missing_symbol_beats_bad_token(client):
    resp = _post(client, {"market": "us", "request_token": "bad"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.MISSING_REQUIRED_FIELD)
    assert body["details"] == {"fields": ["symbol"]}


def test_bad_symbol_beats_bad_token(client):
    resp = _post(client, {"symbol": "!!!", "request_token": "bad"})
    assert resp.status_code == 400
    assert _error(resp)["error_code"] == int(ErrorCode.INVALID_SYMBOL)


# ---------------------------------------------------------------------------
# Valid path: normalization is preserved and the job succeeds
# ---------------------------------------------------------------------------


def test_valid_path_normalizes_symbol_and_name(client):
    with _noop_context() as mocks:
        _, _, mock_fetch, _ = mocks
        resp = _post(
            client,
            {
                "symbol": "aapl",
                "market": "us",
                "name": "  Apple  ",
                "price": 220.5,
                "chart_data": [{"close": 220}],
                "request_token": _tok("ok1"),
            },
        )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["version"] == "v2-structured-pydantic-2026"
    assert data["analysis_summary"] == "Strong growth"
    mock_fetch.assert_called_once()
    assert mock_fetch.call_args[0][:3] == ("AAPL", "Apple", "us")


def test_valid_path_jp_qualification(client):
    with _noop_context() as mocks:
        _, _, mock_fetch, _ = mocks
        resp = _post(
            client, {"symbol": "7203", "market": "jp", "request_token": _tok("ok2")}
        )
    assert resp.status_code == 200
    assert mock_fetch.call_args[0][0] == "7203.T"
    assert mock_fetch.call_args[0][2] == "jp"


def test_valid_path_fullwidth_symbol(client):
    with _noop_context():
        resp = _post(
            client, {"symbol": "ａａｐｌ", "request_token": _tok("ok3")}
        )
    assert resp.status_code == 200
    assert resp.get_json()["analysis_summary"] == "Strong growth"


def test_valid_path_none_chart_and_zero_price(client):
    with _noop_context():
        resp = _post(
            client,
            {
                "symbol": "AAPL",
                "chart_data": None,
                "price": 0,
                "request_token": _tok("ok4"),
            },
        )
    assert resp.status_code == 200


def test_valid_path_empty_market_defaults_to_us(client):
    with _noop_context() as mocks:
        _, _, mock_fetch, _ = mocks
        resp = _post(
            client, {"symbol": "AAPL", "market": "", "request_token": _tok("ok5")}
        )
    assert resp.status_code == 200
    assert mock_fetch.call_args[0][2] == "us"


def test_valid_path_max_length_name(client):
    with _noop_context():
        resp = _post(
            client,
            {
                "symbol": "AAPL",
                "name": "n" * MAX_STOCK_NAME_LENGTH,
                "request_token": _tok("ok6"),
            },
        )
    assert resp.status_code == 200


def test_valid_path_client_fallback_when_server_fetch_empty(client):
    with _noop_context(fetch_ret={}) as mocks:
        _, _, mock_fetch, _ = mocks
        resp = _post(
            client,
            {
                "symbol": "AAPL",
                "price": 111.0,
                "chart_data": [{"close": 111}],
                "request_token": _tok("ok7"),
            },
        )
    assert resp.status_code == 200
    assert mock_fetch.call_args[0][0] == "AAPL"


def test_schema_preserves_int_price_type():
    req = AIAnalyzeV2Request.model_validate(
        {"symbol": "AAPL", "price": 150, "request_token": _tok("ptype")}
    )
    assert req.price == 150
    assert isinstance(req.price, int)
