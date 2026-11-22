"""Tests for final comprehensive code review unit fixes (2026-11-22).

Covers:
- Unit 1: config/io.py flag reset, utils/disk_cache.py configurable max_ttl,
  utils/market_utils.py timezone conversions with accurate US DST fallback.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from services.stock.provider import YFinanceProvider
from utils.disk_cache import StockDiskCache
from utils.market_utils import (
    _is_us_dst,
    _to_jst_datetime,
    _to_ny_datetime,
    is_jp_market_holiday,
    is_us_market_holiday,
)


def test_config_store_transient_read_flag_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify config/io.py properly clears _CONFIG_TRANSIENT_READ_FAILED on valid load."""
    import config.io as cio
    import config_store as cs

    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"test_key": "valid_value"}), encoding="utf-8")

    monkeypatch.setattr(cs, "CONFIG_FILE", config_file)
    cs._CONFIG_CACHE["key"] = None
    cs._CONFIG_CACHE["data"] = None
    cs._CONFIG_TRANSIENT_READ_FAILED = True

    loaded = cio.load_config()
    assert loaded.get("test_key") == "valid_value"
    assert cs._CONFIG_TRANSIENT_READ_FAILED is False


def test_stock_disk_cache_custom_max_ttl_cleanup(tmp_path: Path) -> None:
    """Verify StockDiskCache respects custom max_ttl during background stale cleanup."""
    # Custom max_ttl = 172800 (2 days)
    cache = StockDiskCache(tmp_path, max_entries=10, default_ttl=3600, max_ttl=172800)
    assert cache._max_ttl == 172800

    now = time.time()
    # Entry 1: aged 90,000s (> 86400s default, but < 172800s max_ttl) -> must NOT be removed
    file_retained = tmp_path / "retained.json"
    retained_payload = {"stored_at": now - 90000, "data": {"val": 1}}
    file_retained.write_text(json.dumps(retained_payload), encoding="utf-8")

    # Entry 2: aged 200,000s (> 172800s max_ttl) -> must be removed
    file_stale = tmp_path / "stale.json"
    stale_payload = {"stored_at": now - 200000, "data": {"val": 2}}
    file_stale.write_text(json.dumps(stale_payload), encoding="utf-8")

    removed = cache._remove_stale_entries()
    assert removed == 1
    assert file_retained.exists()
    assert not file_stale.exists()


def test_market_utils_dst_fallback_and_conversions() -> None:
    """Verify _is_us_dst, _to_ny_datetime and _to_jst_datetime fallbacks."""
    # US DST in 2026:
    # Starts: second Sunday in March (2026-03-08) at 02:00 local (07:00 UTC)
    # Ends: first Sunday in November (2026-11-01) at 02:00 local (06:00 UTC)

    # 1. Before DST starts: 2026-03-08 06:59:59 UTC -> EST (False)
    dt_winter_pre = datetime(2026, 3, 8, 6, 59, 59, tzinfo=UTC)
    assert _is_us_dst(dt_winter_pre) is False

    # 2. Right after DST starts: 2026-03-08 07:00:00 UTC -> EDT (True)
    dt_summer_start = datetime(2026, 3, 8, 7, 0, 0, tzinfo=UTC)
    assert _is_us_dst(dt_summer_start) is True

    # 3. Summer peak: 2026-07-15 15:00:00 UTC -> EDT (True)
    dt_summer_mid = datetime(2026, 7, 15, 15, 0, 0, tzinfo=UTC)
    assert _is_us_dst(dt_summer_mid) is True

    # 4. Right after DST ends: 2026-11-01 06:00:00 UTC -> EST (False)
    dt_winter_post = datetime(2026, 11, 1, 6, 0, 0, tzinfo=UTC)
    assert _is_us_dst(dt_winter_post) is False

    # Simulate ZoneInfo raising an exception to verify fallback logic
    with patch("utils.market_utils.ZoneInfo", side_effect=Exception("Simulated ZoneInfo failure")):
        # Summer fallback: UTC-4
        ny_summer = _to_ny_datetime(dt_summer_mid)
        assert ny_summer.hour == 11  # 15 - 4 = 11
        assert ny_summer.tzinfo is None

        # Winter fallback: UTC-5
        ny_winter = _to_ny_datetime(dt_winter_post)
        assert ny_winter.hour == 1  # 6 - 5 = 1
        assert ny_winter.tzinfo is None

        # JST fallback: UTC+9
        jst_time = _to_jst_datetime(datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC))
        assert jst_time.hour == 9
        assert jst_time.tzinfo is None

        # Holiday check functions work cleanly under fallback
        assert is_us_market_holiday(dt_winter_pre) is False
        assert is_jp_market_holiday(datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)) is True


def test_stock_provider_news_summary_length_relaxed() -> None:
    """Verify YFinanceProvider.get_news retains long summaries up to 2000 chars."""
    provider = YFinanceProvider()
    fake_summary = "A" * 500  # Previously truncated at 200
    mock_ticker = MagicMock()
    mock_ticker.get_news.return_value = [
        {
            "content": {
                "title": "Test Title",
                "summary": fake_summary,
                "pubDate": "2026-11-22T00:00:00Z",
                "provider": {"displayName": "TestNews"},
            }
        }
    ]

    with patch.object(provider, "get_ticker", return_value=mock_ticker):
        results = provider.get_news("AAPL")
        assert len(results) == 1
        assert len(results[0]["summary"]) == 500
        assert results[0]["summary"] == fake_summary

