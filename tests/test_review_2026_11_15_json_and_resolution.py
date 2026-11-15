"""Regression tests for the 2026-11-15 JSON-boundary and resolver review fixes.

The shared JSON parser must reject the non-standard ``NaN``/``Infinity``
literals inside untyped nested containers, while direct scalar fields remain
schema-owned so their historical validation error codes are preserved.  The
stock-response facade must also stop converting an internal ``TypeError`` into
a second call without ``real_data_only``.
"""

from __future__ import annotations

import pytest

from app import app
from routes.stocks import common as stock_common
from utils.text_utils import (
    _contains_nested_non_finite_number,
    _parse_json_request,
    _parse_optional_json_request,
)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_json_boundary_rejects_nested_non_finite_numbers(literal: str) -> None:
    body = ('{"items": [{"price": ' + literal + "}]}").encode("ascii")
    with app.test_request_context(
        "/api/test", method="POST", data=body, content_type="application/json"
    ):
        assert _parse_json_request() is None


def test_json_boundary_rejects_non_finite_values_nested_in_lists_and_dicts() -> None:
    body = b'{"portfolio": {"items": [{"weight": NaN}]}}'
    with app.test_request_context(
        "/api/test", method="POST", data=body, content_type="application/json"
    ):
        assert _parse_json_request() is None


def test_json_boundary_leaves_direct_scalars_to_endpoint_schemas() -> None:
    """A direct scalar keeps the schema's historical INVALID_INPUT error."""
    with app.test_request_context(
        "/api/test",
        method="POST",
        data=b'{"price": Infinity}',
        content_type="application/json",
    ):
        payload = _parse_json_request()
    assert payload is not None
    assert payload["price"] == float("inf")


def test_json_boundary_accepts_finite_numbers() -> None:
    body = b'{"items": [{"price": 1.5}, {"change": -0.25}], "count": 2}'
    with app.test_request_context(
        "/api/test", method="POST", data=body, content_type="application/json"
    ):
        assert _parse_json_request() == {
            "items": [{"price": 1.5}, {"change": -0.25}],
            "count": 2,
        }


def test_optional_json_boundary_does_not_treat_nested_nan_body_as_empty() -> None:
    body = b'{"filters": {"threshold": NaN}}'
    with app.test_request_context(
        "/api/news", method="POST", data=body, content_type="application/json"
    ):
        assert _parse_optional_json_request() is None


def test_contains_nested_non_finite_number_ignores_booleans_and_direct_scalars() -> None:
    assert _contains_nested_non_finite_number({"true": True, "false": False}) is False
    assert _contains_nested_non_finite_number({"price": float("inf")}) is False
    assert _contains_nested_non_finite_number({"value": [1.0, float("inf")]}) is True


def test_stock_resolution_does_not_swallow_internal_type_error(monkeypatch) -> None:
    def broken_resolver(*, include_portfolio=False, real_data_only=False):
        del include_portfolio, real_data_only
        raise TypeError("malformed row inside resolver")

    monkeypatch.setattr(
        stock_common._payload_mod, "_resolve_stocks_for_response", broken_resolver
    )

    with pytest.raises(TypeError, match="malformed row inside resolver"):
        stock_common.resolve_stocks_for_response(real_data_only=True)
