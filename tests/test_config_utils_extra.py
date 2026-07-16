"""Coverage-focused tests for config_utils.py helper functions."""
import config_utils
import config_store


def test_build_mistral_legacy_aliases_returns_dict():
    result = config_utils._build_mistral_legacy_aliases()
    assert isinstance(result, dict)


def test_get_or_create_master_key_returns_usable_key():
    key = config_store.get_or_create_master_key()
    assert isinstance(key, str)
    assert len(key) >= 16
