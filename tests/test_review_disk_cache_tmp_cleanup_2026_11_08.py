"""Regression tests for StockDiskCache temporary-file cleanup on failure.

``StockDiskCache.set`` writes through a UUID-named ``*.tmp`` file and promotes
it with ``os.replace``. The error handler only classified ``OSError`` and
``TypeError``; a payload that makes ``json.dump`` raise something else (for
example ``ValueError`` for a circular reference) escaped and left the partially
written tmp file behind until the periodic stale-file sweep.
"""

from pathlib import Path

import pytest

from utils.disk_cache import StockDiskCache


def test_circular_payload_raises_and_removes_tmp_file(tmp_path: Path) -> None:
    cache = StockDiskCache(tmp_path, enable_cleanup=False)
    circular: dict[str, object] = {}
    circular["self"] = circular

    with pytest.raises(ValueError):
        cache.set("circular", circular)

    assert list(tmp_path.glob("*.tmp")) == []


def test_unserializable_payload_is_skipped_without_leaving_tmp_file(tmp_path: Path) -> None:
    """``TypeError`` is the documented degraded-write path: skip, no raise."""
    cache = StockDiskCache(tmp_path, enable_cleanup=False)

    cache.set("object", {"value": object()})

    assert cache.get("object") is None
    assert list(tmp_path.glob("*.tmp")) == []


def test_successful_set_keeps_value_without_leaving_tmp_file(tmp_path: Path) -> None:
    cache = StockDiskCache(tmp_path, enable_cleanup=False)

    cache.set("ok", {"value": 1})

    assert cache.get("ok") == {"value": 1}
    assert list(tmp_path.glob("*.tmp")) == []
