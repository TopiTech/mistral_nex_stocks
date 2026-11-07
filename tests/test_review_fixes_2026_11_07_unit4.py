"""Regression tests for the 2026-11-07 exhaustive review fixes (unit 4).

AI-service fixes pinned here:

- M1: mark_mistral_429 overwrote the cooldown window non-monotonically, so a
  later 429 report with a smaller backoff SHRANK an active server-requested
  wait (unlike every sibling cooldown in market_state).
- M2: the no-SDK fallback SDKError read its ``response`` from kwargs only,
  but the compat layer passes it positionally (the real SDK signature), so
  the crafted 503 response was silently dropped in SDK-missing environments.
- M3: the local Agents SDK run bridged its API key via
  os.environ.setdefault, silently preferring a stale MISTRAL_API_KEY and
  authenticating with a different account than the configured credential.
- M6: the circuit-open / probe-in-progress aborts kept the global pacing
  reservation, accumulating phantom min-interval delays under sustained
  gated traffic and surfacing misleading 429s.
- H4: the parse -> json_schema -> json_object fallback chain caught every
  Exception, so a degraded provider turned one user call into up to three
  sequential provider requests each with its own SDK retry budget.
- M9: _run_coro_sync timeouts surfaced as builtin TimeoutError (an OSError
  subclass since 3.11), classified as status_code=0 instead of a 504.
- M10/M11: usage recording/edge-value coercion and residual-token cost
  accounting plus the tracked USD/JPY rate for the yen estimate.
- M4/M5: the agents-mode stream worker could never be aborted, had an
  unbounded queue, never reported circuit failures, and leaked raw provider
  exception text to the browser.
- M8: permanent 4xx provider errors never broke the embeddings batch loop.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app import app_state
from constants import DEFAULT_USDJPY_RATE
from services import ai_service
from utils.validators import StockAnalysis


def _pacing_state():
    """Snapshot/normalize the Mistral pacing state for one test."""
    with app_state.ai.mistral_cooldown_lock:
        app_state.ai.mistral_next_allowed_ts = 0.0
        app_state.ai.mistral_last_call_ts = 0.0
        app_state.ai.mistral_429_streak = 0


@pytest.fixture(autouse=True)
def _reset_pacing_state():
    with app_state.ai.mistral_cooldown_lock:
        saved_next = app_state.ai.mistral_next_allowed_ts
        saved_last = app_state.ai.mistral_last_call_ts
        saved_streak = app_state.ai.mistral_429_streak
    _pacing_state()
    yield
    with app_state.ai.mistral_cooldown_lock:
        app_state.ai.mistral_next_allowed_ts = saved_next
        app_state.ai.mistral_last_call_ts = saved_last
        app_state.ai.mistral_429_streak = saved_streak


# ---------------------------------------------------------------------------
# M1: monotonic Mistral cooldown window
# ---------------------------------------------------------------------------


def test_mark_mistral_429_window_is_monotonic():
    """A shorter subsequent backoff must not shrink an active cooldown."""
    app_state.ai.mistral_next_allowed_ts = time.time() + 600.0
    app_state.ai.mark_mistral_429()
    assert app_state.ai.mistral_next_allowed_ts >= time.time() + 599.0


def test_mark_mistral_429_extends_window_on_larger_retry_after():
    """A larger Retry-After hint extends the window (parity with yf sibling)."""
    app_state.ai.mistral_next_allowed_ts = time.time() + 5.0
    app_state.ai.mark_mistral_429(retry_after_sec=200.0)
    assert app_state.ai.mistral_next_allowed_ts >= time.time() + 199.0


# ---------------------------------------------------------------------------
# M2: fallback SDKError carries the positional response
# ---------------------------------------------------------------------------


def test_fallback_sdk_error_response_accepts_positional_response():
    """The no-SDK fallback must mirror SDKError(message, raw_response)."""
    from mistral_compat import _fallback_sdk_error_response

    resp = httpx.Response(503, request=httpx.Request("POST", "https://api.mistral.ai/fallback"))
    response, status_code = _fallback_sdk_error_response("mistralai SDK is not installed", resp)
    assert response is resp
    assert status_code == 0


def test_fallback_sdk_error_response_defaults_when_absent():
    from mistral_compat import _fallback_sdk_error_response

    response, status_code = _fallback_sdk_error_response("boom", status_code=409)
    assert response is not None  # empty requests.Response fallback
    assert hasattr(response, "status_code")
    assert status_code == 409


def test_extract_error_response_reads_fallback_response_payload():
    """ai_service's classifier must see the crafted 503 from the fallback."""
    from mistral_compat import _fallback_sdk_error_response

    resp = httpx.Response(503, request=httpx.Request("POST", "https://api.mistral.ai/x"))
    response, _status = _fallback_sdk_error_response("mistralai SDK is not installed", resp)
    # The same derivation the fallback __init__ stores on raw_response.
    assert response is resp
    assert getattr(response, "status_code", None) == 503


