"""Tests for remediation Group A: BE-001, BE-002, and BE-003.

- BE-001: /api/search error caching elimination and race condition prevention.
- BE-002: Centralized DEFAULT_USDJPY_RATE fallback constant across modules.
- BE-003: Safety guard against concurrent user modification during rollback of auto-remove invalid symbols.
"""

from __future__ import annotations

import copy
import inspect
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app import app
from app_state import app_state
from bg.sync_worker import _BATCH_INVALID_MARKER, _auto_remove_invalid_symbols
from constants import CACHE_DURATION_SEARCH, DEFAULT_USDJPY_RATE
from error_codes import ErrorCode
from market_state import MarketDataState
from tests import reset_app_state_internals
from utils.caching import global_cache, sanitize_cache_key
from utils.stock_payload import get_current_usdjpy_rate


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client


# ===========================================================================
# BE-001: /api/search error cache elimination & no error cache entry
# ===========================================================================


def test_be001_search_exception_never_cached(client):
    """BE-001: When stock_provider.search raises, error is not cached, and 502 is returned."""
    query = "errortest"
    cache_key = sanitize_cache_key(f"search_{query.lower()}")

    with global_cache.cache_lock:
        cache = global_cache.caches.get(CACHE_DURATION_SEARCH)
        if cache is not None:
            cache.pop(cache_key, None)

    with patch.object(
        app_state.stock_provider,
        "search",
        side_effect=ValueError("Temporary search provider failure"),
    ):
        resp = client.get(
            f"/api/search?q={query}",
            headers={"Origin": "http://localhost:5000"},
        )
        assert resp.status_code == 502
        payload = resp.get_json()
        assert payload.get("ok") is False
        assert payload.get("error_code") == int(ErrorCode.API_SERVICE_ERROR)

    # Verify that the cache bucket does NOT contain an entry for this key
    with global_cache.cache_lock:
        cache = global_cache.caches.get(CACHE_DURATION_SEARCH)
        assert cache is None or cache_key not in cache

    # Subsequent request with healthy provider succeeds immediately without stale error
    healthy_results = [{"symbol": "ERR", "name": "Error-Free Corp"}]
    with patch.object(
        app_state.stock_provider,
        "search",
        return_value=healthy_results,
    ):
        resp_healthy = client.get(
            f"/api/search?q={query}",
            headers={"Origin": "http://localhost:5000"},
        )
        assert resp_healthy.status_code == 200
        data_healthy = resp_healthy.get_json()
        assert data_healthy.get("results") == healthy_results


def test_be001_search_valid_func_rejects_error_dict():
    """BE-001: valid_func in quotes.py accepts only valid result dictionaries with a results list."""
    from routes.stocks import quotes

    source = inspect.getsource(quotes.api_search)
    assert 'bool(\n            isinstance(payload, dict) and isinstance(payload.get("results"), list)\n        )' in source or 'bool(isinstance(payload, dict) and isinstance(payload.get("results"), list))' in source or 'isinstance(payload, dict) and isinstance(payload.get("results"), list)' in source
    # pop after error in result must not exist
    assert "cache.pop" not in source


# ===========================================================================
# BE-002: USD/JPY exchange rate fallback unification
# ===========================================================================


def test_be002_default_usdjpy_rate_constants():
    """BE-002: DEFAULT_USDJPY_RATE is centrally defined in constants and equals 150.0 by default."""
    assert isinstance(DEFAULT_USDJPY_RATE, float)
    assert DEFAULT_USDJPY_RATE == 150.0


def test_be002_market_state_uses_default_usdjpy_rate():
    """BE-002: MarketDataState initializes last_usdjpy_rate with DEFAULT_USDJPY_RATE."""
    state = MarketDataState()
    assert state.last_usdjpy_rate == DEFAULT_USDJPY_RATE


def test_be002_stock_payload_default_arg():
    """BE-002: get_current_usdjpy_rate default parameter is DEFAULT_USDJPY_RATE."""
    sig = inspect.signature(get_current_usdjpy_rate)
    assert sig.parameters["default_rate"].default == DEFAULT_USDJPY_RATE


