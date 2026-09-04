"""Tests for the Mistral API improvements.

Covers:
  A-1  empty responses are never cached
  A-2  capacity errors (service_tier_capacity_exceeded / code 3505) backoff
  B-1  SDK retries parameter is forwarded
  B-3  chat history character budget trimming
  B-4  reasoning_effort model set can be extended via env
  C-1  temperature parameter forwarding + cache-key partitioning
  C-4  token usage counters
  C-2  streaming: stream_mistral_chat events + /api/chat SSE response
  D-3  normalize_chat_parse_payload shapes
"""

import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from app_state import app_state
from mistral_compat import BackoffStrategy, RetryConfig, SDKError


class EmptyResponseCacheTestCase(unittest.TestCase):
    """A-1: empty responses must never be cached."""

    def setUp(self):
        with app_state.ai.mistral_response_lock:
            app_state.ai.mistral_response_cache.clear()

    @patch("services.ai_service._get_mistral_model_name", return_value="mistral-small-2603")
    @patch("services.ai_service._get_mistral_client")
    def test_empty_content_is_not_cached(self, mock_get_client, mock_get_name):
        from services import ai_service

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        def make_response(content):
            resp = MagicMock()
            resp.model_dump.return_value = {"choices": [{"message": {"content": content}}]}
            return resp

        mock_client.chat.complete.side_effect = [make_response(""), make_response("Hello")]

        res1 = ai_service.call_mistral_chat(
            "key-empty-1", [{"role": "user", "content": "hi"}], use_cache=True
        )
        self.assertEqual(res1["choices"][0]["message"]["content"], "")
        # Identical request: the empty result must NOT have been cached, so the
        # API is called again and returns the usable reply.
        res2 = ai_service.call_mistral_chat(
            "key-empty-1", [{"role": "user", "content": "hi"}], use_cache=True
        )
        self.assertEqual(res2["choices"][0]["message"]["content"], "Hello")
        self.assertEqual(mock_client.chat.complete.call_count, 2)

    @patch("services.ai_service._get_mistral_model_name", return_value="mistral-small-2603")
    @patch("services.ai_service._get_mistral_client")
    def test_usable_content_is_cached(self, mock_get_client, mock_get_name):
        from services import ai_service

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        def make_response(content):
            resp = MagicMock()
            resp.model_dump.return_value = {"choices": [{"message": {"content": content}}]}
            return resp

        resp = make_response("Hello")
        mock_client.chat.complete.side_effect = [resp, resp]

        ai_service.call_mistral_chat(
            "key-usable-1", [{"role": "user", "content": "hi"}], use_cache=True
        )
        res2 = ai_service.call_mistral_chat(
            "key-usable-1", [{"role": "user", "content": "hi"}], use_cache=True
        )
        self.assertEqual(res2["choices"][0]["message"]["content"], "Hello")
        self.assertEqual(mock_client.chat.complete.call_count, 1)


def _make_sdk_error(message: str, status_code: int = 400, body: str | None = None) -> SDKError:
    """Build a real SDKError carrying the given HTTP response.

    Uses the real ``httpx.Response`` so the test exercises the same attribute
    surface (``raw_response`` / ``status_code``) as production (R1). The default
    status 400 (non-429) exercises the payload-based capacity detection that
    must still trigger a backoff.
    """
    req = httpx.Request("POST", "https://api.mistral.ai/v1/chat/completions")
    resp = httpx.Response(
        status_code,
        request=req,
        json={"error": {"type": "service_tier_capacity_exceeded", "code": "3505"}},
    )
    return SDKError(message, resp, body)


