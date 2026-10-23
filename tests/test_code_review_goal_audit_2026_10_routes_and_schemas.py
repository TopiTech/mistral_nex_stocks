"""Comprehensive unit and regression tests for routes and schemas audit.

Verifies:
1. _parse_stock_history_request & _parse_stock_details_request query parsing and error envelopes.
2. _parse_news_request body/query parameter reconciliation and error envelopes.
3. AIPortfolioItemSchema & AiPortfolioItemSchema symmetry and symbol validation.
4. _sanitize_error_message defense-in-depth raw token redaction.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from flask import Flask
from pydantic import ValidationError
from werkzeug.datastructures import MultiDict

from constants import VALID_HISTORY_INTERVALS
from error_codes import ErrorCode
from route_helpers import (
    _parse_news_request,
    _parse_stock_details_request,
    _parse_stock_history_request,
)
from schemas.ai_portfolio import (
    AIPortfolioItemSchema,
    AiPortfolioItemSchema,
    AIPortfolioResponseSchema,
    AiPortfolioResponseSchema,
)
from utils.text_utils import _sanitize_error_message


def test_schema_symmetry_ai_portfolio():
    """Verify exact class identity and exports symmetry between schemas and validators."""
    import utils.validators as v

    assert v.AiPortfolioItemSchema is AiPortfolioItemSchema
    assert v.AIPortfolioItemSchema is AIPortfolioItemSchema
    assert v.AiPortfolioResponseSchema is AiPortfolioResponseSchema
    assert v.AIPortfolioResponseSchema is AIPortfolioResponseSchema
    assert AiPortfolioItemSchema is AIPortfolioItemSchema
    assert AiPortfolioResponseSchema is AIPortfolioResponseSchema


def test_ai_portfolio_item_schema_symbol_validation():
    """Verify AIPortfolioItemSchema normalizes valid symbols and rejects invalid ones."""
    # Normalization
    item = AIPortfolioItemSchema(
        symbol="  msft  ",
        market="US",  # type: ignore[arg-type]
        risk_level="MID",  # type: ignore[arg-type]
        weight_pct=25.0,
    )
    assert item.symbol == "MSFT"
    assert item.market == "us"
    assert item.risk_level == "mid"

    # Rejection of invalid symbols
    with pytest.raises(ValidationError):
        AIPortfolioItemSchema(symbol="<INVALID>", market="us", weight_pct=10.0)

    with pytest.raises(ValidationError):
        AIPortfolioItemSchema(symbol="", market="us", weight_pct=10.0)


def test_parse_stock_history_request_valid():
    """Verify _parse_stock_history_request parses and normalizes valid query arguments."""
    args = MultiDict([
        ("symbol", "  nvda  "),
        ("market", "us"),
        ("period", "1MO"),
        ("interval", "5M"),
    ])
    parsed, err = _parse_stock_history_request(args)
    assert err is None
    assert parsed is not None
    assert parsed.symbol == "NVDA"
    assert parsed.market == "us"
    assert parsed.period == "1mo"
    assert parsed.interval == "5m"


def test_parse_stock_history_request_symbol_market_normalization():
    """Verify JP symbol qualification and TV prefix removal in history query."""
    # JP numeric symbol
    args_jp = MultiDict([("symbol", "7203"), ("market", "jp")])
    parsed, err = _parse_stock_history_request(args_jp)
    assert err is None
    assert parsed is not None
    assert parsed.symbol == "7203.T"
    assert parsed.period == "3mo"  # default
    assert parsed.interval is None

    # TV prefix
    args_tv = MultiDict([("symbol", "NASDAQ:NVDA"), ("market", "us")])
    parsed_tv, err_tv = _parse_stock_history_request(args_tv)
    assert err_tv is None
    assert parsed_tv is not None
    assert parsed_tv.symbol == "NVDA"


def test_parse_stock_history_request_errors():
    """Verify _parse_stock_history_request maps errors to exact historical envelopes."""
    app = Flask(__name__)
    with app.test_request_context():
        # Missing / empty symbol
        _, err_no_sym = _parse_stock_history_request(MultiDict([("market", "us")]))
        assert err_no_sym is not None
        resp, status = err_no_sym
        assert status == 400
        assert resp.get_json()["error_code"] == int(ErrorCode.INVALID_SYMBOL)

        # Invalid symbol format
        _, err_bad_sym = _parse_stock_history_request(
            MultiDict([("symbol", "../../etc/passwd"), ("market", "us")])
        )
        assert err_bad_sym is not None
        resp, status = err_bad_sym
        assert status == 400
        assert resp.get_json()["error_code"] == int(ErrorCode.INVALID_SYMBOL)

        # Invalid market
        _, err_market = _parse_stock_history_request(
            MultiDict([("symbol", "AAPL"), ("market", "unknown_market")])
        )
        assert err_market is not None
        resp, status = err_market
        assert status == 400
        assert resp.get_json()["error_code"] == int(ErrorCode.INVALID_MARKET)

        # Invalid period
        _, err_period = _parse_stock_history_request(
            MultiDict([("symbol", "AAPL"), ("period", "99y")])
        )
        assert err_period is not None
        resp, status = err_period
        assert status == 400
        assert resp.get_json()["error_code"] == int(ErrorCode.INVALID_PERIOD)

        # Invalid interval
        _, err_interval = _parse_stock_history_request(
            MultiDict([("symbol", "AAPL"), ("interval", "2h")])
        )
        assert err_interval is not None
        resp, status = err_interval
        assert status == 400
        data = resp.get_json()
        assert data["error_code"] == int(ErrorCode.INVALID_INTERVAL)
        assert data["details"]["allowed"] == sorted(VALID_HISTORY_INTERVALS)


def test_parse_stock_details_request_valid_and_errors():
    """Verify _parse_stock_details_request handling of valid and invalid arguments."""
    app = Flask(__name__)
    with app.test_request_context():
        # Valid default market
        parsed, err = _parse_stock_details_request(MultiDict([("symbol", "aapl")]))
        assert err is None
        assert parsed is not None
        assert parsed.symbol == "AAPL"
        assert parsed.market == "us"

        # JP qualification
        parsed_jp, err_jp = _parse_stock_details_request(
            MultiDict([("symbol", "6758"), ("market", "jp")])
        )
        assert err_jp is None
        assert parsed_jp is not None
        assert parsed_jp.symbol == "6758.T"
        assert parsed_jp.market == "jp"

        # Invalid symbol
        _, err_sym = _parse_stock_details_request(MultiDict([("symbol", "   ")]))
        assert err_sym is not None
        resp, status = err_sym
        assert status == 400
        assert resp.get_json()["error_code"] == int(ErrorCode.INVALID_SYMBOL)

        # Invalid market
        _, err_m = _parse_stock_details_request(
            MultiDict([("symbol", "AAPL"), ("market", "europe")])
        )
        assert err_m is not None
        resp, status = err_m
        assert status == 400
        assert resp.get_json()["error_code"] == int(ErrorCode.INVALID_MARKET)


def test_parse_news_request():
    """Verify _parse_news_request parses body force and query force accurately."""
    app = Flask(__name__)

    # Default: empty body, no query
    with app.test_request_context("/api/news", method="POST", json={}):
        req = MagicMock(args=MultiDict())
        force, err = _parse_news_request(req)
        assert err is None
        assert force is False

    # Body force=True
    with app.test_request_context("/api/news", method="POST", json={"force": True}):
        req = MagicMock(args=MultiDict())
        force, err = _parse_news_request(req)
        assert err is None
        assert force is True

    # Query force=true overrides body force=False
    with app.test_request_context("/api/news?force=true", method="POST", json={"force": False}):
        req = MagicMock(args=MultiDict([("force", "true")]))
        force, err = _parse_news_request(req)
        assert err is None
        assert force is True

    # Malformed body (e.g. invalid json)
    with app.test_request_context(
        "/api/news",
        method="POST",
        data='{"force":',
        content_type="application/json",
    ):
        req = MagicMock(args=MultiDict())
        force, err = _parse_news_request(req)
        assert force is None
        assert err is not None
        resp, status = err
        assert status == 400
        assert resp.get_json()["error_code"] == int(ErrorCode.MALFORMED_INPUT)


def test_sanitize_error_message_naked_tokens():
    """Verify _sanitize_error_message redacts naked API keys without labels."""
    # Standalone Mistral / OpenAI key format
    raw_sk = "Error connecting to sk-1234567890abcdef1234567890abcdef from host"
    sanitized_sk = _sanitize_error_message(raw_sk)
    assert "sk-1234567890abcdef1234567890abcdef" not in sanitized_sk
    assert "[REDACTED]" in sanitized_sk

    # Standalone Tavily key format
    raw_tvly = "Tavily search failed: tvly-abcdef1234567890abcdef123456"
    sanitized_tvly = _sanitize_error_message(raw_tvly)
    assert "tvly-abcdef1234567890abcdef123456" not in sanitized_tvly
    assert "[REDACTED]" in sanitized_tvly

    # Safe message without sensitive tokens
    safe_msg = "Stock fetch failed for AAPL (timeout after 5.0s)"
    assert _sanitize_error_message(safe_msg) == safe_msg
