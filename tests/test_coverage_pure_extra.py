"""Coverage tests for small pure modules: mistral_compat, execution_state, ai_state, env_helpers, http_utils, text_utils, wsgi worker guard."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import mistral_compat
import execution_state
import ai_state
import utils.env_helpers as env_helpers
import utils.http_utils as http_utils
import utils.text_utils as text_utils


class MistralCompatTestCase(unittest.TestCase):
    def test_message_helpers(self):
        self.assertEqual(mistral_compat.SystemMessage("a"), {"role": "system", "content": "a"})
        self.assertEqual(mistral_compat.UserMessage("b"), {"role": "user", "content": "b"})
        self.assertEqual(mistral_compat.AssistantMessage("c"), {"role": "assistant", "content": "c"})

    def test_mistral_client_resolves(self):
        # mistralai is installed in the environment; the real client must resolve
        self.assertTrue(hasattr(mistral_compat, "Mistral"))
        self.assertTrue(hasattr(mistral_compat, "SDKError"))


class ExecutionStateTestCase(unittest.TestCase):
    def test_shutdown_with_type_error_fallback(self):
        es = execution_state.ExecutionState()
        bad_exec = MagicMock()
        # Old Python (<=3.8) rejects cancel_futures; real code falls back to
        # shutdown(wait=False). Simulate that with a side_effect keyed on args.
        def _shutdown(*args, **kwargs):
            if kwargs.get("cancel_futures"):
                raise TypeError("boom")
            return None
        bad_exec.shutdown.side_effect = _shutdown
        es.executor = bad_exec
        es.news_executor = MagicMock()
        es.sync_refresh_executor = MagicMock()
        es.shutdown()  # must not raise
        self.assertEqual(bad_exec.shutdown.call_count, 2)

    def test_shutdown_thread_join_error_swallowed(self):
        es = execution_state.ExecutionState()
        t = MagicMock()
        t.is_alive.return_value = True
        t.join.side_effect = RuntimeError("dead")
        es.background_threads = [t]
        es.shutdown()  # must not raise
        t.join.assert_called_once()


class AIStateTestCase(unittest.TestCase):
    def test_init_and_add_history(self):
        st = ai_state.AIState()
        st.add_chat_history("k", [{"role": "user", "content": "hi"}])
        self.assertEqual(st.chat_history["k"], [{"role": "user", "content": "hi"}])

    def test_mark_mistral_429_with_valid_retry(self):
        st = ai_state.AIState()
        backoff = st.mark_mistral_429(retry_after_sec=10)
        self.assertGreater(backoff, 0)
        self.assertEqual(st.mistral_429_streak, 1)

    def test_mark_mistral_429_invalid_retry(self):
        st = ai_state.AIState()
        backoff = st.mark_mistral_429(retry_after_sec="not-a-number")
        self.assertGreater(backoff, 0)

    def test_reset_mistral_streak(self):
        st = ai_state.AIState()
        st.mistral_429_streak = 3
        st.mistral_next_allowed_ts = 123.0
        st.reset_mistral_streak()
        self.assertEqual(st.mistral_429_streak, 0)
        self.assertEqual(st.mistral_next_allowed_ts, 0.0)

    def test_get_or_create_mistral_client_caches(self):
        st = ai_state.AIState()
        c1 = st.get_or_create_mistral_client("key123")
        c2 = st.get_or_create_mistral_client("key123")
        self.assertIs(c1, c2)


class EnvHelpersTestCase(unittest.TestCase):
    def test_env_int_default_and_bounds(self):
        self.assertEqual(env_helpers._env_int("NOT_SET_X", 7), 7)
        with patch.dict("os.environ", {"MNS_TEST_INT": "abc"}, clear=False):
            self.assertEqual(env_helpers._env_int("MNS_TEST_INT", 5, 1, 10), 5)
        with patch.dict("os.environ", {"MNS_TEST_INT": "50"}, clear=False):
            self.assertEqual(env_helpers._env_int("MNS_TEST_INT", 5, 1, 10), 10)
        with patch.dict("os.environ", {"MNS_TEST_INT": "-5"}, clear=False):
            self.assertEqual(env_helpers._env_int("MNS_TEST_INT", 5, 1, 10), 1)

    def test_env_float_default_and_bounds(self):
        self.assertEqual(env_helpers._env_float("NOT_SET_Y", 2.5), 2.5)
        with patch.dict("os.environ", {"MNS_TEST_FLOAT": "xyz"}, clear=False):
            self.assertEqual(env_helpers._env_float("MNS_TEST_FLOAT", 1.0, 0.0, 5.0), 1.0)
        with patch.dict("os.environ", {"MNS_TEST_FLOAT": "99"}, clear=False):
            self.assertEqual(env_helpers._env_float("MNS_TEST_FLOAT", 1.0, 0.0, 5.0), 5.0)

    def test_is_production_env(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(env_helpers._is_production_env())
        with patch.dict("os.environ", {"MNS_PROD": "1"}, clear=True):
            self.assertTrue(env_helpers._is_production_env())
        with patch.dict("os.environ", {"MNS_COOKIE_SECURE": "true"}, clear=True):
            self.assertTrue(env_helpers._is_production_env())


class HttpUtilsTestCase(unittest.TestCase):
    def test_none_response(self):
        self.assertIsNone(http_utils.parse_retry_after(None))

    def test_dict_headers(self):
        self.assertEqual(http_utils.parse_retry_after(SimpleNamespace(headers={"Retry-After": "30"})), 30.0)
        self.assertIsNone(http_utils.parse_retry_after({"headers": {}}))

    def test_case_insensitive_headers(self):
        class CI:
            def get(self, k, default=None):
                return {"retry-after": "5"}.get(k.lower(), default)
        self.assertEqual(http_utils.parse_retry_after(SimpleNamespace(headers=CI())), 5.0)

    def test_http_date_header(self):
        from email.utils import formatdate
        when = formatdate(timeval=1000000, usegmt=True)
        with patch("utils.http_utils.time.time", return_value=900000.0):
            val = http_utils.parse_retry_after(SimpleNamespace(headers={"Retry-After": when}))
            self.assertGreaterEqual(val, 0.0)

    def test_invalid_http_date(self):
        self.assertIsNone(http_utils.parse_retry_after(SimpleNamespace(headers={"Retry-After": "garbage"})))

    def test_response_via_exception_attr(self):
        resp = SimpleNamespace(headers={"Retry-After": "2"})
        exc = SimpleNamespace(response=resp)
        self.assertEqual(http_utils.parse_retry_after(exc), 2.0)

    def test_exception_with_broken_response_attr(self):
        exc = SimpleNamespace(response=SimpleNamespace())  # no headers
        # attribute access for response should raise -> caught -> None
        self.assertIsNone(http_utils.parse_retry_after(exc))


class TextUtilsTestCase(unittest.TestCase):
    def test_short_text_strips_control(self):
        self.assertEqual(text_utils._short_text("  hello  "), "hello")
        self.assertEqual(text_utils._short_text("a\tb\nc"), "abc")
        long = "x" * 200
        self.assertTrue(text_utils._short_text(long).endswith("..."))

    def test_token_fingerprint_and_mask(self):
        self.assertEqual(text_utils._token_fingerprint(""), "none")
        fp = text_utils._token_fingerprint("secret")
        self.assertTrue(fp.startswith("sha256="))
        self.assertEqual(text_utils._token_mask(""), "none")
        self.assertEqual(text_utils._token_mask("ab"), "**")
        self.assertEqual(text_utils._token_mask("abcdef"), "ab...ef")

    def test_is_valid_api_key(self):
        self.assertFalse(text_utils._is_valid_api_key(None))
        self.assertFalse(text_utils._is_valid_api_key("short"))
        self.assertFalse(text_utils._is_valid_api_key("has space"))
        self.assertTrue(text_utils._is_valid_api_key("validkey12"))

    def test_sanitize_error_message(self):
        self.assertEqual(text_utils._sanitize_error_message(""), "")
        dirty = "api_key=abc12345 token=xyz secret=pass"
        sanitized = text_utils._sanitize_error_message(dirty)
        self.assertNotIn("abc12345", sanitized)
        self.assertIn("[REDACTED]", sanitized)
        self.assertIn("[REDACTED]", text_utils._sanitize_error_message("bearer aaaaaaaaaaaaaaaaaaaa"))

    def test_parse_non_negative_float(self):
        self.assertEqual(text_utils.parse_non_negative_float(5, "x"), 5.0)
        self.assertEqual(text_utils.parse_non_negative_float("3.5", "x"), 3.5)
        with self.assertRaises(ValueError):
            text_utils.parse_non_negative_float(True, "x")
        with self.assertRaises(ValueError):
            text_utils.parse_non_negative_float("bad", "x")
        with self.assertRaises(ValueError):
            text_utils.parse_non_negative_float(float("nan"), "x")
        with self.assertRaises(ValueError):
            text_utils.parse_non_negative_float(-1, "x")
        with self.assertRaises(ValueError):
            text_utils.parse_non_negative_float(100, "x", max_value=10)


if __name__ == "__main__":
    unittest.main()
