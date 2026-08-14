# tests/test_audit_comprehensive_fixes_2026.py
"""Regression test suite for comprehensive codebase review fixes."""

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app_bg import _update_indices_data
from app_state import app_state
from config_store import load_config
from native_host.native_host import _get_ancestor_process_names


class TestWin32ProcessAncestryFailClosed:
    """Verify that _get_ancestor_process_names fails closed on unverified creation times."""

    def test_ancestor_breaks_when_curr_creation_time_none(self, monkeypatch):
        if os.name != "nt":
            pytest.skip("Windows-specific test")

        import native_host.native_host as nh

        monkeypatch.setattr(nh, "_get_proc_creation_time", lambda pid: None)
        ancestors = _get_ancestor_process_names(max_depth=5)
        assert ancestors == []

    def test_ancestor_breaks_when_ppid_creation_time_none(self, monkeypatch):
        if os.name != "nt":
            pytest.skip("Windows-specific test")

        import native_host.native_host as nh

        def fake_creation_time(pid):
            if pid == os.getpid():
                return 1000
            return None

        monkeypatch.setattr(nh, "_get_proc_creation_time", fake_creation_time)
        ancestors = _get_ancestor_process_names(max_depth=5)
        assert ancestors == []

    def test_ancestor_breaks_when_ppid_is_newer_than_child(self, monkeypatch):
        if os.name != "nt":
            pytest.skip("Windows-specific test")

        import native_host.native_host as nh

        def fake_creation_time(pid):
            if pid == os.getpid():
                return 1000
            return 2000  # Parent created AFTER child (PID recycled)

        monkeypatch.setattr(nh, "_get_proc_creation_time", fake_creation_time)
        ancestors = _get_ancestor_process_names(max_depth=5)
        assert ancestors == []


class TestConfigLoadRetryResilience:
    """Verify load_config retry logic withstands transient read errors without corruption reset."""

    def test_load_config_retries_on_transient_error(self, monkeypatch, tmp_path):
        import config_store as cs

        cfg_file = tmp_path / "config.json"
        valid_data = {"test_key": "valid_value"}
        cfg_file.write_text(json.dumps(valid_data), encoding="utf-8")

        monkeypatch.setattr(cs, "CONFIG_FILE", cfg_file)
        monkeypatch.setattr(cs, "APP_DATA_DIR", tmp_path)
        cs._CONFIG_CACHE["data"] = None
        cs._CONFIG_CACHE["key"] = None

        attempts = 0

        # Simulate 1 transient OSError before succeeding on attempt 2
        orig_open = open

        def flaky_open(file, *args, **kwargs):
            nonlocal attempts
            if str(file) == str(cfg_file) and "r" in args:
                attempts += 1
                if attempts == 1:
                    raise OSError("Sharing violation (simulated)")
            return orig_open(file, *args, **kwargs)

        with patch("builtins.open", side_effect=flaky_open):
            loaded = load_config()

        assert attempts >= 2
        assert loaded.get("test_key") == "valid_value"
        assert not cs._CONFIG_CORRUPTED


class TestUsdJpyPersistenceThrottling:
    """Verify that _update_indices_data throttles disk writes of user_stocks."""

    def test_usdjpy_persistence_throttled_when_rate_unchanged(self, monkeypatch):
        save_calls = 0

        def count_save():
            nonlocal save_calls
            save_calls += 1

        import app_bg as bg_mod

        monkeypatch.setattr(bg_mod, "save_user_stocks", count_save)
        app_state.market.last_usdjpy_rate = 155.0
        app_state.market.last_usdjpy_persisted_ts = time.time()

        # Update with identical rate (155.0) right away
        idx_res = [{"symbol": "USDJPY=X", "price": "155.00", "change": 0.0}]
        _update_indices_data(idx_res, [], [])

        # Should NOT call save_user_stocks because rate didn't change and timestamp is fresh
        assert save_calls == 0

    def test_usdjpy_persisted_when_rate_changes(self, monkeypatch):
        save_calls = 0

        def count_save():
            nonlocal save_calls
            save_calls += 1

        import app_bg as bg_mod

        monkeypatch.setattr(bg_mod, "save_user_stocks", count_save)
        app_state.market.last_usdjpy_rate = 150.0
        app_state.market.last_usdjpy_persisted_ts = time.time()

        # Update with rate that changed significantly (155.50 vs 150.0)
        idx_res = [{"symbol": "USDJPY=X", "price": "155.50", "change": 5.5}]
        _update_indices_data(idx_res, [], [])

        assert save_calls == 1
        assert app_state.market.last_usdjpy_rate == 155.50


class TestExtensionAllowedRoutes:
    """Verify chrome extension route configuration."""

    def test_allowed_routes_contains_orbit(self):
        bg_js_path = Path(__file__).resolve().parents[1] / "chrome_extension" / "background.js"
        content = bg_js_path.read_text(encoding="utf-8")
        assert '"/experimental/orbit"' in content or "'/experimental/orbit'" in content

    def test_manifest_least_privilege(self):
        manifest_path = Path(__file__).resolve().parents[1] / "chrome_extension" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert "content_scripts" not in manifest
        assert "activeTab" in manifest["permissions"]
        assert "scripting" in manifest["permissions"]