def test_be002_storage_load_user_stocks_fallback():
    """BE-002: storage.load_user_stocks falls back to DEFAULT_USDJPY_RATE when missing or invalid."""
    from utils.storage import load_user_stocks

    reset_app_state_internals()
    try:
        with patch("utils.storage._locked_read_user_stocks", return_value={"us": {}, "jp": {}}):
            load_user_stocks(force=True)
            assert app_state.market.last_usdjpy_rate == DEFAULT_USDJPY_RATE

        with patch(
            "utils.storage._locked_read_user_stocks",
            return_value={"us": {}, "jp": {}, "last_usdjpy_rate": "invalid"},
        ):
            load_user_stocks(force=True)
            assert app_state.market.last_usdjpy_rate == DEFAULT_USDJPY_RATE
    finally:
        reset_app_state_internals()


def test_be002_storage_save_user_stocks_fallback():
    """BE-002: storage.save_user_stocks falls back to DEFAULT_USDJPY_RATE if last_usdjpy_rate is invalid."""
    from utils.storage import save_user_stocks

    reset_app_state_internals()
    try:
        app_state.market.last_usdjpy_rate = -10.0  # Invalid non-positive rate
        captured_data: dict[str, object] = {}

        def mock_protect_data(encoded: str, **kwargs: object) -> dict[str, str]:
            nonlocal captured_data
            captured_data = json.loads(encoded)
            return {"data": "mock_encrypted"}

        def mock_write(encoded_data: str, tmp_file: object, target_file: Path, lock_file: object) -> None:
            target_file.write_text("{}")

        with patch("utils.storage.protect_data", side_effect=mock_protect_data), \
             patch("utils.storage._write_user_stocks_with_lock", side_effect=mock_write):
            save_user_stocks()

        assert captured_data.get("last_usdjpy_rate") == DEFAULT_USDJPY_RATE
    finally:
        reset_app_state_internals()


# ===========================================================================
# BE-003: Auto-remove invalid symbols concurrent rollback safety
# ===========================================================================


def test_be003_rollback_preserves_concurrent_user_addition():
    """BE-003: If user re-adds/updates symbol while lock is released during slow save, rollback does not overwrite."""
    reset_app_state_internals()
    try:
        symbol = "CONCUR_SYM"
        market = "us"
        original_stock = {"name": "OldStaleName", "shares": 5, "avg_price": 100.0}
        app_state.market.user_us[symbol] = copy.deepcopy(original_stock)

        threshold = app_state.market.INVALID_SYMBOL_REMOVAL_THRESHOLD
        app_state.market.invalid_symbol_streak[symbol] = threshold - 1

        user_new_stock = {"name": "NewUserStock", "shares": 50, "avg_price": 250.0}

        def fake_save_user_stocks_with_concurrent_user_mutation():
            # Simulates user re-adding/updating the symbol concurrently while save is running outside locks
            with app_state.market.user_stocks_lock:
                app_state.market.user_us[symbol] = copy.deepcopy(user_new_stock)
            # Raise an error to trigger rollback in _auto_remove_invalid_symbols
            raise OSError("Disk write failed during persistence")

        with patch("app_bg.save_user_stocks", side_effect=fake_save_user_stocks_with_concurrent_user_mutation):
            _auto_remove_invalid_symbols(
                [(symbol, "OldStaleName", market)],
                [(_BATCH_INVALID_MARKER, symbol)],
            )

        # Safety guard must prevent overwriting user's concurrent mutation with original_stock
        assert app_state.market.user_us[symbol] == user_new_stock
        # Invalid streak should NOT be restored since the user re-added/modified the symbol
        assert app_state.market.invalid_symbol_streak.get(symbol) is None
    finally:
        reset_app_state_internals()


def test_be003_rollback_restores_when_no_concurrent_modification():
    """BE-003: Normal rollback succeeds when user has not concurrently modified the symbol."""
    reset_app_state_internals()
    try:
        symbol = "CLEAN_SYM"
        market = "us"
        original_stock = {"name": "CleanOldName", "shares": 2, "avg_price": 50.0}
        app_state.market.user_us[symbol] = copy.deepcopy(original_stock)

        threshold = app_state.market.INVALID_SYMBOL_REMOVAL_THRESHOLD
        app_state.market.invalid_symbol_streak[symbol] = threshold - 1

        with patch("app_bg.save_user_stocks", side_effect=OSError("Disk failure")):
            _auto_remove_invalid_symbols(
                [(symbol, "CleanOldName", market)],
                [(_BATCH_INVALID_MARKER, symbol)],
            )

        # Symbol must be restored to original_stock because it was not in container
        assert app_state.market.user_us[symbol] == original_stock
        assert app_state.market.invalid_symbol_streak[symbol] == threshold
    finally:
        reset_app_state_internals()
