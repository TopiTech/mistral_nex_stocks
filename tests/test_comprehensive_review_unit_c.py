"""Regression tests for Unit C code review fixes ([B-1 route], [C-1])."""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from unittest.mock import patch

from app import app
from credential_manager import PREVIEW_API_KEY
from route_helpers import _is_polling_token_inflight
from routes import api_analysis
from utils.caching import _get_cached_value


def test_is_polling_token_inflight_detects_finished_jobs_without_session_cookie():
    token = "tok_no_cookie_123456"

    # Populate finished analyze_result_cache and chat_result_cache under another scope.
    with api_analysis.analyze_fetch_lock:
        api_analysis.analyze_result_cache[f"analyze:convA:{token}"] = (
            time.time(),
            {"summary": "done"},
            None,
        )
    try:
        with app.test_request_context("/api/analyze-v2", method="POST"):
            assert _is_polling_token_inflight(token) is False
    finally:
        with api_analysis.analyze_fetch_lock:
            api_analysis.analyze_result_cache.pop(f"analyze:convA:{token}", None)

    # Active inflight job without session cookie must still return True.
    tok_active = "tok_active_123456"
    with api_analysis.chat_fetch_lock:
        api_analysis.chat_fetch_inflight[f"chat:convB:{tok_active}"] = {"done": threading.Event()}
    try:
        with app.test_request_context("/api/chat", method="POST"):
            assert _is_polling_token_inflight(tok_active) is True
    finally:
        with api_analysis.chat_fetch_lock:
            api_analysis.chat_fetch_inflight.pop(f"chat:convB:{tok_active}", None)

    # Finished news_result_cache entry returns False without session cookie.
    tok_news = "tok_news_done_123456"
    with api_analysis.news_fetch_lock:
        api_analysis.news_result_cache[f"news_ddgs_only_force:{tok_news}"] = (
            time.time(),
            {"us": {"content": "ok"}},
            None,
        )
    try:
        with app.test_request_context("/api/news", method="POST"):
            assert _is_polling_token_inflight(tok_news) is False
    finally:
        with api_analysis.news_fetch_lock:
            api_analysis.news_result_cache.pop(f"news_ddgs_only_force:{tok_news}", None)


def test_api_news_force_refresh_poll_uses_news_result_cache_without_duplicate_llm_call(
    client, monkeypatch
):
    bg_tasks: list[threading.Thread] = []
    call_count = {"n": 0}
    gate = threading.Event()

    def slow_get_news(**kwargs):
        call_count["n"] += 1
        gate.wait(timeout=2.0)
        return {
            "us": {"content": "Fresh US headline after forced refresh.", "status": "success"},
            "jp": {"content": "Fresh JP headline.", "status": "success"},
            "trends": {"content": "Fresh trends.", "status": "success"},
        }

    def run_in_thread(executor, fn):
        fut: Future = Future()

        def _runner():
            try:
                with app.app_context():
                    res = fn()
                fut.set_result(res)
            except BaseException as exc:
                fut.set_exception(exc)

        t = threading.Thread(target=_runner, daemon=True)
        bg_tasks.append(t)
        t.start()
        return fut

    monkeypatch.setattr(api_analysis, "NEWS_PREPARE_WAIT_SEC", 0.05)

    with (
        patch.object(api_analysis, "require_trusted_or_admin", return_value=(True, None)),
        patch.object(api_analysis, "extract_api_key", return_value="real-mistral-key"),
        patch.object(api_analysis, "extract_langsearch_api_key", return_value=""),
        patch.object(api_analysis, "extract_tavily_api_key", return_value=""),
        patch.object(api_analysis, "_submit_in_app_context", side_effect=run_in_thread),
        patch.object(
            api_analysis.news_service, "get_synchronized_market_news", side_effect=slow_get_news
        ),
    ):
        token = "news-force-poll-token-0001"
        r1 = client.post(
            "/api/news?force=true",
            json={"force": True, "request_token": token},
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        assert r1.status_code == 200
        assert r1.get_json()["fetching"] is True

        # Unblock background job and let it complete into news_result_cache.
        gate.set()
        for t in bg_tasks:
            t.join(timeout=3.0)

        # Poll #2 arrives carrying force=true + same request_token after job finished:
        # must return cached result cleanly without running a second forced LLM job.
        r2 = client.post(
            "/api/news?force=true",
            json={"force": True, "request_token": token},
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        assert r2.status_code == 200
        j2 = r2.get_json()
        assert j2["us"]["content"] == "Fresh US headline after forced refresh."
        assert "disclaimer" in j2
        assert call_count["n"] == 1


def test_api_news_preview_and_provider_isolation_prevent_cross_cache_poisoning(client):
    calls: list[str] = []

    def fake_get_news(**kwargs):
        api_key = kwargs["api_key"]
        calls.append(api_key)
        if api_key == PREVIEW_API_KEY:
            return {
                "us": {"content": "Preview mode dummy summary.", "status": "success"},
                "jp": {"content": "Preview JP.", "status": "success"},
                "trends": {"content": "Preview T.", "status": "success"},
            }
        return {
            "us": {"content": "Real provider news summary.", "status": "success"},
            "jp": {"content": "Real JP.", "status": "success"},
            "trends": {"content": "Real T.", "status": "success"},
        }

    def run_inline(_executor, fn):
        fut: Future = Future()
        fut.set_result(fn())
        return fut

    with (
        patch.object(api_analysis, "require_trusted_or_admin", return_value=(True, None)),
        patch.object(api_analysis, "extract_langsearch_api_key", return_value=""),
        patch.object(api_analysis, "extract_tavily_api_key", return_value=""),
        patch.object(api_analysis, "_submit_in_app_context", side_effect=run_inline),
        patch.object(
            api_analysis.news_service, "get_synchronized_market_news", side_effect=fake_get_news
        ),
    ):
        with patch.object(api_analysis, "extract_api_key", return_value=PREVIEW_API_KEY):
            r_prev = client.post("/api/news", json={}, environ_base={"REMOTE_ADDR": "127.0.0.1"})
        assert r_prev.status_code == 200
        assert r_prev.get_json()["us"]["content"] == "Preview mode dummy summary."
        assert _get_cached_value("news_bundle_latest_ddgs_only", duration=86400) is None

        with patch.object(api_analysis, "extract_api_key", return_value="real-mistral-key"):
            r_real = client.post("/api/news", json={}, environ_base={"REMOTE_ADDR": "127.0.0.1"})
        assert r_real.status_code == 200
        assert r_real.get_json()["us"]["content"] == "Real provider news summary."
        assert calls == [PREVIEW_API_KEY, "real-mistral-key"]

        # Switching to OpenAI provider must not serve the cached Mistral bundle.
        with (
            patch.object(api_analysis, "extract_api_key", return_value="sk-openai-real"),
            patch("credential_manager.is_openai_provider", return_value=True),
            patch("credential_manager.get_openai_model", return_value="gpt-4o-mini"),
        ):
            r_oa = client.post("/api/news", json={}, environ_base={"REMOTE_ADDR": "127.0.0.1"})
        assert r_oa.status_code == 200
        assert calls == [PREVIEW_API_KEY, "real-mistral-key", "sk-openai-real"]