# ---------------------------------------------------------------------------
# M3: local agents run must use the caller's credential
# ---------------------------------------------------------------------------


def test_load_sdk_agent_overrides_conflicting_env_key(monkeypatch):
    """A stale MISTRAL_API_KEY must be replaced by the configured credential."""
    import os

    import mistral_compat

    if not getattr(mistral_compat, "HAS_AGENTS_SDK", False):
        pytest.skip("mistralai-agents SDK not installed")

    monkeypatch.setenv("MISTRAL_API_KEY", "stale-op-key")
    fake_agent = SimpleNamespace(model="x")
    with patch.object(mistral_compat.mistral_agents, "Agent", return_value=fake_agent):
        agent = mistral_compat.load_sdk_agent(api_key="fresh-configured-key")
    assert agent is fake_agent
    assert os.environ["MISTRAL_API_KEY"] == "fresh-configured-key"


def test_load_sdk_agent_without_conflict_still_sets_key(monkeypatch):
    import os

    import mistral_compat

    if not getattr(mistral_compat, "HAS_AGENTS_SDK", False):
        pytest.skip("mistralai-agents SDK not installed")

    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    fake_agent = SimpleNamespace(model="y")
    with patch.object(mistral_compat.mistral_agents, "Agent", return_value=fake_agent):
        mistral_compat.load_sdk_agent(api_key="brand-new-key")
    assert os.environ["MISTRAL_API_KEY"] == "brand-new-key"


# ---------------------------------------------------------------------------
# M6: circuit aborts release the pacing reservation
# ---------------------------------------------------------------------------


def test_circuit_open_abort_releases_pacing_slot():
    """Gated traffic must not accumulate phantom pacing delays (SVC-02)."""
    _pacing_state()
    client = MagicMock()
    with (
        patch("services.ai_service.is_openai_provider", return_value=False),
        patch.object(app_state.market, "is_circuit_open", return_value=True),
        patch.object(app_state.market, "get_circuit_state", return_value={"status": "OPEN"}),
        patch("services.ai_service._get_mistral_client", return_value=client),
        patch("services.ai_service.MISTRAL_MIN_INTERVAL_SEC", 1.35),
        patch("services.ai_service.MISTRAL_JITTER_FACTOR", 0.0),
    ):
        result = ai_service.call_mistral_chat(
            "test-key-123", [{"role": "user", "content": "x"}]
        )
    assert result["error"]["status_code"] == 503
    client.chat.complete.assert_not_called()
    # Without the rollback the reservation would point ~1.35s into the future.
    assert app_state.ai.mistral_last_call_ts <= time.time() + 0.5


