# tests/test_review_r11_fix.py
"""Regression tests for R11 (SVC-4): shallow-copy sharing in fetch_history_sync_impl.

The fix replaces ``dict(result)`` with ``copy.deepcopy(result)`` when storing
the successful payload into ``yfinance_short_cache``, so that the mutable
``history`` list is NOT shared between the cache entry and the caller's return
value.  These tests verify that invariant.
"""

import copy
from unittest.mock import patch

import pandas as pd
import pytest

from app_state import app_state
from services import stock_service


def _make_dummy_hist(rows: int = 3) -> pd.DataFrame:
    """Build a minimal DataFrame that looks like a yfinance history result."""
    from pandas import Timestamp

    data = []
    index = []
    base = Timestamp("2026-01-05", tz="UTC")
    for i in range(rows):
        index.append(base + pd.DateOffset(days=i))
        data.append(
            {
                "Open": 150.0 + i,
                "High": 155.0 + i,
                "Low": 149.0 + i,
                "Close": 154.0 + i,
                "Volume": 100000 + i * 1000,
            }
        )
    return pd.DataFrame(data, index=index)


@pytest.fixture(autouse=True)
def _clear_short_cache():
    """Ensure a clean yfinance_short_cache before each test."""
    with app_state.yfinance_short_cache_lock:
        app_state.yfinance_short_cache.clear()
    yield
    with app_state.yfinance_short_cache_lock:
        app_state.yfinance_short_cache.clear()


def test_r11_cache_history_is_independent_from_return_value():
    """R11: the ``history`` list stored in the cache must be a **different object**
    from the one returned to the caller.

    After ``fetch_history_sync_impl`` returns, modifying the caller's list
    must NOT mutate the cached entry.
    """
    dummy_hist = _make_dummy_hist(rows=3)

    with (
        patch.object(stock_service, "_history_with_timeout", return_value=dummy_hist),
        patch.object(stock_service, "safe_get_ticker", return_value=True),
    ):
        result = stock_service.fetch_history_sync_impl("R11TEST", "us", "1d")

    # Sanity: result must be a success payload
    assert "error" not in result, f"unexpected error: {result}"
    assert "history" in result
    assert len(result["history"]) == 3

    # Compute the cache key the same way the implementation does.
    # ``fetch_history_sync_impl(symbol, market, "1d")`` with the default
    # ``interval="auto"`` resolves ``requested_interval="5m"`` (see
    # ``fetch_history_sync_impl``), so the payload cache key uses ``5m``.
    from utils.caching import history_short_payload_cache_key

    cache_key = history_short_payload_cache_key("R11TEST", "1d", "5m")

    with app_state.yfinance_short_cache_lock:
        cached = app_state.yfinance_short_cache.get(cache_key)

    assert cached is not None, "cache entry must exist after fetch"
    assert "history" in cached

    # --- Verify object identity ---
    # The cached list MUST be a different object from the returned list
    assert cached["history"] is not result["history"], (
        "cached history list must NOT be the same object as the returned list"
    )

    # --- Verify deep equality ---
    assert cached["history"] == result["history"], (
        "cached history must be equal in value to the returned history"
    )


def test_r11_caller_mutation_does_not_pollute_cache():
    """R11: destructive caller mutation of the returned ``history`` list must
    NOT pollute the cache entry.

    This simulates a hypothetical caller that clears, appends, or sorts the
    returned list in place.
    """
    dummy_hist = _make_dummy_hist(rows=5)

    with (
        patch.object(stock_service, "_history_with_timeout", return_value=dummy_hist),
        patch.object(stock_service, "safe_get_ticker", return_value=True),
    ):
        result = stock_service.fetch_history_sync_impl("R11TEST_MUT", "us", "1d")

    from utils.caching import history_short_payload_cache_key

    cache_key = history_short_payload_cache_key("R11TEST_MUT", "1d", "5m")

    # Snapshot the original cached history
    with app_state.yfinance_short_cache_lock:
        cached_before = copy.deepcopy(app_state.yfinance_short_cache.get(cache_key))
    assert cached_before is not None

    # --- Mutate the returned list ---
    returned_history = result["history"]

    # 1. Append a new element
    returned_history.append({"x": 999, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 100})
    assert len(returned_history) == 6

    # 2. Modify an existing element in place
    returned_history[0]["c"] = 9999.99

    # 3. Clear and re-populate (simulating extreme caller behaviour)
    returned_history.clear()
    returned_history.append({"x": 0, "o": 0, "h": 0, "l": 0, "c": 0, "v": 0})

    # --- Verify cache is NOT affected ---
    with app_state.yfinance_short_cache_lock:
        cached_after = app_state.yfinance_short_cache.get(cache_key)

    assert cached_after is not None
    assert cached_after["history"] == cached_before["history"], (
        "cache history must remain unchanged after caller mutates the returned list"
    )
    assert len(cached_after["history"]) == 5, (
        "cache history must still have the original 5 elements"
    )
    # The first element's 'c' value must still be the original value
    assert cached_after["history"][0]["c"] != 9999.99, (
        "cache history first element must not reflect caller's mutation"
    )


def test_r11_cache_deep_copy_ensures_full_isolation():
    """R11: verify that the entire cached dict is deeply independent, not just
    the top-level ``history`` key.  All nested dicts inside the history list
    must also be independent copies.
    """
    dummy_hist = _make_dummy_hist(rows=2)

    with (
        patch.object(stock_service, "_history_with_timeout", return_value=dummy_hist),
        patch.object(stock_service, "safe_get_ticker", return_value=True),
    ):
        result = stock_service.fetch_history_sync_impl("R11TEST_DEEP", "us", "1d")

    from utils.caching import history_short_payload_cache_key

    cache_key = history_short_payload_cache_key("R11TEST_DEEP", "1d", "5m")

    with app_state.yfinance_short_cache_lock:
        cached = app_state.yfinance_short_cache.get(cache_key)

    assert cached is not None

    # Each inner dict in the history list must be a different object
    for i in range(len(result["history"])):
        assert cached["history"][i] is not result["history"][i], (
            f"inner dict at index {i} must NOT be the same object"
        )

    # Mutating an inner dict of the returned list must not affect cache
    result["history"][0]["o"] = 999.0
    with app_state.yfinance_short_cache_lock:
        cached_after = app_state.yfinance_short_cache.get(cache_key)

    assert cached_after["history"][0]["o"] != 999.0, (
        "cache inner dict must not reflect caller's mutation of inner dict"
    )


def test_r11_cache_hit_returns_an_isolated_payload():
    """A payload returned from the short-cache hit path must also be isolated."""
    dummy_hist = _make_dummy_hist(rows=3)

    with (
        patch.object(stock_service, "_history_with_timeout", return_value=dummy_hist),
        patch.object(stock_service, "safe_get_ticker", return_value=True),
    ):
        first_result = stock_service.fetch_history_sync_impl("R11TEST_HIT", "us", "1d")
        second_result = stock_service.fetch_history_sync_impl("R11TEST_HIT", "us", "1d")

    assert second_result["history"] == first_result["history"]
    assert second_result["history"] is not first_result["history"]
    second_result["history"][0]["c"] = 9999.99

    from utils.caching import history_short_payload_cache_key

    cache_key = history_short_payload_cache_key("R11TEST_HIT", "1d", "5m")
    with app_state.yfinance_short_cache_lock:
        cached = app_state.yfinance_short_cache.get(cache_key)

    assert cached is not None
    assert cached["history"][0]["c"] != 9999.99
