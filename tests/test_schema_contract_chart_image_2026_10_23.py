"""HTTP contract tests for the canonical chart-image schema (U1/ci slice).

``POST /api/analyze-chart-image`` now validates only through
``schemas.AIAnalyzeChartImageRequest`` (via
``route_helpers._parse_chart_image_request``). These tests pin the historic
error envelopes (code, message, fields, status) and the normalization the
old inline checks produced, so a future schema edit cannot silently change
the contract. Cases cover: malformed bodies, image presence/shape/size, the
Data-URI vs Base64 branch checks, symbol/market/prompt errors,
cross-field precedence, and the valid path (normalization + 200 through a
mocked vision service).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app import app
from constants import CHART_IMAGE_MAX_IMAGE_DATA_CHARS
from error_codes import ErrorCode
from schemas.analysis import AIAnalyzeChartImageRequest


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


PNG_URI = "data:image/png;base64,iVBORw0KGgo="
RAW_B64 = "iVBORw0KGgo="


def _post(client, payload=None, raw=None):
    data = raw if raw is not None else json.dumps(payload)
    return client.post(
        "/api/analyze-chart-image",
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


def _mock_vision(result=None):
    return patch(
        "routes.api_analysis.analyze_chart_image_with_mistral", return_value=result
    )


# ---------------------------------------------------------------------------
# Body / auth gates (stay in the route)
# ---------------------------------------------------------------------------


def test_malformed_json(client):
    resp = _post(client, raw="{bad json")
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.BAD_REQUEST)
    assert body["details"] == {}


def test_missing_api_key(client):
    with patch("routes.api_analysis.extract_api_key", return_value=""):
        resp = _post(client, {"image_data": RAW_B64})
    assert resp.status_code == 401
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.API_AUTH_FAILED)


# ---------------------------------------------------------------------------
# Image presence / shape / size
# ---------------------------------------------------------------------------


def test_missing_image(client):
    resp = _post(client, {"symbol": "AAPL"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"] == {
        "reason": "image_data (Base64またはData URI) が必要です"
    }


def test_empty_images(client):
    resp = _post(client, {"image_data": "", "image": ""})
    assert resp.status_code == 400
    assert _error(resp)["details"] == {
        "reason": "image_data (Base64またはData URI) が必要です"
    }


def test_image_not_string(client):
    resp = _post(client, {"image_data": 123})
    assert resp.status_code == 400
    assert _error(resp)["details"] == {
        "reason": "image_data (Base64またはData URI) が必要です"
    }


def test_image_whitespace_only(client):
    resp = _post(client, {"image_data": "   "})
    assert resp.status_code == 400
    assert _error(resp)["details"] == {
        "reason": "image_data (Base64またはData URI) が必要です"
    }


def test_image_too_large(client):
    resp = _post(client, {"image_data": "A" * (CHART_IMAGE_MAX_IMAGE_DATA_CHARS + 1)})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"] == {"reason": "image_dataが大きすぎます（上限約5MB）"}


# ---------------------------------------------------------------------------
# Data-URI branch
# ---------------------------------------------------------------------------


def test_data_uri_without_comma(client):
    resp = _post(client, {"image_data": "data:foo"})
    assert resp.status_code == 400
    assert _error(resp)["details"] == {
        "reason": "image_dataはimage/*のData URI形式である必要があります"
    }


def test_data_uri_non_image_header(client):
    resp = _post(client, {"image_data": "data:text/plain;base64,AAA"})
    assert resp.status_code == 400
    assert _error(resp)["details"] == {
        "reason": "image_dataはimage/*のData URI形式である必要があります"
    }


def test_svg_rejected(client):
    resp = _post(client, {"image_data": "data:image/svg+xml;base64,AAA"})
    assert resp.status_code == 400
    assert _error(resp)["details"] == {"reason": "SVG画像はサポートされていません"}


def test_unsupported_mime(client):
    resp = _post(client, {"image_data": "data:image/bmp;base64,AAA"})
    assert resp.status_code == 400
    assert _error(resp)["details"] == {
        "reason": "対応形式は png/jpeg/gif/webp のbase64 Data URIのみです"
    }


def test_mime_without_base64_flag(client):
    resp = _post(client, {"image_data": "data:image/png,AAA"})
    assert resp.status_code == 400
    assert _error(resp)["details"] == {
        "reason": "対応形式は png/jpeg/gif/webp のbase64 Data URIのみです"
    }


def test_empty_b64_part(client):
    resp = _post(client, {"image_data": "data:image/png;base64,   "})
    assert resp.status_code == 400
    assert _error(resp)["details"] == {
        "reason": "image_dataのData URIはbase64形式である必要があります"
    }


# ---------------------------------------------------------------------------
# Raw Base64 branch
# ---------------------------------------------------------------------------


def test_url_rejected(client):
    resp = _post(client, {"image_data": "https://example.com/x.png"})
    assert resp.status_code == 400
    assert _error(resp)["details"] == {
        "reason": "URL形式のimage_dataは許可されていません。"
        "Data URIまたはBase64で送信してください"
    }


def test_uppercase_scheme_rejected(client):
    resp = _post(client, {"image_data": "DATA:foo"})
    assert resp.status_code == 400
    assert _error(resp)["details"] == {
        "reason": "image_dataのData URI形式が不正です"
    }


def test_non_base64_rejected(client):
    resp = _post(client, {"image_data": "hello world!!"})
    assert resp.status_code == 400
    assert _error(resp)["details"] == {
        "reason": "image_dataはBase64形式である必要があります"
    }


# ---------------------------------------------------------------------------
# Symbol / market / prompt errors
# ---------------------------------------------------------------------------


def test_symbol_not_string(client):
    resp = _post(client, {"image_data": RAW_B64, "symbol": 123})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_SYMBOL)
    assert body["details"] == {}


def test_symbol_bad_shape(client):
    resp = _post(client, {"image_data": RAW_B64, "symbol": "!!!"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_SYMBOL)
    assert body["details"] == {}


def test_market_not_string(client):
    resp = _post(client, {"image_data": RAW_B64, "market": 123})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_MARKET)
    assert body["details"] == {}


def test_unknown_market(client):
    resp = _post(client, {"image_data": RAW_B64, "market": "xx"})
    assert resp.status_code == 400
    assert _error(resp)["error_code"] == int(ErrorCode.INVALID_MARKET)


def test_prompt_not_string(client):
    resp = _post(client, {"image_data": RAW_B64, "prompt": 5})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"] == {
        "reason": "prompt must be a string",
        "fields": ["prompt"],
    }


def test_prompt_too_long(client):
    resp = _post(client, {"image_data": RAW_B64, "prompt": "x" * 2001})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"] == {
        "reason": "promptは2000文字以内で入力してください",
        "fields": ["prompt"],
    }


# ---------------------------------------------------------------------------
# Precedence: the first historic check wins
# ---------------------------------------------------------------------------


def test_image_error_beats_bad_symbol(client):
    resp = _post(client, {"image_data": "!!!", "symbol": "!!!"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INVALID_INPUT)
    assert body["details"]["reason"] == "image_dataはBase64形式である必要があります"


def test_image_too_large_beats_bad_symbol(client):
    resp = _post(
        client,
        {"image_data": "A" * (CHART_IMAGE_MAX_IMAGE_DATA_CHARS + 1), "symbol": "!!!"},
    )
    assert resp.status_code == 400
    assert _error(resp)["details"] == {
        "reason": "image_dataが大きすぎます（上限約5MB）"
    }


def test_unknown_market_beats_bad_symbol_shape(client):
    resp = _post(
        client, {"image_data": RAW_B64, "symbol": "!!!", "market": "xx"}
    )
    assert resp.status_code == 400
    assert _error(resp)["error_code"] == int(ErrorCode.INVALID_MARKET)


def test_unknown_market_beats_bad_prompt(client):
    resp = _post(
        client, {"image_data": RAW_B64, "market": "xx", "prompt": "x" * 2001}
    )
    assert resp.status_code == 400
    assert _error(resp)["error_code"] == int(ErrorCode.INVALID_MARKET)


def test_symbol_type_beats_bad_prompt(client):
    resp = _post(client, {"image_data": RAW_B64, "symbol": 7, "prompt": 8})
    assert resp.status_code == 400
    assert _error(resp)["error_code"] == int(ErrorCode.INVALID_SYMBOL)


# ---------------------------------------------------------------------------
# Valid path
# ---------------------------------------------------------------------------


def test_valid_data_uri_normalizes_and_analyzes(client):
    result = {"summary": "Uptrend", "patterns": []}
    with _mock_vision(result) as mock_vision:
        resp = _post(
            client,
            {
                "image_data": PNG_URI,
                "symbol": "aapl",
                "market": "us",
                "prompt": "  focus on trend  ",
            },
        )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["summary"] == "Uptrend"
    mock_vision.assert_called_once_with(
        "dummy-key", PNG_URI, symbol="AAPL", market="us", custom_prompt="focus on trend"
    )


def test_valid_raw_base64_via_alias_and_jp(client):
    with _mock_vision({"ok": True}) as mock_vision:
        resp = _post(client, {"image": RAW_B64, "symbol": "7203", "market": "jp"})
    assert resp.status_code == 200
    assert mock_vision.call_args[0][1] == RAW_B64
    assert mock_vision.call_args[1] == {
        "symbol": "7203.T",
        "market": "jp",
        "custom_prompt": "",
    }


def test_valid_base64_with_whitespace_and_defaults(client):
    padded = "  iVBOR\nw0KGgo=  "
    with _mock_vision({"ok": True}) as mock_vision:
        resp = _post(client, {"image_data": padded})
    assert resp.status_code == 200
    assert mock_vision.call_args[0][1] == "iVBOR\nw0KGgo="
    assert mock_vision.call_args[1] == {"symbol": "", "market": "us", "custom_prompt": ""}


def test_valid_service_error_maps_to_500(client):
    with _mock_vision({"error": "boom"}) as mock_vision:
        resp = _post(client, {"image_data": RAW_B64})
    assert resp.status_code == 500
    body = _error(resp)
    assert body["error_code"] == int(ErrorCode.INTERNAL_SERVER_ERROR)
    assert mock_vision.called


def test_schema_merges_and_strips_image_alias():
    req = AIAnalyzeChartImageRequest.model_validate(
        {"image": f"  {RAW_B64}  ", "symbol": "aapl"}
    )
    assert req.image_data == RAW_B64
    assert req.symbol == "AAPL"
    assert req.market == "us"
    assert req.prompt == ""