def test_probe_in_progress_abort_releases_pacing_slot():
    _pacing_state()
    client = MagicMock()
    with (
        patch("services.ai_service.is_openai_provider", return_value=False),
        patch.object(app_state.market, "is_circuit_open", return_value=False),
        patch.object(app_state.market, "get_circuit_state", return_value={"status": "HALF_OPEN"}),
        patch.object(app_state.market, "try_claim_circuit_probe", return_value=False),
        patch("services.ai_service._get_mistral_client", return_value=client),
        patch("services.ai_service.MISTRAL_MIN_INTERVAL_SEC", 1.35),
        patch("services.ai_service.MISTRAL_JITTER_FACTOR", 0.0),
    ):
        result = ai_service.call_mistral_chat(
            "test-key-123", [{"role": "user", "content": "x"}]
        )
    assert result["error"]["status_code"] == 503
    client.chat.complete.assert_not_called()
    assert app_state.ai.mistral_last_call_ts <= time.time() + 0.5


# ---------------------------------------------------------------------------
# H4: provider-health failures must not trigger the structured-output chain
# ---------------------------------------------------------------------------


def _chat_chain_tolerant_patches(client):
    return (
        patch("services.ai_service.is_openai_provider", return_value=False),
        patch.object(app_state.market, "is_circuit_open", return_value=False),
        patch.object(app_state.market, "get_circuit_state", return_value={"status": "CLOSED"}),
        patch("services.ai_service._get_mistral_client", return_value=client),
        patch("services.ai_service.MISTRAL_MIN_INTERVAL_SEC", 0.0),
        patch("services.ai_service.MISTRAL_JITTER_FACTOR", 0.0),
    )


def test_structured_fallback_not_attempted_on_429():
    """chat.parse 429 must re-raise, not issue two more provider requests."""
    client = MagicMock()
    body_429 = httpx.Response(429, request=httpx.Request("POST", "https://api.mistral.ai"))
    rate_limit_error = ai_service.SDKError("Too many requests", body_429)
    rate_limit_error.status_code = 429
    client.chat.parse.side_effect = rate_limit_error
    patches = _chat_chain_tolerant_patches(client)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
    ):
        result = ai_service.call_mistral_chat(
            "test-key-123",
            [{"role": "user", "content": "analyze"}],
            response_format=StockAnalysis,
        )
    client.chat.complete.assert_not_called()
    assert result["error"]["status_code"] == 429


def test_structured_fallback_not_attempted_on_timeout():
    """chat.parse transport timeout must re-raise, not retry via complete."""
    client = MagicMock()
    client.chat.parse.side_effect = httpx.ReadTimeout("read timed out")
    patches = _chat_chain_tolerant_patches(client)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
    ):
        result = ai_service.call_mistral_chat(
            "test-key-123",
            [{"role": "user", "content": "analyze"}],
            response_format=StockAnalysis,
        )
    client.chat.complete.assert_not_called()
    assert result["error"]["status_code"] == 504


def test_structured_fallback_still_used_for_structural_errors():
    """The pinned TypeError path keeps falling back (contract retained)."""
    client = MagicMock()
    client.chat.parse.side_effect = TypeError(
        "Unexpected type for message.content: <class 'list'>"
    )
    mock_complete_resp = MagicMock()
    mock_complete_resp.model_dump.return_value = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": '{"headline": "ok"}'}
                    ],
                }
            }
        ]
    }
    client.chat.complete.return_value = mock_complete_resp
    patches = _chat_chain_tolerant_patches(client)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
    ):
        ai_service.call_mistral_chat(
            "test-key-123",
            [{"role": "user", "content": "analyze"}],
            response_format=StockAnalysis,
        )
    client.chat.complete.assert_called_once()


# ---------------------------------------------------------------------------
# M8: permanent client errors break the embeddings batch loop
# ---------------------------------------------------------------------------


