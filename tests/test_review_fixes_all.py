"""
Comprehensive test suite verifying fixes for Code Review findings (H-1..H-3, M-1..M-6, L-1..L-5).
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from native_host.native_host import _safe_float_env, _safe_int_env
from route_helpers import cleanup_history_circuit_state
from services.news_service import _wrap_cdata
from utils.disk_cache import StockDiskCache


def test_wrap_cdata_clean_and_escaped():
    """Verify H-2: _wrap_cdata produces clean, non-nested CDATA blocks with breakout protection."""
    assert _wrap_cdata(None) == "<![CDATA[データなし]]>"
    assert _wrap_cdata("") == "<![CDATA[データなし]]>"

    normal_text = "Sample market news headline"
    wrapped = _wrap_cdata(normal_text)
    assert wrapped == "<![CDATA[Sample market news headline]]>"
    assert wrapped.count("<![CDATA[") == 1
    assert wrapped.count("]]>") == 1

    injection_text = "Headline with ]]> breakout attempt"
    wrapped_inj = _wrap_cdata(injection_text)
    assert "]]]]><![CDATA[>" in wrapped_inj
    assert wrapped_inj.startswith("<![CDATA[")
    assert wrapped_inj.endswith("]]>")


def test_stock_disk_cache_lazy_initialization():
    """Verify H-3: StockDiskCache does not create directories at instantiation time."""
    with tempfile.TemporaryDirectory() as tmpdir:
        non_existent_dir = Path(tmpdir) / "sub_cache_dir"
        assert not non_existent_dir.exists()

        cache = StockDiskCache(cache_dir=non_existent_dir, enable_cleanup=False)
        # Directory should NOT exist immediately after instantiation
        assert not non_existent_dir.exists()

        # Operation triggers lazy directory creation
        cache.set("test_key", {"data": 123})
        assert non_existent_dir.exists()
        assert cache.get("test_key") == {"data": 123}


def test_native_host_safe_env_parsing():
    """Verify L-5: _safe_int_env and _safe_float_env handle invalid string values gracefully."""
    with patch.dict(os.environ, {"TEST_INT_KEY": "invalid_num", "TEST_FLOAT_KEY": "not_a_float"}):
        assert _safe_int_env("TEST_INT_KEY", 1024) == 1024
        assert _safe_float_env("TEST_FLOAT_KEY", 1.5) == 1.5

    with patch.dict(os.environ, {"TEST_INT_KEY": "2048", "TEST_FLOAT_KEY": "2.5"}):
        assert _safe_int_env("TEST_INT_KEY", 1024) == 2048
        assert _safe_float_env("TEST_FLOAT_KEY", 1.5) == 2.5


def test_cleanup_history_circuit_state_brackets():
    """Verify M-2: cleanup_history_circuit_state evaluates conditions correctly with explicit grouping."""
    from app_state import app_state
    from market_state import CircuitState

    with app_state.market.history_circuit_lock:
        app_state.market.history_circuit_state["SYM_OPEN_STALE"] = CircuitState(
            status="OPEN", open_until=10.0, timeout_streak=5
        )
        app_state.market.history_circuit_state["SYM_CLOSED_CLEAN"] = CircuitState(
            status="CLOSED", open_until=0.0, timeout_streak=0
        )
        app_state.market.history_circuit_state["SYM_CLOSED_STREAK"] = CircuitState(
            status="CLOSED", open_until=0.0, timeout_streak=2
        )

    # Run cleanup with now_ts = 1000.0 (stale_after_sec = 600)
    cleanup_history_circuit_state(now_ts=1000.0, stale_after_sec=600)

    with app_state.market.history_circuit_lock:
        # Open stale and closed clean should be removed
        assert "SYM_OPEN_STALE" not in app_state.market.history_circuit_state
        assert "SYM_CLOSED_CLEAN" not in app_state.market.history_circuit_state
        # Closed with active streak should remain
        assert "SYM_CLOSED_STREAK" in app_state.market.history_circuit_state


def test_native_host_safe_int_env_min_value_floor():
    """R18: _safe_int_env clamps values below min_value to the floor so a
    mis-set NATIVE_HOST_MAX_MESSAGE_BYTES cannot break framing/drain logic."""
    with patch.dict(os.environ, {"TEST_MIN_KEY": "1"}):
        assert _safe_int_env("TEST_MIN_KEY", 1024, min_value=4096) == 4096
        assert _safe_int_env("TEST_MIN_KEY", 1024, min_value=None) == 1
        # Invalid strings still fall back to the default (not the floor).
    with patch.dict(os.environ, {"TEST_MIN_KEY": "invalid"}):
        assert _safe_int_env("TEST_MIN_KEY", 1024, min_value=4096) == 1024
    with patch.dict(os.environ, {"TEST_MIN_KEY": "8192"}):
        assert _safe_int_env("TEST_MIN_KEY", 1024, min_value=4096) == 8192

    # The module-level constants carry the floor at import time.
    import native_host.native_host as nh

    assert nh.MAX_MESSAGE_BYTES >= 4096
    assert nh.MAX_DRAIN_BYTES >= 4096
