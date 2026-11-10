"""Regression tests for the 2026-11-11 services/routes review fixes."""

from __future__ import annotations

import time
from unittest.mock import patch

from app_state import app_state
from market_state import CircuitState
from services.ai_portfolio_service import sanitize_ai_portfolio
from services.openai_service import get_openai_embeddings_batch
from services.realtime_engine import RealtimeMarketEngine
from session_manager import yf_session_manager


def test_sanitize_ai_portfolio_keeps_zero_target_price_as_none() -> None:
    cleaned = sanitize_ai_portfolio(
        {
            "id": "zero-target",
            "items": [
                {
                    "symbol": "AAPL",
                    "market": "us",
                    "weight_pct": 100.0,
                    "target_price": 0.0,
                    "rationale": "unknown target",
                }
            ],
        }
    )

    assert len(cleaned["items"]) == 1
    assert cleaned["items"][0]["target_price"] is None


def test_clear_rate_limit_respects_expected_until() -> None:
    key = "review-expected-until"
    try:
        yf_session_manager.mark_rate_limited(key, duration=30)
        observed = yf_session_manager.get_rate_limit_until(key)
        assert observed > time.time()

        # A newer window must survive a success from an older request.
        yf_session_manager.mark_rate_limited(key, duration=60)
        newer = yf_session_manager.get_rate_limit_until(key)
        assert newer > observed
        assert (
            yf_session_manager.clear_rate_limit(key, expected_until=observed)
            is False
        )
        assert yf_session_manager.get_rate_limit_until(key) == newer

        # The owner of the current window can clear it.
        assert yf_session_manager.clear_rate_limit(key, expected_until=newer) is True
        assert yf_session_manager.is_rate_limited(key) is False
    finally:
        yf_session_manager.clear_rate_limit(key)


def test_mistral_slot_rollback_does_not_erase_newer_reservation() -> None:
    from services import ai_service

    with app_state.ai.mistral_cooldown_lock:
        app_state.ai.mistral_next_allowed_ts = 0.0
        app_state.ai.mistral_last_call_ts = 100.0

    # This caller reserved 99.0; another thread has since reserved 100.0.
    ai_service._release_mistral_call_slot(99.0)
    assert app_state.ai.mistral_last_call_ts == 100.0


def test_realtime_purge_ttl_exceeds_max_snapshot_interval(monkeypatch) -> None:
    monkeypatch.setattr(
        "services.realtime.engine.SSE_MODE2_FULL_SNAPSHOT_INTERVAL_SEC", 120
    )
    engine = RealtimeMarketEngine()
    cid = engine.register_client()
    try:
        # Simulate a client whose last full snapshot was 121s ago. With the
        # supported 120s snapshot interval this client is still active; a fixed
        # 120s purge TTL would remove it.
        engine._client_last_seen[cid] = time.time() - 121.0
        engine._purge_stale_clients()
        assert cid in engine._client_last_seen
        assert cid in engine._client_states
    finally:
        engine.unregister_client(cid)


def test_heatmap_stale_fetch_does_not_clear_new_owner_marker() -> None:
    from routes.stocks.common import _fetch_heatmap_cached

    cache_key = "heatmap_review_owner"
    app_state.heatmap_fetch_inflight.add(cache_key)
    app_state.heatmap_fetch_start_times[cache_key] = 2.0
    try:
        with patch(
            "routes.stocks.common._caching_mod.get_cached",
            return_value={"stocks": [{"symbol": "AAPL"}]},
        ):
            _fetch_heatmap_cached(cache_key, "us", ["AAPL"], fetch_token=1.0)

        assert cache_key in app_state.heatmap_fetch_inflight
        assert app_state.heatmap_fetch_start_times[cache_key] == 2.0

        with patch(
            "routes.stocks.common._caching_mod.get_cached",
            return_value={"stocks": [{"symbol": "AAPL"}]},
        ):
            _fetch_heatmap_cached(cache_key, "us", ["AAPL"], fetch_token=2.0)

        assert cache_key not in app_state.heatmap_fetch_inflight
        assert cache_key not in app_state.heatmap_fetch_start_times
    finally:
        app_state.heatmap_fetch_inflight.discard(cache_key)
        app_state.heatmap_fetch_start_times.pop(cache_key, None)


def test_openai_embeddings_bool_index_falls_back_to_position() -> None:
    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": [{"index": True, "embedding": [1.0]}]}

    class FakeClient:
        def post(self, *args, **kwargs):
            return FakeResponse()

    with (
        patch.object(app_state.market, "is_circuit_open", return_value=False),
        patch("services.openai_service._get_openai_client", return_value=FakeClient()),
        patch("services.openai_service.get_openai_base_url", return_value="http://example.test/v1"),
        patch("services.openai_service.get_openai_embeddings_model", return_value="m"),
        patch("services.openai_service._build_openai_headers", return_value={}),
    ):
        results = get_openai_embeddings_batch(["a", "b"], api_key="k")

    assert results == [[1.0], None]


def test_mistral_embeddings_use_provider_index_not_position() -> None:
    from types import SimpleNamespace

    from services import embeddings_service

    class FakeEmbeddings:
        def create(self, **kwargs):
            return SimpleNamespace(
                data=[
                    SimpleNamespace(index=1, embedding=[2.0]),
                    SimpleNamespace(index=0, embedding=[1.0]),
                ]
            )

    fake_client = SimpleNamespace(embeddings=FakeEmbeddings())

    with (
        patch("services.embeddings_service._get_client", return_value=fake_client),
        patch("credential_manager.is_openai_provider", return_value=False),
        patch("services.ai_service._acquire_mistral_call_slot", return_value=0.0),
        patch("services.ai_service._wait_for_rate_limit_slot", return_value="ready"),
        patch.object(app_state.market, "is_circuit_open", return_value=False),
        patch.object(app_state.market, "get_circuit_state", return_value={"status": "CLOSED"}),
    ):
        results = embeddings_service.get_mistral_embeddings_batch(
            ["review-index-a", "review-index-b"], "k"
        )

    assert results == [[1.0], [2.0]]