class CapacityErrorBackoffTestCase(unittest.TestCase):
    """A-2: capacity errors trigger the 429 backoff even without HTTP 429."""

    @patch("services.ai_service._get_mistral_model_name", return_value="mistral-small-2603")
    @patch("services.ai_service._get_mistral_client")
    def test_capacity_error_triggers_backoff(self, mock_get_client, mock_get_name):
        from services import ai_service

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        # HTTP status 400 (no 429): capacity detection must still back off.
        mock_client.chat.complete.side_effect = _make_sdk_error("capacity exceeded")

        old_streak = app_state.ai.mistral_429_streak
        old_next = app_state.ai.mistral_next_allowed_ts
        try:
            app_state.ai.mistral_429_streak = 0
            res = ai_service.call_mistral_chat(
                "key-capacity-1", [{"role": "user", "content": "hi"}], use_cache=False
            )
            self.assertIn("error", res)
            self.assertGreater(app_state.ai.mistral_429_streak, 0)
            self.assertGreater(app_state.ai.mistral_next_allowed_ts, time.time() - 1)
        finally:
            app_state.ai.mistral_429_streak = old_streak
            app_state.ai.mistral_next_allowed_ts = old_next

    @patch("services.ai_service._get_mistral_model_name", return_value="mistral-small-2603")
    @patch("services.ai_service._get_mistral_client")
    def test_retry_after_header_honored(self, mock_get_client, mock_get_name):
        """R1: a 429 Retry-After header must floor the cooldown."""
        from services import ai_service

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        req = httpx.Request("POST", "https://api.mistral.ai/v1/chat/completions")
        resp = httpx.Response(
            429,
            request=req,
            headers={"Retry-After": "120"},
            json={"error": {"type": "rate_limit_exceeded"}},
        )
        mock_client.chat.complete.side_effect = SDKError("rate limited", resp)

        old_streak = app_state.ai.mistral_429_streak
        old_next = app_state.ai.mistral_next_allowed_ts
        try:
            app_state.ai.mistral_429_streak = 0
            res = ai_service.call_mistral_chat(
                "key-retry-1", [{"role": "user", "content": "hi"}], use_cache=False
            )
            self.assertIn("error", res)
            # Streak backoff (2^1=2s) is smaller than Retry-After (120s), so the
            # server-suggested wait must be used as the floor.
            remaining = app_state.ai.mistral_next_allowed_ts - time.time()
            self.assertGreaterEqual(remaining, 118.0)
        finally:
            app_state.ai.mistral_429_streak = old_streak
            app_state.ai.mistral_next_allowed_ts = old_next


class SdkRetriesTestCase(unittest.TestCase):
    """B-1: the SDK retries parameter is forwarded."""

    @patch("services.ai_service._get_mistral_model_name", return_value="mistral-small-2603")
    @patch("services.ai_service._get_mistral_client")
    def test_retries_passed_to_sdk(self, mock_get_client, mock_get_name):
        from services import ai_service

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        resp = MagicMock()
        resp.model_dump.return_value = {"choices": [{"message": {"content": "ok"}}]}
        mock_client.chat.complete.return_value = resp

        ai_service.call_mistral_chat(
            "key-retries-1", [{"role": "user", "content": "hi"}], use_cache=False
        )
        _, kwargs = mock_client.chat.complete.call_args
        self.assertIn("retries", kwargs)
        retry_config = kwargs["retries"]
        self.assertIsInstance(retry_config, RetryConfig)
        self.assertIsInstance(retry_config.backoff, BackoffStrategy)
        self.assertEqual(retry_config.strategy, "backoff")
        self.assertTrue(retry_config.retry_connection_errors)
        expected_request_budget = int(ai_service.MISTRAL_API_TIMEOUT_SEC * 1000) * (
            ai_service.MISTRAL_SDK_RETRIES + 1
        )
        self.assertGreaterEqual(retry_config.backoff.max_elapsed_time, expected_request_budget)

    @patch("services.ai_service.MISTRAL_SDK_RETRIES", 0)
    def test_zero_retries_disables_sdk_retry_config(self):
        from services import ai_service

        self.assertIsNone(ai_service._build_mistral_retry_config())


