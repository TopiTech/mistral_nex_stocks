"""Regression tests for the 2026-10 root-cause review fixes.

Covers:
- ``ScreenerQueryRequest`` rejects a boolean ``limit`` as a normal validation
  error (HTTP 400), never a raw ``TypeError`` that escapes the route handler.
- ``StockDiskCache.has()`` and ``get()`` agree on freshness when the file mtime
  was refreshed but the embedded ``stored_at`` is old.
- ``MarketDataState.mark_scraper_blocked`` never shortens an active cooldown
  when a later report carries a smaller backoff.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from market_state import MarketDataState
from schemas.stocks import ScreenerQueryRequest
from utils.disk_cache import StockDiskCache


def test_screener_boolean_limit_is_a_validation_error() -> None:
    """A boolean is not an integer limit; it must not raise raw TypeError."""
    with pytest.raises(ValidationError):
        ScreenerQueryRequest(limit=True)  # type: ignore[arg-type]


def test_screener_boolean_limit_returns_400(client) -> None:
    """The HTTP boundary maps the validation failure to 400, not 500."""
    response = client.get("/api/screener?limit=true")
    assert response.status_code == 400


def test_disk_cache_has_agrees_with_get_after_mtime_refresh(tmp_path: Path) -> None:
    """``has()`` must use ``stored_at`` just like ``get()``.

    Simulates restoring/copying a stale entry: the JSON still carries the old
    ``stored_at`` while the mtime is fresh.  ``get()`` treats it as expired, so
    ``has()`` must too.
    """
    cache = StockDiskCache(cache_dir=tmp_path, enable_cleanup=False, default_ttl=60)
    cache.set("restored", {"value": 1})
    path = cache._entry_path("restored")

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["stored_at"] = time.time() - 3600
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert cache.get("restored") is None
    assert cache.has("restored") is False


def test_disk_cache_has_still_accepts_legacy_entries(tmp_path: Path) -> None:
    """Entries without ``stored_at`` keep using mtime for freshness."""
    cache = StockDiskCache(cache_dir=tmp_path, enable_cleanup=False, default_ttl=60)
    path = cache._entry_path("legacy")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"value": "legacy"}), encoding="utf-8")

    assert cache.has("legacy") is True
    assert cache.get("legacy") == "legacy"


def test_scraper_block_window_is_monotonic() -> None:
    """A shorter later backoff must not shorten an active cooldown."""
    market = MarketDataState()
    try:
        market.scraper_block_until = 0.0
        market.scraper_block_streak = 0

        market.mark_scraper_blocked(retry_after=300)
        original_until = market.scraper_block_until
        assert original_until > time.time() + 200

        # Next report has no Retry-After and a graduated backoff (120s), which
        # would land earlier than the existing 300s window.
        market.mark_scraper_blocked()
        assert market.scraper_block_until >= original_until
    finally:
        market.scraper_block_until = 0.0
        market.scraper_block_streak = 0
