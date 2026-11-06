"""Tests for audit review goal hardening v10.

Covers:
- AI portfolio atomic storage write hardening (permissions, fsync, Windows replace retry)
- AI portfolio strict load sharing-violation retry resilience
- _json_safe handling of datetimes, timestamps, pandas NaT/NA, and fallback serialization
- AI portfolio rebalance result caching and poll error cleanup
- Shutdown manager atomic write Windows sharing-violation retry
"""

from __future__ import annotations

import datetime
import json
import os
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

import config_store
from routes.stocks.common import _json_safe
from services.ai_portfolio_service import (
    PortfolioStorageError,
    _load_saved_ai_portfolios_strict,
    _write_saved_ai_portfolios,
    load_saved_ai_portfolios,
)
from shutdown_manager import _write_atomic_restricted

# =====================================================================
# Unit 1: _json_safe Comprehensive Serialization Tests
# =====================================================================


def test_json_safe_primitives_and_containers():
    data = {
        "str": "hello",
        "int": 42,
        "float": 3.14,
        "nan": float("nan"),
        "inf": float("inf"),
        "neg_inf": float("-inf"),
        "bool": True,
        "none": None,
        "list": [1, float("nan"), "world"],
        "nested_dict": {"a": float("inf"), "b": 100},
        "tuple": (1, 2, float("nan")),
        "set": {3, 1, 2},
    }
    safe = _json_safe(data)
    assert safe["str"] == "hello"
    assert safe["int"] == 42
    assert safe["float"] == 3.14
    assert safe["nan"] is None
    assert safe["inf"] is None
    assert safe["neg_inf"] is None
    assert safe["bool"] is True
    assert safe["none"] is None
    assert safe["list"] == [1, None, "world"]
    assert safe["nested_dict"] == {"a": None, "b": 100}
    assert safe["tuple"] == [1, 2, None]
    assert safe["set"] == [1, 2, 3]

    # Verify standard json.dumps with allow_nan=False succeeds
    dumped = json.dumps(safe, allow_nan=False)
    assert '"nan": null' in dumped


def test_json_safe_temporal_types():
    dt = datetime.datetime(2026, 11, 7, 8, 30, 45, tzinfo=datetime.UTC)
    d = datetime.date(2026, 11, 7)
    t = datetime.time(8, 30, 45)

    assert _json_safe(dt) == dt.isoformat()
    assert _json_safe(d) == d.isoformat()
    assert _json_safe(t) == t.isoformat()

    # In nested payload
    payload = {"timestamp": dt, "date": d, "time": t}
    safe = _json_safe(payload)
    dumped = json.dumps(safe, allow_nan=False)
    assert dt.isoformat() in dumped


def test_json_safe_pandas_and_numpy_types():
    pd = pytest.importorskip("pandas")
    np = pytest.importorskip("numpy")

    ts = pd.Timestamp("2026-11-07 08:30:45")
    nat = pd.NaT
    nan_val = np.nan
    np_int = np.int64(99)
    np_float = np.float64(12.34)
    np_bool = np.bool_(True)
    np_array = np.array([1.0, np.nan, 3.0])

    assert _json_safe(ts) == ts.isoformat()
    assert _json_safe(nat) is None
    assert _json_safe(nan_val) is None
    assert _json_safe(np_int) == 99
    assert _json_safe(np_float) == 12.34
    assert _json_safe(np_bool) is True
    assert _json_safe(np_array) == [1.0, None, 3.0]

    safe = _json_safe({"ts": ts, "nat": nat, "arr": np_array})
    dumped = json.dumps(safe, allow_nan=False)
    assert '"nat": null' in dumped


def test_json_safe_custom_objects_and_fallback():
    class CustomObject:
        def __str__(self) -> str:
            return "custom-object-value"

    obj = CustomObject()
    u = uuid.uuid4()
    p = Path("/tmp/test_path")

    safe = _json_safe({"obj": obj, "uuid": u, "path": p})
    assert safe["obj"] == "custom-object-value"
    assert safe["uuid"] == str(u)
    assert safe["path"] == str(p)

    dumped = json.dumps(safe, allow_nan=False)
    assert "custom-object-value" in dumped


