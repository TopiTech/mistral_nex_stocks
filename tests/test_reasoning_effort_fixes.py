"""
tests/test_reasoning_effort_fixes.py - Regression tests for Mistral reasoning_effort,
stream delta extraction, and unread HTTP error handling.
"""

import json
import os
import unittest
from unittest.mock import MagicMock, patch

import httpx

from services.ai_service import (
    _extract_error_payload,
    _extract_mistral_wait_seconds,
    _extract_stream_delta,
    _resolve_reasoning_effort,
    stream_mistral_chat,
)
from utils.validators import _clean_reasoning_tags


class TestReasoningEffortResolution(unittest.TestCase):
    """Test _resolve_reasoning_effort normalization for Mistral server API."""

    def test_default_is_none(self):
        with patch.dict(os.environ, {"MNS_MISTRAL_REASONING_EFFORT": ""}):
            self.assertEqual(_resolve_reasoning_effort("mistral-small-2603"), "none")
            self.assertEqual(_resolve_reasoning_effort("mistral-medium-2604"), "none")

    def test_non_reasoning_model_returns_none(self):
        self.assertIsNone(_resolve_reasoning_effort("mistral-large-latest"))
        self.assertIsNone(_resolve_reasoning_effort("codestral-latest"))
        self.assertIsNone(_resolve_reasoning_effort(""))

    def test_normalize_values(self):
        # High-effort mappings
        self.assertEqual(_resolve_reasoning_effort("mistral-small-2603", "high"), "high")
        self.assertEqual(_resolve_reasoning_effort("mistral-small-2603", "medium"), "high")
        self.assertEqual(_resolve_reasoning_effort("mistral-small-2603", "xhigh"), "high")

        # None-effort mappings
        self.assertEqual(_resolve_reasoning_effort("mistral-small-2603", "none"), "none")
        self.assertEqual(_resolve_reasoning_effort("mistral-small-2603", "low"), "none")
        self.assertEqual(_resolve_reasoning_effort("mistral-small-2603", "minimal"), "none")
        self.assertEqual(_resolve_reasoning_effort("mistral-small-2603", "off"), "none")

    def test_env_var_override(self):
        with patch.dict(os.environ, {"MNS_MISTRAL_REASONING_EFFORT": "high"}):
            self.assertEqual(_resolve_reasoning_effort("mistral-small-2603"), "high")

        with patch.dict(os.environ, {"MNS_MISTRAL_REASONING_EFFORT": "medium"}):
            self.assertEqual(_resolve_reasoning_effort("mistral-small-2603"), "high")

        with patch.dict(os.environ, {"MNS_MISTRAL_REASONING_EFFORT": "none"}):
            self.assertEqual(_resolve_reasoning_effort("mistral-small-2603"), "none")


class TestErrorPayloadHandling(unittest.TestCase):
    """Test that _extract_error_payload safely reads unread streaming httpx.Response."""

    def test_unread_httpx_streaming_response(self):
        """Verify _extract_error_payload calls .read() or catches ResponseNotRead gracefully."""
        # Create an httpx Response that simulates a stream without read() called
        request = httpx.Request("POST", "https://api.mistral.ai/v1/chat/completions")
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.read.side_effect = None
        mock_response.json.return_value = {
            "object": "error",
            "message": "reasoning_effort='medium' is not supported for this model.",
        }
        mock_response.text = '{"object": "error", "message": "reasoning_effort=\'medium\' is not supported for this model."}'

        exc = httpx.HTTPStatusError("400 Bad Request", request=request, response=mock_response)
        payload = _extract_error_payload(exc)

        self.assertIsNotNone(payload)
        self.assertIn("reasoning_effort", payload.get("message", ""))
        mock_response.read.assert_called_once()

    def test_httpx_response_not_read_exception_does_not_crash(self):
        """Verify that even if .read() or .json() throws ResponseNotRead, extraction doesn't crash."""
        request = httpx.Request("POST", "https://api.mistral.ai/v1/chat/completions")
        mock_response = MagicMock()
        mock_response.read.side_effect = httpx.ResponseNotRead()
        mock_response.json.side_effect = httpx.ResponseNotRead()
        type(mock_response).text = property(lambda s: (_ for _ in ()).throw(httpx.ResponseNotRead()))

        exc = httpx.HTTPStatusError("400 Bad Request", request=request, response=mock_response)
        # Must not raise an unhandled exception
        payload = _extract_error_payload(exc)
        self.assertIsNone(payload)

    def test_wait_seconds_extraction_safety(self):
        """Verify _extract_mistral_wait_seconds never raises on malformed or unread responses."""
        self.assertEqual(_extract_mistral_wait_seconds(None), 0.0)
        self.assertEqual(_extract_mistral_wait_seconds({}), 0.0)
        self.assertEqual(_extract_mistral_wait_seconds({"headers": {"retry-after": "5"}}), 5.0)


