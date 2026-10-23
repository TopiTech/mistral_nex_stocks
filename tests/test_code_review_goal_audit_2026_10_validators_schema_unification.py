"""Unit tests verifying schema unification between utils.validators and schemas.stocks.

Ensures:
- ScreenerFilterSchema inherits ScreenerQueryRequest
- HeatmapFilterSchema inherits HeatmapQueryRequest
- All field validation, bounds checks, boolean rejections, and defaults are uniformly enforced
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from schemas.stocks import HeatmapQueryRequest, ScreenerQueryRequest
from utils.validators import HeatmapFilterSchema, ScreenerFilterSchema


def test_schema_inheritance_hierarchy():
    """Verify clean inheritance and polymorphism."""
    assert issubclass(ScreenerFilterSchema, ScreenerQueryRequest)
    assert issubclass(HeatmapFilterSchema, HeatmapQueryRequest)


def test_screener_schema_defaults_and_overrides():
    """Verify default values and inherited fields."""
    s = ScreenerFilterSchema()
    assert s.market == "all"
    assert s.sector == ""
    assert s.limit == 150
    assert s.sort_by == "market_cap"
    assert s.sort_order == "desc"
    assert s.min_price is None

    # Polymorphic instantiation
    assert isinstance(s, ScreenerQueryRequest)


def test_screener_schema_bounds_and_boolean_rejection():
    """Verify inherited validators operate identically."""
    # Rejection of booleans
    with pytest.raises(ValidationError):
        ScreenerFilterSchema(min_price=True)  # type: ignore[arg-type]

    # Rejection of inverted bounds
    with pytest.raises(ValidationError):
        ScreenerFilterSchema(min_price=100.0, max_price=50.0)

    with pytest.raises(ValidationError):
        ScreenerFilterSchema(min_change=10.0, max_change=-10.0)

    with pytest.raises(ValidationError):
        ScreenerFilterSchema(min_market_cap=1000.0, max_market_cap=500.0)

    with pytest.raises(ValidationError):
        ScreenerFilterSchema(min_pe=50.0, max_pe=10.0)


def test_screener_schema_change_range_bounds():
    """Verify min_change and max_change range bounds (-100 to 10000)."""
    # Valid
    s = ScreenerFilterSchema(min_change=-100.0, max_change=10000.0)
    assert s.min_change == -100.0
    assert s.max_change == 10000.0

    # Below -100
    with pytest.raises(ValidationError):
        ScreenerFilterSchema(min_change=-100.1)

    # Above 10000
    with pytest.raises(ValidationError):
        ScreenerFilterSchema(max_change=10001.0)


def test_heatmap_schema_features():
    """Verify HeatmapFilterSchema inheritance and fields."""
    h = HeatmapFilterSchema(market="jp", group_by="industry", size_by="volume", color_by="pe_ratio")
    assert isinstance(h, HeatmapQueryRequest)
    assert h.market == "jp"
    assert h.group_by == "industry"
    assert h.size_by == "volume"
    assert h.color_by == "pe_ratio"