class TemperatureTestCase(unittest.TestCase):
    """C-1: temperature forwarding + cache-key partitioning."""

    @patch("services.ai_service._get_mistral_model_name", return_value="mistral-small-2603")
    @patch("services.ai_service._get_mistral_client")
    def test_temperature_passed_through(self, mock_get_client, mock_get_name):
        from services import ai_service

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        resp = MagicMock()
        resp.model_dump.return_value = {"choices": [{"message": {"content": "ok"}}]}
        mock_client.chat.complete.return_value = resp

        ai_service.call_mistral_chat(
            "key-temp-1",
            [{"role": "user", "content": "hi"}],
            use_cache=False,
            temperature=0.0,
        )
        _, kwargs = mock_client.chat.complete.call_args
        self.assertEqual(kwargs["temperature"], 0.0)

    def test_temperature_partitions_cache_key(self):
        from services import ai_service

        msgs = [{"role": "user", "content": "hello"}]
        key_low = ai_service._build_mistral_cache_key(
            "mistral-small-2603", msgs, 600, None, temperature=0.0
        )
        key_none = ai_service._build_mistral_cache_key(
            "mistral-small-2603", msgs, 600, None, temperature=None
        )
        self.assertNotEqual(key_low, key_none)


class ReasoningModelsExtraTestCase(unittest.TestCase):
    """B-4: MNS_MISTRAL_REASONING_MODELS_EXTRA extends the built-in set."""

    @patch("services.ai_service.MISTRAL_REASONING_MODELS_EXTRA", "mistral-large-2512, custom-xyz")
    def test_extra_models_support_reasoning(self):
        from services import ai_service

        self.assertTrue(ai_service._supports_reasoning_effort("mistral-large-2512"))
        self.assertTrue(ai_service._supports_reasoning_effort("custom-xyz"))
        self.assertFalse(ai_service._supports_reasoning_effort("mistral-large-latest"))


class StreamReasoningEffortTestCase(unittest.TestCase):
    """R6: stream_mistral_chat must honor MNS_MISTRAL_REASONING_EFFORT like call_mistral_chat."""

    @patch("services.ai_service._get_mistral_model_name", return_value="mistral-small-2603")
    @patch("services.ai_service._get_mistral_client")
    def test_stream_honors_env_override(self, mock_get_client, mock_get_name):
        from services import ai_service

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.chat.stream.return_value = iter([])

        with patch.dict(os.environ, {"MNS_MISTRAL_REASONING_EFFORT": "none"}):
            events = list(
                ai_service.stream_mistral_chat("key-env-1", [{"role": "user", "content": "hi"}])
            )
        self.assertEqual(events[-1]["type"], "done")
        _, kwargs = mock_client.chat.stream.call_args
        self.assertEqual(kwargs["reasoning_effort"], "none")

    @patch("services.ai_service._get_mistral_model_name", return_value="mistral-small-2603")
    @patch("services.ai_service._get_mistral_client")
    def test_stream_defaults_to_model_reasoning(self, mock_get_client, mock_get_name):
        from services import ai_service

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.chat.stream.return_value = iter([])

        with patch.dict(os.environ, {"MNS_MISTRAL_REASONING_EFFORT": ""}):
            events = list(
                ai_service.stream_mistral_chat("key-env-2", [{"role": "user", "content": "hi"}])
            )
        self.assertEqual(events[-1]["type"], "done")
        _, kwargs = mock_client.chat.stream.call_args
        self.assertEqual(kwargs["reasoning_effort"], "none")

    @patch("services.ai_service._get_mistral_model_name", return_value="mistral-small-2603")
    @patch("services.ai_service._get_mistral_client")
    def test_call_path_matches_stream_path(self, mock_get_client, mock_get_name):
        """Both chat paths must resolve the same env override (R6)."""
        from services import ai_service

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.chat.stream.return_value = iter([])
        resp = MagicMock()
        resp.model_dump.return_value = {"choices": [{"message": {"content": "ok"}}]}
        mock_client.chat.complete.return_value = resp

        with patch.dict(os.environ, {"MNS_MISTRAL_REASONING_EFFORT": "high"}):
            ai_service.call_mistral_chat(
                "key-env-3", [{"role": "user", "content": "hi"}], use_cache=False
            )
            list(ai_service.stream_mistral_chat("key-env-3", [{"role": "user", "content": "hi"}]))
        _, complete_kwargs = mock_client.chat.complete.call_args
        _, stream_kwargs = mock_client.chat.stream.call_args
        self.assertEqual(complete_kwargs["reasoning_effort"], "high")
        self.assertEqual(stream_kwargs["reasoning_effort"], "high")


