"""Unit tests verifying scraper market state dynamic resolution and cache invalidation.

Covers:
- Dynamic resolution of app_state.market without stale global caching
- Immediate visibility of updated app_state.market across lifecycle shifts
- Invalidation via _reset_market_state
- Graceful fallback when market is None
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import services.realtime.utils as rt_utils
from app_state import app_state
from tests.test_support.state_reset import reset_app_state_internals


@pytest.fixture(autouse=True)
def _cleanup_state():
    yield
    reset_app_state_internals()


def test_scraper_market_state_dynamically_tracks_app_state():
    """Verify _scraper_market_state reflects changes in app_state.market rather than being locked."""
    original_market = getattr(app_state, "market", None)

    mock_market_1 = MagicMock()
    mock_market_1.is_scraper_blocked.return_value = False
    app_state.market = mock_market_1

    assert rt_utils._scraper_market_state() is mock_market_1
    assert not rt_utils._is_scraper_blocked()

    # Rebind to a different market instance
    mock_market_2 = MagicMock()
    mock_market_2.is_scraper_blocked.return_value = True
    app_state.market = mock_market_2

    # Must immediately return mock_market_2, not mock_market_1
    assert rt_utils._scraper_market_state() is mock_market_2
    assert rt_utils._is_scraper_blocked() is True

    # Restore
    app_state.market = original_market


def test_scraper_market_state_reset():
    """Verify reset_app_state_internals clears _scraper_market_ref."""
    rt_utils._scraper_market_ref = MagicMock()
    reset_app_state_internals()
    assert rt_utils._scraper_market_ref is None


def test_scraper_market_state_none_fallback():
    """Verify _is_scraper_blocked returns False safely when market is None."""
    original_market = getattr(app_state, "market", None)
    app_state.market = None
    rt_utils._scraper_market_ref = None

    try:
        assert rt_utils._scraper_market_state() is None
        assert rt_utils._is_scraper_blocked() is False
    finally:
        app_state.market = original_market
