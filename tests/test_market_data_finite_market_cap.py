"""Derived market caps must stay finite in public JSON responses.

Each input is a valid finite float, but their product can overflow to
``inf``. Flask's default JSON provider emits ``Infinity`` for that value,
which is not valid JSON and breaks ``response.json()`` in the browser. The
market-data layer now clamps the derived metric at its single construction
site.
"""

from __future__ import annotations

import json
import math

from services.market_data_service import _build_market_row, build_heatmap_payload


def test_screener_row_market_cap_is_finite_when_product_overflows() -> None:
    row = _build_market_row(
        "OVERFLOW",
        "us",
        {"price": 1e308, "sharesOutstanding": 10.0},
        "OVERFLOW",
    )

    assert math.isfinite(row["market_cap"])
    assert row["market_cap"] == 0.0
    json.dumps(row, allow_nan=False)


def test_heatmap_omits_rows_with_unrepresentable_market_cap() -> None:
    def _fetch(items, lightweight=True):
        return [{"symbol": "OVERFLOW", "price": 1e308, "sharesOutstanding": 10.0}]

    payload = build_heatmap_payload("us", ["OVERFLOW"], fetch_batch_fn=_fetch)

    assert payload["stocks"] == []
    json.dumps(payload, allow_nan=False)


def test_heatmap_keeps_a_finite_market_cap() -> None:
    def _fetch(items, lightweight=True):
        return [{"symbol": "AAPL", "price": 100.0, "sharesOutstanding": 10.0}]

    payload = build_heatmap_payload("us", ["AAPL"], fetch_batch_fn=_fetch)

    assert len(payload["stocks"]) == 1
    assert payload["stocks"][0]["market_cap"] == 1000.0
    json.dumps(payload, allow_nan=False)
