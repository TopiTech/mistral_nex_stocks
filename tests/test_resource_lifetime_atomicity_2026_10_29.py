"""Regression coverage for the 2026-10-29 resource/atomicity fixes.

* CONC-4: previous realtime prices are per-card UI state and must not survive
  in a symbol-keyed registry after the card is gone.
* MAIN-6: a fallback HTTP client is owned by the call that created it and is
  closed on success, failure, and generator close; pooled clients stay open.
* MAIN-7: one cache lookup decides presence and value, so a TTL expiry cannot
  turn an already-observed hit into a duplicate background fetch.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from cachetools import TTLCache

from app_state import app_state
from services.openai_service import (
    _CALL_OWNED_CLIENT_ATTR,
    _close_call_owned_openai_client,
    _get_openai_client,
    call_openai_chat,
    get_openai_embeddings_batch,
    stream_openai_chat,
)
from utils.caching import (
    _get_cached_value,
    _get_cached_value_if_present,
    _has_cached_key,
    global_cache,
)


class _Clock:
    """Manually advanced monotonic clock for deterministic TTL tests."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _install_cache(duration: int, clock: _Clock) -> TTLCache:
    cache: TTLCache = TTLCache(maxsize=4, ttl=1.0, timer=clock)
    cache["history"] = {"history": [{"c": 100.5}]}
    with global_cache.cache_lock:
        global_cache.caches[duration] = cache
    return cache


def _remove_cache(duration: int) -> None:
    with global_cache.cache_lock:
        global_cache.caches.pop(duration, None)


def test_split_check_then_get_misses_when_ttl_advances_between_calls() -> None:
    """Pin the defect the single-lookup helper replaces: two TTL evaluations."""
    clock = _Clock()
    duration = 987_654
    _install_cache(duration, clock)
    try:
        clock.now = 0.9
        assert _has_cached_key("history", duration) is True
        clock.now = 1.1
        assert _get_cached_value("history", duration) is None
    finally:
        _remove_cache(duration)


def test_get_cached_value_if_present_uses_one_expiry_evaluation() -> None:
    clock = _Clock()
    duration = 987_655
    _install_cache(duration, clock)
    try:
        clock.now = 0.9
        present, value = _get_cached_value_if_present("history", duration)
        assert present is True
        assert value == {"history": [{"c": 100.5}]}

        clock.now = 1.1
        present, value = _get_cached_value_if_present("history", duration)
        assert present is False
        assert value is None
    finally:
        _remove_cache(duration)


def test_get_cached_value_if_present_distinguishes_stored_none() -> None:
    duration = 987_656
    with global_cache.cache_lock:
        cache: TTLCache = TTLCache(maxsize=4, ttl=60)
        cache["empty"] = None
        global_cache.caches[duration] = cache
    try:
        assert _get_cached_value_if_present("empty", duration) == (True, None)
        assert _get_cached_value_if_present("missing", duration) == (False, None)
    finally:
        _remove_cache(duration)


def test_stock_history_route_uses_one_cache_lookup(client) -> None:
    """The route must resolve a hit with a single atomic lookup, not two."""
    payload = {
        "history": [
            {"x": 1793145600000, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5, "v": 1000}
        ]
    }
    lookups: list[str] = []

    def lookup(key, duration, default=None):
        lookups.append(key)
        return True, payload

    with (
        patch("routes.stocks.quotes._get_cached_value_if_present", side_effect=lookup),
        patch("routes.stocks.quotes._submit_async_history_fetch") as submit,
    ):
        resp = client.get(
            "/api/stock-history?symbol=AAPL&market=us&period=1mo",
            headers={"Origin": "http://localhost:5000"},
        )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["history"][0]["c"] == 100.5
    assert len(lookups) == 1, "the route must not split the cache hit into two lookups"
    submit.assert_not_called()


def test_get_openai_client_marks_only_fallback_as_call_owned() -> None:
    fallback = MagicMock()
    with (
        patch.object(
            app_state.ai,
            "get_or_create_openai_client",
            side_effect=RuntimeError("pool unavailable"),
        ),
        patch("services.openai_service._create_httpx_client", return_value=fallback),
    ):
        client = _get_openai_client("http://localhost:11434/v1", "key")

    assert client is fallback
    assert getattr(client, _CALL_OWNED_CLIENT_ATTR, False) is True
    client.close.assert_not_called()

    _close_call_owned_openai_client(client)
    client.close.assert_called_once()


def test_get_openai_client_pooled_client_is_never_closed() -> None:
    pooled = MagicMock()
    with patch.object(app_state.ai, "get_or_create_openai_client", return_value=pooled):
        client = _get_openai_client("http://localhost:11434/v1", "key")

    assert client is pooled
    _close_call_owned_openai_client(client)
    pooled.close.assert_not_called()


def _call_owned_client() -> MagicMock:
    client = MagicMock()
    setattr(client, _CALL_OWNED_CLIENT_ATTR, True)
    return client


def test_call_openai_chat_closes_call_owned_client_on_success() -> None:
    client = _call_owned_client()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "hello"}}]
    }
    client.post.return_value = resp

    with patch("services.openai_service._get_openai_client", return_value=client):
        result = call_openai_chat(
            "sk-test", [{"role": "user", "content": "hi"}], use_cache=False
        )

    assert result["choices"][0]["message"]["content"] == "hello"
    client.close.assert_called_once()


def test_call_openai_chat_closes_call_owned_client_on_transport_error() -> None:
    client = _call_owned_client()
    client.post.side_effect = httpx.ConnectError("boom")

    with patch("services.openai_service._get_openai_client", return_value=client):
        result = call_openai_chat(
            "sk-test", [{"role": "user", "content": "hi"}], use_cache=False
        )

    assert result["error"]["status_code"] == 503
    client.close.assert_called_once()


def _stream_client(lines: list[bytes]) -> MagicMock:
    client = _call_owned_client()
    stream_resp = MagicMock()
    stream_resp.status_code = 200
    stream_resp.iter_lines.return_value = lines
    stream_cm = MagicMock()
    stream_cm.__enter__.return_value = stream_resp
    client.stream.return_value = stream_cm
    return client


def test_stream_openai_chat_closes_call_owned_client_on_completion() -> None:
    client = _stream_client(
        [
            b'data: {"choices": [{"delta": {"content": "hi"}}]}',
            b"data: [DONE]",
        ]
    )

    with patch("services.openai_service._get_openai_client", return_value=client):
        events = list(stream_openai_chat("sk-test", [{"role": "user", "content": "hi"}]))

    assert any(event.get("type") == "done" for event in events)
    client.close.assert_called_once()


def test_stream_openai_chat_closes_call_owned_client_when_stream_raises() -> None:
    client = _call_owned_client()
    client.stream.side_effect = httpx.ConnectError("boom")

    with patch("services.openai_service._get_openai_client", return_value=client):
        events = list(stream_openai_chat("sk-test", [{"role": "user", "content": "hi"}]))

    assert events[-1]["type"] == "error"
    assert events[-1]["status_code"] == 503
    client.close.assert_called_once()


@pytest.mark.parametrize("fail", [False, True])
def test_embeddings_closes_call_owned_client_on_every_path(fail: bool) -> None:
    client = _call_owned_client()
    if fail:
        client.post.side_effect = httpx.ConnectError("boom")
    else:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"data": [{"index": 0, "embedding": [0.5]}]}
        client.post.return_value = resp

    with patch("services.openai_service._get_openai_client", return_value=client):
        vectors = get_openai_embeddings_batch(["doc"], api_key="sk-test")

    assert vectors == ([None] if fail else [[0.5]])
    client.close.assert_called_once()
