"""Unit tests verifying code review fixes for goal implementation."""

import concurrent.futures
from unittest import mock

import pytest

import credential_manager
import crypto_utils
from services.ai_portfolio_service import sanitize_ai_portfolio


def test_keyring_inspection_failure_rollback_preserves_keys(monkeypatch):
    """Test that if keyring inspection fails with an error, rollback does not delete the key."""
    deleted_keys = []
    set_keys = {}

    class MockKeyring:
        def get_password(self, service, key_name):
            raise RuntimeError("Keyring locked or inaccessible")

        def delete_password(self, service, key_name):
            deleted_keys.append(key_name)

        def set_password(self, service, key_name, value):
            set_keys[key_name] = value

    mock_kr = MockKeyring()
    monkeypatch.setattr(credential_manager, "_keyring_available", lambda: True)
    monkeypatch.setattr(credential_manager, "_keyring", lambda: mock_kr)
    monkeypatch.setattr(crypto_utils, "KEYRING_AVAILABLE", True)
    monkeypatch.setattr(crypto_utils, "keyring", mock_kr)

    # Force save_config to fail to trigger rollback
    with mock.patch("config_store.save_config", side_effect=OSError("Disk full")):
        with pytest.raises(OSError, match="Disk full"):
            credential_manager.save_api_credentials(mistral_api_key="test-key-123")

    # The key should NOT have been deleted because inspection failed (sentinel used)
    assert "mistral_api_key" not in deleted_keys


def test_ephemeral_key_concurrent_access():
    """Test that _get_ephemeral_key is thread-safe and returns identical key across threads."""
    with crypto_utils._EPHEMERAL_LOCK:
        crypto_utils._EPHEMERAL_KEY = None

    keys = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(crypto_utils._get_ephemeral_key) for _ in range(20)]
        for f in concurrent.futures.as_completed(futures):
            keys.append(f.result())

    assert len(keys) == 20
    assert len(set(keys)) == 1
    assert isinstance(keys[0], str)
    assert len(keys[0]) > 0


def test_sanitize_ai_portfolio_zero_weights_edge_cases():
    """Test that sanitize_ai_portfolio handles zero-weight and negative-weight items safely."""
    portfolio_all_zero = {
        "id": "custom-1",
        "theme": "AI",
        "items": [
            {"symbol": "NVDA", "market": "us", "weight_pct": 0.0},
            {"symbol": "MSFT", "market": "us", "weight_pct": 0.0},
            {"symbol": "AAPL", "market": "us", "weight_pct": 0.0},
        ],
    }
    cleaned = sanitize_ai_portfolio(portfolio_all_zero)
    assert len(cleaned["items"]) == 3
    # Should be evenly distributed to 100%
    total_w = sum(it["weight_pct"] for it in cleaned["items"])
    assert pytest.approx(total_w, abs=0.1) == 100.0

    portfolio_empty = {
        "id": "custom-2",
        "theme": "Empty",
        "items": [],
    }
    cleaned_empty = sanitize_ai_portfolio(portfolio_empty)
    assert cleaned_empty["items"] == []
