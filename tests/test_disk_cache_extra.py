"""Coverage tests for utils/disk_cache.py — minimal, no tempfile or shutil.

Only tests internal methods that don't require disk I/O, or that can use
simple os.path operations without tempfile.
"""

from pathlib import Path


def test_entry_path_special_chars():
    """Special characters in key are replaced with underscores."""
    from utils.disk_cache import StockDiskCache

    # Use a non-existent path — only testing _entry_path, not I/O
    cache = StockDiskCache(cache_dir=Path("/_nonexistent_mns_dir_"), enable_cleanup=False)
    path = cache._entry_path("hist_^N225_3mo")
    assert "hist__N225_3mo" in str(path)
    assert str(path).endswith(".json")


def test_entry_path_long_key():
    """Keys longer than 200 chars are truncated."""
    from utils.disk_cache import StockDiskCache

    cache = StockDiskCache(cache_dir=Path("/_nonexistent_mns_dir2_"), enable_cleanup=False)
    long_key = "a" * 100 + "!" * 150
    path = cache._entry_path(long_key)
    assert len(path.name) <= 205


def test_stats_nonexistent_dir():
    """stats() returns zero entries for a non-existent directory."""
    from utils.disk_cache import StockDiskCache

    cache = StockDiskCache(cache_dir=Path("/_nonexistent_mns_stats_"), enable_cleanup=False)
    stats = cache.stats()
    assert stats["disk_cache_entries"] == 0


def test_cleanup_nonexistent_dir():
    """cleanup() on non-existent directory raises no error."""
    from utils.disk_cache import StockDiskCache

    cache = StockDiskCache(cache_dir=Path("/_nonexistent_mns_clean_"), enable_cleanup=False)
    removed = cache.cleanup()
    assert removed == 0


def test_delete_prefix_nonexistent():
    """delete_prefix on empty directory returns 0."""
    from utils.disk_cache import StockDiskCache

    cache = StockDiskCache(cache_dir=Path("/_nonexistent_mns_dp_"), enable_cleanup=False)
    removed = cache.delete_prefix("no_match")
    assert removed == 0


def test_delete_nonexistent_key():
    """delete on non-existent key returns False."""
    from utils.disk_cache import StockDiskCache

    cache = StockDiskCache(cache_dir=Path("/_nonexistent_mns_del_"), enable_cleanup=False)
    assert not cache.delete("no_such_key")