# =====================================================================
# Unit 2: AI Portfolio Storage Atomic Write & Load Hardening
# =====================================================================


def test_write_and_load_saved_ai_portfolios_roundtrip(tmp_path: Path):
    test_file = tmp_path / "ai_portfolios.json"
    with patch("services.ai_portfolio_service.AI_PORTFOLIO_STORAGE_FILE", test_file):
        portfolios = [
            {
                "id": "custom-1",
                "theme": "Growth",
                "title": "Growth Portfolio",
                "items": [
                    {"symbol": "AAPL", "market": "us", "weight_pct": 50.0},
                    {"symbol": "7203.T", "market": "jp", "weight_pct": 50.0},
                ],
            }
        ]
        master_key = config_store.get_or_create_master_key()
        _write_saved_ai_portfolios(portfolios, master_key=master_key)

        assert test_file.exists()
        loaded = _load_saved_ai_portfolios_strict(master_key=master_key)
        assert len(loaded) == 1
        assert loaded[0]["id"] == "custom-1"
        assert len(loaded[0]["items"]) == 2


def test_write_saved_ai_portfolios_replace_retry_on_windows(tmp_path: Path):
    test_file = tmp_path / "ai_portfolios.json"
    real_replace = os.replace
    calls = []

    def mock_replace(src, dst):
        if "ai_portfolios" in str(src):
            calls.append(src)
            if len(calls) < 3:
                raise PermissionError("Simulated Windows sharing violation")
        return real_replace(src, dst)

    with patch("services.ai_portfolio_service.AI_PORTFOLIO_STORAGE_FILE", test_file):
        with patch("services.ai_portfolio_service._is_windows", return_value=True):
            with patch("os.replace", side_effect=mock_replace):
                portfolios = [{"id": "test-pf", "title": "Test"}]
                _write_saved_ai_portfolios(portfolios)

                assert len(calls) == 3
                assert test_file.exists()


def test_write_saved_ai_portfolios_unlink_failure_in_finally_is_swallowed(tmp_path: Path):
    test_file = tmp_path / "ai_portfolios.json"
    with patch("services.ai_portfolio_service.AI_PORTFOLIO_STORAGE_FILE", test_file):
        with patch.object(Path, "unlink", side_effect=OSError("Access is denied on temp file")):
            portfolios = [{"id": "test-pf", "title": "Test"}]
            # Should not raise OSError from finally block
            _write_saved_ai_portfolios(portfolios)
            assert test_file.exists()


def test_load_saved_ai_portfolios_strict_permission_error_retry(tmp_path: Path):
    test_file = tmp_path / "ai_portfolios.json"
    with patch("services.ai_portfolio_service.AI_PORTFOLIO_STORAGE_FILE", test_file):
        portfolios = [{"id": "retry-pf", "title": "Retry Test"}]
        _write_saved_ai_portfolios(portfolios)

        real_open = open
        open_attempts = []

        def mock_open(path, *args, **kwargs):
            if str(path) == str(test_file):
                open_attempts.append(len(open_attempts) + 1)
                if len(open_attempts) < 3:
                    raise PermissionError("[WinError 32] The process cannot access the file")
            return real_open(path, *args, **kwargs)

        with patch("services.ai_portfolio_service._is_windows", return_value=True):
            with patch("builtins.open", side_effect=mock_open):
                loaded = _load_saved_ai_portfolios_strict()
                assert len(open_attempts) == 3
                assert len(loaded) == 1
                assert loaded[0]["id"] == "retry-pf"


def test_load_saved_ai_portfolios_graceful_on_storage_error(tmp_path: Path):
    test_file = tmp_path / "ai_portfolios.json"
    with patch("services.ai_portfolio_service.AI_PORTFOLIO_STORAGE_FILE", test_file):
        # Corrupt file
        test_file.write_text("corrupted non-json content", encoding="utf-8")
        with pytest.raises(PortfolioStorageError):
            _load_saved_ai_portfolios_strict()

        # load_saved_ai_portfolios should catch PortfolioStorageError and return []
        assert load_saved_ai_portfolios() == []