class UsageRecordingTestCase(unittest.TestCase):
    """C-4: cumulative token usage counters."""

    @patch("services.ai_service._get_mistral_model_name", return_value="mistral-small-2603")
    @patch("services.ai_service._get_mistral_client")
    def test_usage_recorded(self, mock_get_client, mock_get_name):
        from services import ai_service

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        resp = MagicMock()
        resp.model_dump.return_value = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }
        mock_client.chat.complete.return_value = resp

        before = app_state.ai.mistral_usage_stats()
        ai_service.call_mistral_chat(
            "key-usage-1", [{"role": "user", "content": "hi"}], use_cache=False
        )
        after = app_state.ai.mistral_usage_stats()
        self.assertEqual(after["call_count"] - before["call_count"], 1)
        self.assertEqual(after["prompt_tokens"] - before["prompt_tokens"], 10)
        self.assertEqual(after["completion_tokens"] - before["completion_tokens"], 20)


class TrimHistoryBudgetTestCase(unittest.TestCase):
    """B-3: chat history is trimmed to the character budget, newest first."""

    def test_drops_oldest_turns(self):
        from routes.api_analysis import _trim_history_to_budget

        messages = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "A" * 100},
            {"role": "assistant", "content": "B" * 100},
            {"role": "user", "content": "C" * 100},
        ]
        # Budget 210: system(3) + C(100) + B(100) fits; A does not.
        trimmed = _trim_history_to_budget(messages, max_chars=210)
        self.assertEqual(trimmed[0]["role"], "system")
        self.assertEqual(len(trimmed), 3)
        self.assertEqual(trimmed[-1]["content"], "C" * 100)

        # Tighter budget 150: only the newest turn survives after system.
        trimmed_tight = _trim_history_to_budget(messages, max_chars=150)
        self.assertEqual(len(trimmed_tight), 2)
        self.assertEqual(trimmed_tight[-1]["content"], "C" * 100)

    def test_within_budget_unchanged(self):
        from routes.api_analysis import _trim_history_to_budget

        messages = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "hi"},
        ]
        self.assertEqual(_trim_history_to_budget(messages, max_chars=1000), messages)

    def test_over_budget_keeps_newest_message_truncated(self):
        """R4: the newest turn must survive even when it alone exceeds the budget."""
        from routes.api_analysis import _trim_history_to_budget

        messages = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "A" * 500},
            {"role": "user", "content": "B" * 2000},
        ]
        # Budget 600: system(3) + newest user message truncated to 597 chars.
        trimmed = _trim_history_to_budget(messages, max_chars=600)
        self.assertEqual(len(trimmed), 2)
        self.assertEqual(trimmed[0]["role"], "system")
        self.assertEqual(trimmed[1]["role"], "user")
        self.assertEqual(len(trimmed[1]["content"]), 597)
        self.assertTrue(trimmed[1]["content"].startswith("B"))

    def test_system_over_budget_still_keeps_newest_slice(self):
        """R4: even with a system message over the budget, a non-empty slice of
        the newest user message is retained."""
        from routes.api_analysis import _trim_history_to_budget

        messages = [
            {"role": "system", "content": "S" * 5000},
            {"role": "user", "content": "Q" * 100},
        ]
        trimmed = _trim_history_to_budget(messages, max_chars=100)
        self.assertEqual(len(trimmed), 2)
        self.assertEqual(trimmed[1]["role"], "user")
        self.assertEqual(len(trimmed[1]["content"]), 1)


