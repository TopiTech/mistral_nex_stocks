"""Freshness metadata must be finite and shared by get()/has().

A NaN ``stored_at`` used to make ``get()`` treat the entry as fresh forever
(``age > ttl`` is False for NaN) while ``has()`` reported it missing
(``age <= ttl`` is also False for NaN). Inf, -Inf and far-future timestamps
had the same class of failure. Both reads now use one finite-age rule that
falls back to the file mtime for unusable metadata.
"""

from __future__ import annotations

import json
import math
import os
import time

import pytest

from utils.disk_cache import StockDiskCache


@pytest.fixture
def cache(tmp_path):
    return StockDiskCache(
        cache_dir=tmp_path / "cache",
        max_entries=10,
        default_ttl=60,
        enable_cleanup=False,
    )


def _write_entry(cache: StockDiskCache, key: str, stored_at: float) -> None:
    cache._ensure_cache_dir()
    path = cache._entry_path(key)
    path.write_text(
        json.dumps({"value": "sentinel", "stored_at": stored_at}),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "stored_at",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "posinf", "neginf"],
)
def test_non_finite_stored_at_falls_back_to_mtime(cache: StockDiskCache, stored_at: float) -> None:
    _write_entry(cache, "entry", stored_at)

    assert cache.get("entry", ttl=60) == "sentinel"
    # The previous implementation disagreed with get() for NaN: get() kept
    # serving the entry while has() said it was absent.
    assert cache.has("entry", ttl=60) is True


def test_future_stored_at_does_not_make_an_old_entry_immortal(cache: StockDiskCache) -> None:
    _write_entry(cache, "future", time.time() + 365 * 24 * 3600)
    path = cache._entry_path("future")
    old = time.time() - 120.0
    os.utime(path, (old, old))

    assert cache.get("future", ttl=60) is None
    assert cache.has("future", ttl=60) is False


def test_finite_stored_at_remains_authoritative(cache: StockDiskCache) -> None:
    _write_entry(cache, "fresh", time.time())
    path = cache._entry_path("fresh")
    old = time.time() - 3600.0
    os.utime(path, (old, old))

    assert cache.get("fresh", ttl=60) == "sentinel"
    assert cache.has("fresh", ttl=60) is True
    assert math.isfinite(time.time())
