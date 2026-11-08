"""Regression tests for API-boundary contract defects found in the 2026-11-08 review.

Coverage:
* ``upside_3m`` must leave the analysis normalizer as a string even when the
  upstream model returns a number (the UI calls ``String.prototype.includes``).
* An invalid optional ``request_token`` must be reported as a request-token
  failure, not as a missing/invalid theme.
* ``error_response`` must sanitize the top-level ``error``/``message`` fields,
  not only ``details``/``extra_fields``.
"""

from __future__ import annotations

import json

from flask import Flask

from error_codes import ErrorCode
from route_helpers import (
    _parse_ai_generate_request,
    _parse_ai_rebalance_request,
)
from utils.stock_payload import error_response
from utils.validators import normalize_analysis_result


def _body_and_status(result):
    response, status = result
    return response.get_json(), status


def _parse_in_app_context(parser, payload):
    with Flask(__name__).test_request_context("/"):
        return parser(payload)


def test_numeric_upside_3m_is_normalized_to_string():
    normalized = normalize_analysis_result(
        {
            "analysis_summary": "strong momentum",
            "recommendation": "buy",
            "sentiment": "bullish",
            "upside_3m": 12.5,
        }
    )

    assert isinstance(normalized["upside_3m"], str), (
        "the analysis API contract requires upside_3m to be a string; "
        f"got {type(normalized['upside_3m']).__name__}"
    )
    assert normalized["upside_3m"] == "+12.5%"


def test_numeric_negative_upside_3m_keeps_sign():
    normalized = normalize_analysis_result(
        {"analysis_summary": "weak", "upside_3m": -8.25}
    )

    assert normalized["upside_3m"] == "-8.25%"


def test_ai_portfolio_generate_invalid_request_token_reports_token():
    parsed, err = _parse_in_app_context(
        _parse_ai_generate_request, {"theme": "tech", "request_token": "short"}
    )

    assert parsed is None
    assert err is not None
    body, status = _body_and_status(err)
    assert status == 400
    assert body["details"]["fields"] == ["request_token"]
    assert "request_token" in body["details"]["reason"]


def test_ai_portfolio_generate_overlong_request_token_reports_token():
    parsed, err = _parse_in_app_context(
        _parse_ai_generate_request, {"theme": "tech", "request_token": "x" * 129}
    )

    assert parsed is None
    assert err is not None
    body, status = _body_and_status(err)
    assert status == 400
    assert body["details"]["fields"] == ["request_token"]


def test_ai_portfolio_rebalance_invalid_request_token_reports_token():
    parsed, err = _parse_in_app_context(
        _parse_ai_rebalance_request, {"theme": "tech", "request_token": "short"}
    )

    assert parsed is None
    assert err is not None
    body, status = _body_and_status(err)
    assert status == 400
    assert body["details"]["fields"] == ["request_token"]
    assert "request_token" in body["details"]["reason"]


def test_error_response_sanitizes_top_level_message():
    secret = "sk-" + "a" * 24

    with Flask(__name__).test_request_context("/"):
        body, status = _body_and_status(
            error_response(
                ErrorCode.FETCH_FAILED,
                message=f"upstream failure api_key={secret}",
            )
        )

    assert status == 400
    assert secret not in json.dumps(body, ensure_ascii=False)
    assert "[REDACTED]" in body["error"]
    assert body["error"] == body["message"]
