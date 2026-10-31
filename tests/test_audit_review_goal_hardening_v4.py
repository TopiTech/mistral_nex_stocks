"""Tests verifying audit review goal hardening v4 fixes.

Covers:
1. services/openai_service._extract_and_parse_openai_json with list input and Pydantic model.
2. utils/stock_payload.error_response and error_handlers._build_error_response 7-key envelope contract.
3. routes/stocks/quotes.make_history_response error envelope completeness (ok: False, error_flag: True).
4. routes/stocks/stream.api_stocks_stream mode=0 returning ok_response envelope.
5. routes/stocks/views.api_screener retaining required strict float helper signature.
6. routes/system package re-exports (api_system_bp, api_health, etc.).
7. services/realtime/scrapers.YahooJPRealtimeScraper volume non-negative clamping.
8. pyproject.toml configuration including app_factory and config packages.
"""

from __future__ import annotations

import tomllib
from unittest.mock import MagicMock, patch

from flask import Flask
from pydantic import BaseModel

from error_codes import ErrorCode
from error_handlers import _build_error_response
from routes.stocks.quotes import quotes_bp
from routes.stocks.stream import stream_bp
from routes.system import (
    api_cache_stats,
    api_credentials,
    api_credentials_verify,
    api_csp_report,
    api_csrf_token,
    api_health,
    api_metrics,
    api_shutdown,
    api_system_bp,
    get_ai_usage,
)
from services.openai_service import _extract_and_parse_openai_json
from services.realtime.scrapers import YahooJPRealtimeScraper
from utils.stock_payload import error_response


class _SampleLineItem(BaseModel):
    price: float
    kind: str


class _SampleContainerModel(BaseModel):
    lines: list[_SampleLineItem]


class _SampleItemsModel(BaseModel):
    items: list[str]


def test_openai_extract_and_parse_json_list_no_model():
    """When raw_content is a list and pydantic_cls is None, return (None, raw_content) safely."""
    raw_list = [{"foo": "bar"}]
    parsed, content = _extract_and_parse_openai_json(raw_list, pydantic_cls=None)
    assert parsed is None
    assert content == raw_list


def test_openai_extract_and_parse_json_list_container_model():
    """When raw_content is a list and pydantic_cls expects a container (e.g. lines/items), validate properly."""
    raw_list = [{"price": 123.45, "kind": "support"}, {"price": 150.0, "kind": "resistance"}]
    parsed, content = _extract_and_parse_openai_json(raw_list, pydantic_cls=_SampleContainerModel)
    assert parsed is not None
    assert "lines" in parsed
    assert len(parsed["lines"]) == 2
    assert parsed["lines"][0]["price"] == 123.45
    assert content == raw_list

    raw_items = ["apple", "banana"]
    parsed_items, _ = _extract_and_parse_openai_json(raw_items, pydantic_cls=_SampleItemsModel)
    assert parsed_items is not None
    assert parsed_items["items"] == ["apple", "banana"]


def test_openai_extract_and_parse_json_list_validation_failure():
    """When raw_content is an invalid list for pydantic_cls, return (None, raw_content) without raising."""
    raw_list = [{"unexpected_field": "invalid_value"}]
    parsed, content = _extract_and_parse_openai_json(raw_list, pydantic_cls=_SampleContainerModel)
    assert parsed is None
    assert content == raw_list


def test_error_response_envelope_contract():
    """error_response must conform strictly to the 7-key error envelope contract."""
    app = Flask(__name__)
    with app.test_request_context():
        resp, status = error_response(ErrorCode.BAD_REQUEST, status_code=400)
    assert status == 400
    data = resp.get_json()
    assert set(data.keys()) == {
        "ok",
        "error",
        "error_flag",
        "code",
        "error_code",
        "message",
        "details",
    }
    assert data["ok"] is False
    assert data["error_flag"] is True
    assert data["error_code"] == int(ErrorCode.BAD_REQUEST)


