"""Regression tests for review findings addressed in the current revision.

Covers:
- R (High)  config_store.get_or_create_master_key must NOT overwrite a
            present-but-undecodable master key (would orphan all encrypted data).
- R (Medium) news_service must NOT ask the LLM to fabricate a summary (and cache
            it as "success") when every external provider returned empty/failed.
- R (Low)    gunicorn.conf.py on_starting must respect MNS_WORKER_VALIDATION=0
            so its guard matches wsgi.py.
"""

import importlib.util
import os
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Master key preservation (High)
# ---------------------------------------------------------------------------
def test_get_or_create_master_key_fails_closed_on_decode_failure():
    """A present mns_master_key entry that cannot be decoded must raise and must
    never be replaced with a freshly generated key (which would permanently
    orphan all Fernet-encrypted data)."""
    import config_store

    with (
        patch.dict("os.environ", {"MNS_PROD": "0"}, clear=False),
        patch("utils.env_helpers._is_production_env", return_value=False),
        patch("crypto_utils.KEYRING_AVAILABLE", True),
        patch("crypto_utils._is_windows", return_value=True),
        patch(
            "config_store.load_config",
            return_value={"mns_master_key": {"scheme": "keyring", "value": ""}},
        ),
        patch("config_store.save_config") as save_mock,
        patch("config_store._decode_secret", return_value=""),
    ):
        if "MNS_MASTER_KEY" in os.environ:
            del os.environ["MNS_MASTER_KEY"]

        with pytest.raises(RuntimeError):
            config_store.get_or_create_master_key()

        save_mock.assert_not_called()


def test_get_or_create_master_key_reuses_decodable_entry():
    """A decodable existing entry is returned without regeneration or save."""
    import config_store

    with (
        patch.dict("os.environ", {"MNS_PROD": "0"}, clear=False),
        patch("utils.env_helpers._is_production_env", return_value=False),
        patch(
            "config_store.load_config",
            return_value={"mns_master_key": {"scheme": "keyring", "value": ""}},
        ),
        patch("config_store.save_config") as save_mock,
        patch("config_store._decode_secret", return_value="existing-key-12345"),
    ):
        if "MNS_MASTER_KEY" in os.environ:
            del os.environ["MNS_MASTER_KEY"]

        key = config_store.get_or_create_master_key()
        assert key == "existing-key-12345"
        save_mock.assert_not_called()


# ---------------------------------------------------------------------------
# News: no fabricated summary / caching when context is empty (Medium)
# ---------------------------------------------------------------------------
def test_news_empty_context_skips_llm_and_cache():
    from services.news_service import NewsService

    svc = NewsService()
    with (
        patch("services.news_service._determine_search_strategy", return_value="tavily"),
        patch("services.news_service.collect_market_news_context", return_value=""),
        patch("services.news_service.collect_market_trending_titles", return_value=[]),
        patch("services.news_service.call_mistral_chat") as llm_mock,
        patch("services.news_service.get_cached") as cache_mock,
    ):
        result = svc.get_synchronized_market_news(
            api_key="k", langsearch_api_key="", tavily_api_key="tv"
        )

    llm_mock.assert_not_called()
    cache_mock.assert_not_called()
    assert result["us"]["content"] == ""
    assert result["trends"]["content"] == ""
    assert result["us"]["status"] == "empty"


def test_news_with_context_uses_cache_path():
    """When news context is available the normal cached-generation path runs."""
    from services.news_service import NewsService

    svc = NewsService()
    with (
        patch("services.news_service._determine_search_strategy", return_value="tavily"),
        patch("services.news_service.collect_market_news_context", return_value="real us news"),
        patch("services.news_service.collect_market_trending_titles", return_value=["Trend A"]),
        patch(
            "services.news_service.get_cached",
            return_value={
                "us": "半導体セクターが市場全体を牽引",
                "jp": "日経平均は堅調に推移",
                "trends": "テクノロジー株へ資金流入",
            },
        ) as cache_mock,
    ):
        result = svc.get_synchronized_market_news(
            api_key="k", langsearch_api_key="", tavily_api_key="tv"
        )

    cache_mock.assert_called_once()
    assert result["us"]["content"] != ""


# ---------------------------------------------------------------------------
# Gunicorn single-worker guard respects MNS_WORKER_VALIDATION (Low)
# ---------------------------------------------------------------------------
def _load_gunicorn_conf():
    path = Path(__file__).resolve().parent.parent / "gunicorn.conf.py"
    spec = importlib.util.spec_from_file_location("gunicorn_conf_module", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakeServer:
    def __init__(self, workers):
        self.num_workers = workers


def test_gunicorn_on_starting_hard_fails_by_default():
    module = _load_gunicorn_conf()
    with patch.dict("os.environ", {}, clear=False):
        os.environ.pop("MNS_WORKER_VALIDATION", None)
        with patch("sys.exit") as sys_exit_mock:
            module.on_starting(_FakeServer(workers=4))
    assert sys_exit_mock.called


def test_gunicorn_on_starting_respects_validation_disable():
    module = _load_gunicorn_conf()
    with patch.dict("os.environ", {"MNS_WORKER_VALIDATION": "0"}, clear=False):
        with patch("sys.exit") as sys_exit_mock:
            module.on_starting(_FakeServer(workers=4))
    assert not sys_exit_mock.called