class TestStreamDeltaHygiene(unittest.TestCase):
    """Test that _extract_stream_delta keeps reasoning thoughts out of user visible stream."""

    def test_plain_content_streamed(self):
        chunk = {"choices": [{"delta": {"content": "Hello SoftBank"}}]}
        self.assertEqual(_extract_stream_delta(chunk), "Hello SoftBank")

    def test_reasoning_content_filtered_by_default(self):
        chunk = {"choices": [{"delta": {"content": None, "reasoning_content": "Internal chain of thought..."}}]}
        self.assertIsNone(_extract_stream_delta(chunk))
        self.assertEqual(_extract_stream_delta(chunk, include_thinking=True), "Internal chain of thought...")

    def test_mixed_content_extracts_only_visible_text(self):
        chunk = {"choices": [{"delta": {"content": "Visible answer", "reasoning_content": "Internal thought"}}]}
        self.assertEqual(_extract_stream_delta(chunk), "Visible answer")


class TestCleanReasoningTags(unittest.TestCase):
    """Test _clean_reasoning_tags cleans all thinking tag formats."""

    def test_xml_thought_tags(self):
        text = "<thought>Thinking about SoftBank price...</thought>ソフトバンクの目標株価は..."
        self.assertEqual(_clean_reasoning_tags(text), "ソフトバンクの目標株価は...")

    def test_xml_thinking_tags(self):
        text = "<thinking>\nStep 1: Check PE ratio\nStep 2: Check revenue\n</thinking>\nPEレシオは割安です。"
        self.assertEqual(_clean_reasoning_tags(text), "PEレシオは割安です。")

    def test_bracket_think_tags(self):
        text = "[THINK]Internal model monologue[/THINK]最新の決算発表によれば..."
        self.assertEqual(_clean_reasoning_tags(text), "最新の決算発表によれば...")

    def test_preserve_for_history(self):
        text = "<thought>Keep me</thought>Visible"
        self.assertEqual(_clean_reasoning_tags(text, preserve_for_history=True), text)


class TestStream400RetryWithoutReasoningEffort(unittest.TestCase):
    """Test stream_mistral_chat auto-retries when 400 reasoning_effort error occurs."""

    @patch("services.ai_service._get_mistral_model_name", return_value="mistral-small-2603")
    @patch("services.ai_service._get_mistral_client")
    def test_stream_auto_retries_400_reasoning_error(self, mock_get_client, mock_get_name):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        call_count = [0]

        def mock_stream(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call with reasoning_effort fails with 400 Bad Request
                mock_resp = MagicMock()
                mock_resp.status_code = 400
                mock_resp.json.return_value = {
                    "object": "error",
                    "message": "reasoning_effort='medium' is not supported for this model. Must be one of (<ReasoningEffort.none: 'none'>, <ReasoningEffort.high: 'high'>)",
                }
                mock_resp.text = json.dumps(mock_resp.json.return_value)
                raise httpx.HTTPStatusError("400 Bad Request", request=MagicMock(), response=mock_resp)
            else:
                # Second retry call succeeds
                chunk = {"choices": [{"delta": {"content": "Success on retry without reasoning_effort"}}]}
                return iter([chunk])

        mock_client.chat.stream.side_effect = mock_stream

        events = list(
            stream_mistral_chat(
                api_key="test-key",
                messages=[{"role": "user", "content": "9434.Tの分析"}],
                reasoning_effort="high",
            )
        )

        self.assertEqual(call_count[0], 2)
        delta_texts = [e["text"] for e in events if e.get("type") == "delta"]
        self.assertIn("Success on retry without reasoning_effort", delta_texts)