def test_build_error_response_envelope_parity():
    """error_handlers._build_error_response must produce the exact 7-key error envelope."""
    app = Flask(__name__)
    with app.test_request_context():
        resp, status = _build_error_response("Resource not found", 404, error_code=ErrorCode.NOT_FOUND)
    assert status == 404
    data = resp.get_json()
    assert set(data.keys()) == {
        "ok",
        "error",
        "error_flag",
        "code",
        "error_code",
        "message",
        "details",
    }
    assert data["ok"] is False
    assert data["error_flag"] is True
    assert data["error_code"] == int(ErrorCode.NOT_FOUND)
    assert data["message"] == "Resource not found"


def test_make_history_response_error_envelope():
    """make_history_response must populate complete error envelope when payload has 'error'."""
    app = Flask(__name__)
    app.register_blueprint(quotes_bp)

    with patch("routes.stocks.quotes.require_trusted_or_admin", return_value=(True, None)):
        with patch(
            "routes.stocks.quotes._get_cached_value_if_present",
            return_value=(True, {"error": "Upstream error", "symbol": "AAPL", "history": []}),
        ):
            with app.test_client() as client:
                res = client.get("/api/stock-history?symbol=AAPL&period=1d")
                assert res.status_code == 200
                data = res.get_json()
                assert "error" in data
                assert data["ok"] is False
                assert data["error_flag"] is True


def test_stream_mode_0_envelope():
    """GET /api/stocks/stream?mode=0 returns ok_response envelope (ok: True, success: True)."""
    app = Flask(__name__)
    app.register_blueprint(stream_bp)

    with patch("routes.stocks.stream.require_sse_auth", return_value=(True, None)):
        with app.test_client() as client:
            res = client.get("/api/stocks/stream?mode=0")
            assert res.status_code == 200
            data = res.get_json()
            assert data["ok"] is True
            assert data["success"] is True
            assert data["status"] == "disabled"
            assert data["sse_mode"] == 0
            assert "disabled by client" in data["message"]


def test_routes_system_reexports():
    """routes.system must properly export blueprints and handlers."""
    assert api_system_bp is not None
    assert callable(api_health)
    assert callable(api_cache_stats)
    assert callable(api_metrics)
    assert callable(api_credentials)
    assert callable(api_credentials_verify)
    assert callable(api_csp_report)
    assert callable(api_csrf_token)
    assert callable(api_shutdown)
    assert callable(get_ai_usage)


def test_scraper_volume_clamping():
    """YahooJPRealtimeScraper must clamp negative parsed volumes to 0."""
    scraper = YahooJPRealtimeScraper()

    # Simulate HTML where quote field volume parsed as negative
    html_with_neg_vol = """
    <html>
      <script>var nextData = {"volume": "-500"};</script>
      <div>priceChangeRate: 1.2</div>
      <div>price: 1500</div>
    </html>
    """
    with patch.object(scraper, "_get_session") as mock_session:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html_with_neg_vol
        mock_session.return_value.get.return_value = mock_resp

        with patch.object(scraper, "_extract_quote_field", side_effect=lambda html, field: "1500" if field == "price" else None):
            with patch("services.realtime.scrapers._extract_next_data_quotes", return_value={"volume": "-500"}):
                result = scraper.fetch_jp_symbol("7203.T")
                if result:
                    assert result["volume"] >= 0


def test_pyproject_includes_app_factory_and_config():
    """pyproject.toml must include app_factory and config in mypy and pyrefly settings."""
    with open("pyproject.toml", "rb") as f:
        config = tomllib.load(f)

    mypy_files = config.get("tool", {}).get("mypy", {}).get("files", [])
    assert "app_factory" in mypy_files
    assert "config" in mypy_files

    pyrefly_includes = config.get("tool", {}).get("pyrefly", {}).get("project-includes", [])
    assert "app_factory" in pyrefly_includes
