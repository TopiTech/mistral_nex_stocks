"""U2 regression: sys.modules test-seam removal (2026-10-23).

Guards that non-test source no longer reaches across modules via
``sys.modules.get(...)`` lookups, and that the replacement lazy-import
seams preserve override behavior (facade/app_bg attribute patches still
honored) including fallback paths.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from bg import common as bg_common
from bg import leader_election, sync_worker
from services.realtime import scrapers as rt_scrapers
from services.realtime import tv_client as rt_tv_client
from services.realtime import utils as rt_utils

REPO_ROOT = Path(__file__).resolve().parent.parent
_SEAM_TARGETS = (
    'sys.modules.get("app_bg")',
    "sys.modules.get('app_bg')",
    'sys.modules.get("services.realtime_engine")',
    "sys.modules.get('services.realtime_engine')",
    'sys.modules["app_bg"]',
    'sys.modules["services.realtime_engine"]',
)
_SEAM_SCAN_DIRS = ("app_factory", "bg", "routes", "services")


def _iter_source_files():
    for dirname in _SEAM_SCAN_DIRS:
        yield from (REPO_ROOT / dirname).rglob("*.py")
    for name in ("app_bg.py", "route_helpers.py", "app.py"):
        candidate = REPO_ROOT / name
        if candidate.exists():
            yield candidate


def test_no_sys_modules_seam_in_source():
    offenders = []
    for path in _iter_source_files():
        text = path.read_text(encoding="utf-8")
        for marker in _SEAM_TARGETS:
            if marker in text:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {marker}")
    assert offenders == [], f"sys.modules seam still present: {offenders}"


def test_bg_common_get_attr_fallback():
    assert bg_common._get_app_bg_attr("u2_missing_attr_xyz", "fb") == "fb"


def test_bg_common_is_market_open_honors_app_bg_override():
    with patch("app_bg.is_market_open", return_value=True) as mock_fn:
        assert bg_common.is_market_open("jp") is True
        mock_fn.assert_called_once_with("jp")


def test_sync_worker_start_time_roundtrip_with_app_bg_mirror():
    with patch("app_bg._sync_start_time", 123.0):
        assert sync_worker._get_sync_start_time() == 123.0
    sync_worker._set_sync_start_time(456.5)
    try:
        import app_bg

        assert app_bg.__dict__.get("_sync_start_time") == 456.5
        assert sync_worker._get_sync_start_time() == 456.5
    finally:
        sync_worker._set_sync_start_time(0.0)


def test_sync_worker_generation_and_lock_override():
    sentinel = object()
    sync_worker._set_sync_generation(7)
    try:
        import app_bg

        assert app_bg.__dict__.get("_sync_generation") == 7
    finally:
        sync_worker._set_sync_generation(0)
    with patch("app_bg._sync_execution_lock", sentinel):
        assert sync_worker._get_sync_execution_lock() is sentinel


def test_leader_election_mirrors_app_bg_state():
    sentinel = object()
    leader_election._set_is_sync_leader(False)
    try:
        import app_bg

        assert app_bg.__dict__.get("_is_sync_leader") is False
    finally:
        leader_election._set_is_sync_leader(True)
    leader_election._set_leader_lock_file(sentinel)
    try:
        assert leader_election._get_leader_lock_file() is sentinel
    finally:
        leader_election._set_leader_lock_file(None)
    with patch("app_bg._LEADER_LOCK_FILE", sentinel):
        assert leader_election._get_leader_lock_file() is sentinel


def test_facade_attr_fallback_for_missing_name():
    assert rt_utils._get_facade_attr("u2_missing_attr_xyz", "fb") == "fb"


def test_realtime_loggers_honor_facade_override():
    mock_logger = MagicMock()
    with patch("services.realtime_engine.logger", mock_logger):
        assert rt_scrapers._get_logger() is mock_logger
        assert rt_tv_client._get_logger() is mock_logger


def test_is_pts_session_honors_facade_override_and_falls_back():
    jst = ZoneInfo("Asia/Tokyo")
    sunday = datetime(2026, 8, 2, 18, 0, tzinfo=jst)
    assert rt_utils.is_pts_session(sunday) is False
    with patch(
        "services.realtime_engine.is_pts_session", return_value=True
    ) as mock_fn:
        assert rt_utils.is_pts_session(sunday) is True
        mock_fn.assert_called_once_with(sunday)


def test_scraper_block_and_yf_limited_honor_facade_override():
    with patch(
        "services.realtime_engine._is_scraper_blocked", return_value=True
    ):
        assert rt_utils._is_scraper_blocked() is True
    with patch(
        "services.realtime_engine._is_yf_rate_limited", return_value=True
    ):
        assert rt_utils._is_yf_rate_limited() is True


def test_rt_time_and_sleep_use_stdlib():
    import time as std_time

    with patch(
        "services.realtime_engine.time.time", return_value=1700000000.0
    ):
        # Facade time *is* the stdlib time module, so facade patches apply.
        assert rt_utils._rt_time() == 1700000000.0
    assert abs(rt_utils._rt_time() - std_time.time()) < 5.0
    rt_utils._rt_sleep(0)


def test_tv_client_websocket_attr_resolves():
    sentinel = object()
    resolved = rt_tv_client._get_rt_attr("websocket", sentinel)
    assert resolved is not sentinel
    assert rt_tv_client._get_rt_attr("u2_missing_attr_xyz", sentinel) is sentinel