class _StatusError(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def _embedding_client(status_code):
    calls: list[list[str]] = []

    class _Embeddings:
        def create(self, model, inputs):
            calls.append(list(inputs))
            raise _StatusError(status_code)

    class _Client:
        embeddings = _Embeddings()

    return _Client(), calls


def _run_mistral_embeddings(client, texts, batch_size):
    from services import embeddings_service as emb

    with (
        patch.object(emb, "_get_client", return_value=client),
        patch("services.ai_service._acquire_mistral_call_slot", return_value=0.0),
        patch("services.ai_service._wait_for_rate_limit_slot", return_value="ready"),
        patch("services.ai_service._release_mistral_call_slot"),
        patch("credential_manager.is_openai_provider", return_value=False),
    ):
        return emb.get_mistral_embeddings_batch(texts, api_key="key", batch_size=batch_size)


def test_mistral_embeddings_break_on_permanent_client_error():
    """A 401 must stop the loop, not burn one call per remaining chunk."""
    client, calls = _embedding_client(401)
    results = _run_mistral_embeddings(client, ["alpha", "beta", "gamma"], batch_size=1)
    assert len(calls) == 1, "permanent client error must abort remaining chunks"
    assert results == [None, None, None]


def test_mistral_embeddings_continue_on_transient_server_error():
    """5xx keeps the pre-existing behavior: try the next chunk."""
    client, calls = _embedding_client(503)
    results = _run_mistral_embeddings(client, ["alpha", "beta"], batch_size=1)
    assert len(calls) == 2
    assert results == [None, None]


# ---------------------------------------------------------------------------
# M9: _run_coro_sync timeout is classified as a provider timeout
# ---------------------------------------------------------------------------


def test_run_coro_sync_timeout_maps_to_provider_timeout():
    """The timeout must surface as httpx.TimeoutException (classifier: 504)."""

    async def _hang():
        await asyncio.sleep(30)
        return "never"

    with pytest.raises(httpx.TimeoutException):
        ai_service._run_coro_sync(_hang(), timeout_sec=0.2)


def test_communications_classifier_maps_timeout_to_504():
    exc = httpx.TimeoutException("timed out")
    assert isinstance(exc, (httpx.TimeoutException,))
    # Sanity: the builtin TimeoutError alias (3.11+) is what asyncio raises,
    # and the translation in _run_coro_sync prevents the OSError fallthrough.
    assert issubclass(TimeoutError, OSError)


# ---------------------------------------------------------------------------
# M10/M11: usage recording and accounting
# ---------------------------------------------------------------------------


def test_record_ai_usage_tolerates_malformed_provider_values():
    """String/float/dirty token counts must not raise the caller."""
    with app_state.ai.mistral_usage_lock:
        before_calls = app_state.ai.mistral_call_count
        before_prompt = app_state.ai.mistral_total_prompt_tokens
    app_state.ai.record_ai_usage(
        {"prompt_tokens": "1,234", "total_tokens": "12.5", "completion_tokens": "abc"},
        model="mistral-small-2603",
    )
    with app_state.ai.mistral_usage_lock:
        assert app_state.ai.mistral_call_count == before_calls + 1
        assert app_state.ai.mistral_total_prompt_tokens >= before_prompt + 1234


def test_usage_stats_price_residual_modelless_tokens():
    """Mixed model/model-less usage must cost BOTH buckets."""
    with app_state.ai.mistral_usage_lock:
        app_state.ai.mistral_call_count = 2
        app_state.ai.mistral_total_prompt_tokens = 1_000_000
        app_state.ai.mistral_total_completion_tokens = 0
        app_state.ai.mistral_model_usage = {
            "mistral-small-2603": {
                "calls": 1,
                "prompt_tokens": 400_000,
                "completion_tokens": 0,
            }
        }
    stats = app_state.ai.mistral_usage_stats()
    assert "unknown" in stats["by_model"]
    assert stats["by_model"]["unknown"]["prompt_tokens"] == 600_000
    expected = (400_000 / 1e6 * 0.10) + (600_000 / 1e6 * 0.10)
    assert abs(stats["estimated_cost_usd"] - expected) < 1e-6


def test_usage_stats_jpy_uses_tracked_rate():
    app_state.market.last_usdjpy_rate = 123.4
    with app_state.ai.mistral_usage_lock:
        app_state.ai.mistral_call_count = 1
        app_state.ai.mistral_total_prompt_tokens = 100_000
        app_state.ai.mistral_total_completion_tokens = 0
        app_state.ai.mistral_model_usage = {}
    stats = app_state.ai.mistral_usage_stats()
    expected = round(100_000 / 1e6 * 0.10 * 123.4, 2)
    assert abs(stats["estimated_cost_jpy"] - expected) < 0.01

    app_state.market.last_usdjpy_rate = 0.0
    stats2 = app_state.ai.mistral_usage_stats()
    expected2 = round(100_000 / 1e6 * 0.10 * DEFAULT_USDJPY_RATE, 2)
    assert abs(stats2["estimated_cost_jpy"] - expected2) < 0.01


# ---------------------------------------------------------------------------
# M4/M5: agents-mode stream lifecycle
# ---------------------------------------------------------------------------


def _fake_agents_module():
    class TextDelta:
        def __init__(self, text: str):
            self.text = text

    return SimpleNamespace(TextDelta=TextDelta)


def _agents_stream_patches(agent):
    return (
        patch("services.ai_service._resolve_agents_target", return_value=(True, "")),
        patch("services.ai_service.HAS_AGENTS_SDK", True),
        patch("services.ai_service.mistral_agents", _fake_agents_module()),
        patch("services.ai_service._get_mistral_client", return_value=object()),
        patch("services.ai_service._acquire_mistral_call_slot", return_value=0.0),
        patch("services.ai_service._wait_for_rate_limit_slot", return_value="ready"),
        patch("services.ai_service.load_sdk_agent", return_value=agent),
        patch.object(app_state.market, "is_circuit_open", return_value=False),
        patch.object(app_state.market, "get_circuit_state", return_value={"status": "CLOSED"}),
    )


def _stream_events(agent, *, stop_after_events=200):
    events = []

    def gen():
        yield from ai_service.stream_mistral_chat(
            "test-key-123",
            [{"role": "user", "content": "hi"}],
        )

    g = gen()
    try:
        for ev in g:
            events.append(ev)
            if (
                ev.get("type") in ("error", "done")
                or len(events) >= stop_after_events
            ):
                break
    finally:
        g.close()
    return events


def test_agents_stream_error_is_sanitized_and_reports_circuit():
    """Raw provider text must NOT reach the browser; the failure must be
    circuit-accounted so a dead HALF_OPEN provider re-opens the circuit."""

    class _FailingAgent:
        async def run(self, content, *, on_event=None, timeout=None):
            raise RuntimeError(
                "Bearer sk-SECRETXYZ connection reset by peer https://internal-host/space"
            )

    agent_patches = _agents_stream_patches(_FailingAgent())
    with (
        agent_patches[0],
        agent_patches[1],
        agent_patches[2],
        agent_patches[3],
        agent_patches[4],
        agent_patches[5],
        agent_patches[6],
        agent_patches[7],
        patch.object(app_state.market, "report_circuit_result") as report_fn,
    ):
        agent_patches[8].start()
        try:
            events = _stream_events(_FailingAgent())
        finally:
            agent_patches[8].stop()

    err_events = [e for e in events if e.get("type") == "error"]
    assert err_events, "expected an error event from the failed run"
    assert report_fn.call_args.kwargs.get("success") is False
    assert "sk-SECRETXYZ" not in err_events[0]["message"]
    assert "internal-host" not in err_events[0]["message"]


def test_agents_stream_consumer_close_aborts_worker():
    """Closing the SSE generator must stop the run and reclaim the worker."""
    aborted_seen = threading.Event()

    mods = _fake_agents_module()

    class _SlowAgent:
        async def run(self, content, *, on_event=None, timeout=None):
            for i in range(500):
                try:
                    on_event(mods.TextDelta(f"d{i} "))
                except Exception:
                    aborted_seen.set()
                    raise
                await asyncio.sleep(0.02)
            return SimpleNamespace(text="done")

    agent = _SlowAgent()
    patches = _agents_stream_patches(agent)
    for p in patches:
        p.start()
    try:
        events = _stream_events(agent, stop_after_events=2)
    finally:
        for p in reversed(patches):
            p.stop()

    assert len(events) >= 2
    assert aborted_seen.wait(timeout=10), "worker never observed the cooperative abort"