def test_mistral_tool_loop_normalizes_assistant_tool_call_id() -> None:
    from services.ai_service import call_mistral_chat_with_tools

    turn1 = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "",
                            "type": "function",
                            "function": {
                                "name": "get_stock_quote",
                                "arguments": '{"symbol": "AAPL"}',
                            },
                        }
                    ],
                }
            }
        ]
    }
    turn2 = {
        "choices": [
            {"message": {"role": "assistant", "content": "done"}}
        ]
    }

    with (
        patch("services.ai_service.call_mistral_chat", side_effect=[turn1, turn2]) as chat,
        patch("services.ai_tools.execute_mistral_tool_call", return_value={"price": 1.0}),
    ):
        call_mistral_chat_with_tools("k", [{"role": "user", "content": "x"}])

    second_messages = chat.call_args_list[1][0][1]
    assistant = next(m for m in second_messages if m.get("role") == "assistant")
    tool = next(m for m in second_messages if m.get("role") == "tool")
    assistant_id = assistant["tool_calls"][0]["id"]
    assert assistant_id
    assert tool["tool_call_id"] == assistant_id


def test_news_force_refresh_does_not_join_swr_job(client) -> None:
    import threading

    from routes import api_analysis
    from utils.caching import _set_cached_value

    _set_cached_value("news_bundle_latest_ddgs_only", {"items": ["old"]}, duration=86400)
    _set_cached_value("news_bundle_latest_ddgs_only_ts", 0.0, duration=86400)
    dummy_done = threading.Event()
    api_analysis.news_fetch_inflight["news_ddgs_only"] = {
        "result": None,
        "error": None,
        "done": dummy_done,
    }
    captured: dict = {}

    def _fake_news(**kwargs):
        captured.update(kwargs)
        return {"items": ["new"]}

    try:
        with (
            patch.object(api_analysis, "extract_api_key", return_value="test-api-key"),
            patch.object(api_analysis, "require_trusted_or_admin", return_value=(True, None)),
            patch.object(
                api_analysis.news_service,
                "get_synchronized_market_news",
                side_effect=_fake_news,
            ),
            patch.object(
                api_analysis,
                "_submit_in_app_context",
                side_effect=lambda executor, fn: fn(),
            ),
        ):
            response = client.post(
                "/api/news",
                json={"force": True},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
    finally:
        api_analysis.news_fetch_inflight.pop("news_ddgs_only", None)
        api_analysis.news_fetch_inflight.pop("news_ddgs_only_force", None)
        dummy_done.set()

    assert response.status_code == 200
    assert captured.get("force_refresh") is True
    assert response.get_json().get("items") == ["new"]


def test_copy_to_my_rollback_does_not_delete_concurrent_update(client) -> None:
    from routes.stocks import ai_portfolio as route
    from utils.storage import UserStocksPersistError

    app_state.market.user_us = {}
    app_state.market.last_usdjpy_rate = 150.0
    app_state.market.last_usdjpy_rate_ts = time.time()

    def _fail_after_concurrent_update():
        # Simulate a concurrent portfolio update mutating the same holding dict
        # in place before the persistence failure is reported.
        app_state.market.user_us["AAPL"]["shares"] = 999.0
        raise UserStocksPersistError("disk full")

    try:
        with (
            patch.object(route, "require_trusted_or_admin", return_value=(True, "")),
            patch.object(route, "_parse_json_request", return_value={"id": "x"}),
            patch.object(
                route,
                "_parse_ai_copy_to_my_request",
                return_value=([("AAPL", "us", 100.0, 100.0)], None),
            ),
            patch.object(route, "save_user_stocks", side_effect=_fail_after_concurrent_update),
            patch.object(route, "_watchlist_has_capacity", return_value=True),
        ):
            response = client.post(
                "/api/ai-portfolio/copy-to-my",
                json={"id": "x"},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )

        assert response.status_code == 503
        # The concurrent update must survive the failed request's rollback.
        assert app_state.market.user_us.get("AAPL", {}).get("shares") == 999.0
    finally:
        app_state.market.user_us = {}


def test_credentials_verify_rejects_non_string_openai_base_url(client) -> None:
    response = client.post(
        "/api/credentials/verify",
        json={"ai_provider": "openai_compatible", "openai_base_url": 123},
        headers={"Origin": "http://localhost:5000"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["ok"] is False
    assert "string" in str(payload.get("details", ""))


def test_history_half_open_duplicate_inflight_releases_probe(client) -> None:
    circuit_key = "us:SYM"
    cache_key = "hist_SYM_us_1mo"
    app_state.market.history_circuit_state[circuit_key] = CircuitState(
        status="HALF_OPEN", probing=False
    )
    app_state.history_fetch_inflight.add(cache_key)
    try:
        response = client.get(
            "/api/stock-history?symbol=SYM&market=us&period=1mo",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        assert response.status_code == 200
        state = app_state.market.history_circuit_state[circuit_key]
        assert state.get("probing") is False
    finally:
        app_state.history_fetch_inflight.discard(cache_key)
        app_state.market.history_circuit_state.pop(circuit_key, None)
