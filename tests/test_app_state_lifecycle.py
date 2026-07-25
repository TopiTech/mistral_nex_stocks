"""Lifecycle tests for process-isolated yfinance resources."""

import tempfile
from pathlib import Path

import pytest
from app_state import app_state


@pytest.mark.skip(reason="Disabled for CI troubleshooting")
def test_cleanup_yfinance_cache_removes_only_registered_directory():
    with tempfile.TemporaryDirectory() as parent:
        cache_dir = Path(parent) / "py-yfinance-mns-test"
        cache_dir.mkdir()
        (cache_dir / "cache.db").write_text("temporary", encoding="utf-8")
        original = app_state._yfinance_cache_dir
        try:
            app_state._yfinance_cache_dir = str(cache_dir)
            app_state._cleanup_yfinance_cache()
            assert not cache_dir.exists()
            assert app_state._yfinance_cache_dir is None
        finally:
            app_state._yfinance_cache_dir = original