class MistralBaseUrlNormalizationTestCase(unittest.TestCase):
    """D-4: MISTRAL_BASE_URL must not end in /v1 (SDK v2 appends it itself)."""

    def test_normalize_strips_trailing_v1(self):
        from constants import _normalize_mistral_base_url

        self.assertEqual(
            _normalize_mistral_base_url("https://api.mistral.ai/v1"),
            "https://api.mistral.ai",
        )
        self.assertEqual(
            _normalize_mistral_base_url("https://api.mistral.ai/v1/"),
            "https://api.mistral.ai",
        )
        self.assertEqual(
            _normalize_mistral_base_url("https://api.mistral.ai"),
            "https://api.mistral.ai",
        )
        self.assertEqual(
            _normalize_mistral_base_url("https://proxy.example.com/mistral"),
            "https://proxy.example.com/mistral",
        )
        self.assertEqual(_normalize_mistral_base_url(""), "https://api.mistral.ai")

    def test_default_base_url_has_no_v1_suffix(self):
        """The app default must produce single /v1/chat/completions with SDK v2."""
        from constants import MISTRAL_BASE_URL

        self.assertFalse(MISTRAL_BASE_URL.rstrip("/").endswith("/v1"))

    def test_sdk_v2_builds_correct_url_with_app_default(self):
        """End-to-end: the SDK must hit ``<base>/v1/chat/completions`` exactly
        once (no /v1/v1 duplication -> the 404 "no Route matched").
        Config-agnostic: it derives the expected URL from the actual
        ``MISTRAL_BASE_URL`` in effect (env or default), so the test passes
        regardless of ``MNS_MISTRAL_BASE_URL``."""
        import importlib.util

        if importlib.util.find_spec("mistralai") is None:
            self.skipTest("mistralai SDK not installed")

        import httpx
        from mistralai.client import Mistral

        from constants import MISTRAL_BASE_URL

        captured = []

        def handler(request):
            captured.append(str(request.url))
            return httpx.Response(
                200,
                json={
                    "id": "x",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "m",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "hi"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

        transport = httpx.MockTransport(handler)
        client = Mistral(
            api_key="test",
            server_url=MISTRAL_BASE_URL,
            client=httpx.Client(transport=transport),
        )
        client.chat.complete(
            model="mistral-small-latest", messages=[{"role": "user", "content": "hello"}]
        )
        # The SDK appends the versioned path itself; the base URL must not end
        # in /v1, otherwise the request would hit /v1/v1/chat/completions.
        self.assertEqual(captured, [MISTRAL_BASE_URL.rstrip("/") + "/v1/chat/completions"])
        self.assertNotIn("/v1/v1", captured[0])


class NormalizeChatParsePayloadTestCase(unittest.TestCase):
    """D-3: structured payload extraction helper shapes."""

    def test_dict_content(self):
        from utils.validators import normalize_chat_parse_payload

        resp = {"choices": [{"message": {"content": {"a": 1}}}]}
        self.assertEqual(normalize_chat_parse_payload(resp), {"a": 1})

    def test_string_json_content(self):
        from utils.validators import normalize_chat_parse_payload

        resp = {"choices": [{"message": {"content": '{"a": 1}'}}]}
        self.assertEqual(normalize_chat_parse_payload(resp), {"a": 1})

    def test_parsed_object(self):
        from utils.validators import normalize_chat_parse_payload

        class FakeParsed:
            def model_dump(self):
                return {"a": 2}

        resp = {"choices": [{"message": {"parsed": FakeParsed()}}]}
        self.assertEqual(normalize_chat_parse_payload(resp), {"a": 2})

    def test_error_returns_none(self):
        from utils.validators import normalize_chat_parse_payload

        self.assertIsNone(normalize_chat_parse_payload({"error": {"message": "boom"}}))

    def test_plain_text_returns_none(self):
        from utils.validators import normalize_chat_parse_payload

        resp = {"choices": [{"message": {"content": "plain text"}}]}
        self.assertIsNone(normalize_chat_parse_payload(resp))

    def test_object_style_response(self):
        from utils.validators import normalize_chat_parse_payload

        class FakeMessage:
            def __init__(self):
                self.content = {"x": 9}

        class FakeChoice:
            def __init__(self):
                self.message = FakeMessage()

        class FakeResponse:
            def __init__(self):
                self.choices = [FakeChoice()]

        self.assertEqual(normalize_chat_parse_payload(FakeResponse()), {"x": 9})


class StreamMistralChatTestCase(unittest.TestCase):
    """C-2: stream event extraction and generator behavior."""

    def test_extract_stream_delta_dict_and_object(self):
        from services.ai_service import _extract_stream_delta

        self.assertEqual(_extract_stream_delta({"choices": [{"delta": {"content": "hi"}}]}), "hi")
        self.assertIsNone(_extract_stream_delta({"choices": [{"delta": {"content": ""}}]}))
        self.assertIsNone(_extract_stream_delta({}))

        class D:
            def __init__(self, text):
                self.content = text

        class C:
            def __init__(self, text):
                self.delta = D(text)

        class Chunk:
            def __init__(self, text):
                self.choices = [C(text)]

        self.assertEqual(_extract_stream_delta(Chunk("yo")), "yo")
        # SSE event wrapper: chunk.data

        class Wrapper:
            def __init__(self, text):
                self.data = Chunk(text)

        self.assertEqual(_extract_stream_delta(Wrapper("ya")), "ya")

    @patch("services.ai_service._get_mistral_model_name", return_value="mistral-small-2603")
    @patch("services.ai_service._get_mistral_client")
    def test_stream_yields_delta_and_done(self, mock_get_client, mock_get_name):
        from services import ai_service

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        class D:
            def __init__(self, text):
                self.content = text

        class C:
            def __init__(self, text):
                self.delta = D(text)

        class Chunk:
            def __init__(self, text):
                self.choices = [C(text)]

        mock_client.chat.stream.return_value = iter([Chunk("hel"), Chunk("lo")])

        events = list(
            ai_service.stream_mistral_chat(
                "key-stream-1", [{"role": "user", "content": "hi"}], temperature=0.7
            )
        )
        self.assertEqual(events[0]["type"], "delta")
        self.assertEqual(events[0]["text"], "hel")
        self.assertEqual(events[1]["type"], "delta")
        self.assertEqual(events[1]["text"], "lo")
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["text"], "hello")

        _, kwargs = mock_client.chat.stream.call_args
        self.assertIn("retries", kwargs)
        self.assertEqual(kwargs["temperature"], 0.7)

    @patch("services.ai_service._get_mistral_model_name", return_value="mistral-small-2603")
    @patch("services.ai_service._get_mistral_client")
    def test_stream_closes_sdk_stream_after_consumer_finishes(self, mock_get_client, mock_get_name):
        from services import ai_service

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        class ClosableStream:
            def __init__(self):
                self.closed = False

            def __iter__(self):
                return iter([])

            def close(self):
                self.closed = True

        sdk_stream = ClosableStream()
        mock_client.chat.stream.return_value = sdk_stream

        events = list(
            ai_service.stream_mistral_chat("key-stream-close", [{"role": "user", "content": "hi"}])
        )

        self.assertEqual(events[-1]["type"], "done")
        self.assertTrue(sdk_stream.closed)

    @patch("services.ai_service._get_mistral_model_name", return_value="mistral-small-2603")
    @patch("services.ai_service._get_mistral_client")
    def test_stream_closes_sdk_stream_when_consumer_disconnects(
        self, mock_get_client, mock_get_name
    ):
        from services import ai_service

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        class ClosableStream:
            def __init__(self):
                self.closed = False

            def __iter__(self):
                yield {"choices": [{"delta": {"content": "part"}}]}

            def close(self):
                self.closed = True

        sdk_stream = ClosableStream()
        mock_client.chat.stream.return_value = sdk_stream

        generator = ai_service.stream_mistral_chat(
            "key-stream-disconnect", [{"role": "user", "content": "hi"}]
        )
        self.assertEqual(next(generator), {"type": "delta", "text": "part"})
        generator.close()

        self.assertTrue(sdk_stream.closed)

    @patch("services.ai_service._get_mistral_model_name", return_value="mistral-small-2603")
    @patch("services.ai_service._get_mistral_client")
    def test_stream_error_event_on_failure(self, mock_get_client, mock_get_name):
        from services import ai_service

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.chat.stream.side_effect = _make_sdk_error("upstream down", status_code=503)
        events = list(
            ai_service.stream_mistral_chat("key-stream-2", [{"role": "user", "content": "hi"}])
        )
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("upstream down", events[-1]["message"])


class ChatStreamRouteTestCase(unittest.TestCase):
    """C-2: /api/chat with stream:true returns an SSE response."""

    def setUp(self):
        from app import app

        self.app = app
        self.app.config["TESTING"] = True
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.client = self.app.test_client()
        with app_state.ai.mistral_response_lock:
            app_state.ai.mistral_response_cache.clear()

    @patch("routes.api_analysis.stream_mistral_chat")
    @patch("routes.api_analysis.get_stock_info_cached", return_value={})
    def test_stream_flag_returns_sse(self, mock_info, mock_stream):
        def fake_stream(api_key, messages, max_tokens, temperature):
            yield {"type": "delta", "text": "Hello"}
            yield {"type": "delta", "text": " world"}
            yield {"type": "done", "text": "Hello world"}

        mock_stream.side_effect = fake_stream

        resp = self.client.post(
            "/api/chat",
            json={
                "symbol": "AAPL",
                "market": "us",
                "message": "test",
                "request_token": "test-stream-token-0001",
                "stream": True,
            },
            headers={
                "Origin": "http://localhost:5000",
                "Authorization": "Bearer dummy-key",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/event-stream", resp.content_type)
        body = resp.get_data(as_text=True)
        self.assertIn('"delta"', body)
        self.assertIn("Hello world", body)

        # R3: the stream slot must be fully released after a successful stream.
        from routes import api_analysis as ra

        self.assertTrue(ra.stream_chat_slots.acquire(blocking=False))
        self.assertTrue(ra.stream_chat_slots.acquire(blocking=False))
        ra.stream_chat_slots.release()
        ra.stream_chat_slots.release()

    @patch("routes.api_analysis.stream_mistral_chat")
    @patch("routes.api_analysis.get_stock_info_cached", return_value={})
    def test_stream_error_is_sanitized_and_recorded(self, mock_info, mock_stream):
        """R5: stream failures must not leak raw SDK text and must be cached as errors."""

        def fake_stream(api_key, messages, max_tokens, temperature):
            yield {"type": "error", "message": "Status 500: internal boom", "status_code": 500}

        mock_stream.side_effect = fake_stream

        resp = self.client.post(
            "/api/chat",
            json={
                "symbol": "AAPL",
                "market": "us",
                "message": "test",
                "request_token": "test-stream-token-0002",
                "stream": True,
            },
            headers={
                "Origin": "http://localhost:5000",
                "Authorization": "Bearer dummy-key",
            },
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn('"error"', body)
        self.assertNotIn("internal boom", body)

        # Re-poll (non-streaming) must surface the recorded error instead of the
        # generic fallback reply.
        poll = self.client.post(
            "/api/chat",
            json={
                "symbol": "AAPL",
                "market": "us",
                "message": "test",
                "request_token": "test-stream-token-0002",
            },
            headers={
                "Origin": "http://localhost:5000",
                "Authorization": "Bearer dummy-key",
            },
        )
        self.assertEqual(poll.status_code, 500)

    @patch("routes.api_analysis.stream_mistral_chat")
    @patch("routes.api_analysis.get_stock_info_cached", return_value={})
    def test_stream_concurrency_cap_returns_503(self, mock_info, mock_stream):
        """R3: exceeding the concurrent-stream cap must return 503 like queue-Full."""
        from routes import api_analysis as ra

        with ra.stream_chat_slots:
            with ra.stream_chat_slots:
                resp = self.client.post(
                    "/api/chat",
                    json={
                        "symbol": "AAPL",
                        "market": "us",
                        "message": "test",
                        "request_token": "test-stream-token-0003",
                        "stream": True,
                    },
                    headers={
                        "Origin": "http://localhost:5000",
                        "Authorization": "Bearer dummy-key",
                    },
                )
                self.assertEqual(resp.status_code, 503)
                data = resp.get_json()
                self.assertIn("上限", data.get("details", {}).get("reason", ""))

    def test_metrics_exposes_mistral_usage(self):
        """C-4: /api/metrics exposes the Mistral usage counters."""
        resp = self.client.get("/api/metrics")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("mistral", data)
        self.assertIn("call_count", data["mistral"])
        self.assertIn("total_tokens", data["mistral"])


if __name__ == "__main__":
    unittest.main()