# =====================================================================
# Unit 3: Shutdown Manager Windows Replace Retry
# =====================================================================


def test_shutdown_manager_write_atomic_restricted_windows_retry(tmp_path: Path):
    target = tmp_path / "test_token"
    real_replace = os.replace
    replace_calls = []

    def mock_replace(src, dst):
        replace_calls.append(src)
        if len(replace_calls) < 3:
            raise PermissionError("[WinError 32] Sharing violation")
        return real_replace(src, dst)

    with patch("platform.system", return_value="Windows"):
        with patch("os.replace", side_effect=mock_replace):
            _write_atomic_restricted(target, "test-content-token")
            assert len(replace_calls) == 3
            assert target.exists()
            assert target.read_text(encoding="utf-8") == "test-content-token"


# =====================================================================
# Unit 4: AI Portfolio Rebalance Route Cache Consistency
# =====================================================================


def test_rebalance_ai_portfolio_cache_preserved_on_success(client):
    from routes.stocks.ai_portfolio import (
        ai_portfolio_fetch_lock,
        ai_portfolio_result_cache,
    )

    # Prepopulate cache as if rebalance just finished
    scope = "test-scope-token-12345678"
    theme = "tech"
    inflight_key = f"rebalance:{scope}:{theme}"
    mock_result = {
        "id": "tech",
        "title": "Tech Portfolio",
        "items": [{"symbol": "NVDA", "market": "us", "weight_pct": 100.0}],
    }
    with ai_portfolio_fetch_lock:
        ai_portfolio_result_cache[inflight_key] = (time.time(), mock_result, None)

    resp = client.post(
        "/api/ai-portfolio/rebalance",
        json={"theme": "tech", "conversation_scope": scope},
        headers={"Origin": "http://localhost:5000"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["portfolio"]["id"] == "tech"

    # Verify cached rebalance is retrieved and then popped on read so subsequent rebalances are fresh
    with ai_portfolio_fetch_lock:
        assert inflight_key not in ai_portfolio_result_cache


def test_rebalance_ai_portfolio_cache_cleared_on_error(client):
    from routes.stocks.ai_portfolio import (
        ai_portfolio_fetch_lock,
        ai_portfolio_result_cache,
    )

    scope = "test-scope-err-12345678"
    theme = "tech"
    inflight_key = f"rebalance:{scope}:{theme}"
    err = RuntimeError("Simulated rebalance AI model error")
    with ai_portfolio_fetch_lock:
        ai_portfolio_result_cache[inflight_key] = (time.time(), None, err)

    resp = client.post(
        "/api/ai-portfolio/rebalance",
        json={"theme": "tech", "conversation_scope": scope},
        headers={"Origin": "http://localhost:5000"},
    )
    assert resp.status_code == 500
    data = resp.get_json()
    assert data["ok"] is False

    # Verify inflight_key is popped from cache on error!
    with ai_portfolio_fetch_lock:
        assert inflight_key not in ai_portfolio_result_cache


def test_write_saved_ai_portfolios_posix_umask_and_directory_fsync(tmp_path: Path):
    test_file = tmp_path / "ai_portfolios.json"
    fsync_called = []

    def mock_fsync(fd):
        fsync_called.append(fd)

    with patch("services.ai_portfolio_service.AI_PORTFOLIO_STORAGE_FILE", test_file):
        with patch("services.ai_portfolio_service._is_windows", return_value=False):
            with patch("os.fsync", side_effect=mock_fsync):
                with patch("os.chmod") as mock_chmod:
                    portfolios = [{"id": "posix-pf", "title": "POSIX Test"}]
                    _write_saved_ai_portfolios(portfolios)

                    assert test_file.exists()
                    assert len(fsync_called) >= 1
                    assert mock_chmod.call_count >= 1

