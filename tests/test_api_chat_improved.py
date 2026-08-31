"""Unit tests for the improved api_chat caching and deduplication in routes/api_analysis.py.

Each test uses a distinct symbol name so chat history from one test never
contaminates another — even when the SQLite chat-history database is shared
across the whole test session (the conftest.py database path is fixed for the
process lifetime).
"""

import json
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from app_state import app_state
from tests.test_api_integration import APIIntegrationTestCase


class APIChatImprovedTestCase(APIIntegrationTestCase):
    """Test improvements to the /api/chat endpoint."""

    def setUp(self):
        super().setUp()
        # Mock get_stock_info_cached to return immediately without yfinance network lookups
        self._stock_info_patcher = patch(
            "routes.api_analysis.get_stock_info_cached",
            return_value={"regularMarketPreviousClose": 150.0},
        )
        self._stock_info_mock = self._stock_info_patcher.start()

        # Reset chat history for test run
        with app_state.ai.chat_history_lock:
            app_state.ai.chat_history.clear()

        # Reset caches
        from routes.api_analysis import chat_fetch_inflight, chat_result_cache

        chat_result_cache.clear()
        chat_fetch_inflight.clear()

    def tearDown(self):
        self._stock_info_patcher.stop()
        super().tearDown()
        # Close the thread-local SQLite connection to prevent ResourceWarning
        app_state.ai.chat_history.close()

    @staticmethod
    def _make_chat_response(reply_text: str):
        return {"choices": [{"message": {"content": reply_text}}]}

    def _chat_key(self, market, symbol):
        with self.client.session_transaction() as flask_session:
            scope = flask_session["mns_analysis_conversation"]
        return f"{scope}:{market}:{symbol}"

    def _make_stream_response(self, suffix):
        """Build an isolated stream response with an already-appended user turn."""
        from routes import api_analysis as ra

        chat_key = f"stream-history-{suffix}"
        inflight_key = f"chat:stream-{suffix}"
        user_message = "Will this turn be rolled back?"
        result_holder = {"result": None, "error": None, "done": threading.Event()}
        slot = threading.BoundedSemaphore(1)
        self.assertTrue(slot.acquire(blocking=False))
        release_once = ra._ReleaseOnce(slot)

        with ra.chat_fetch_lock:
            ra.chat_fetch_inflight[inflight_key] = result_holder
        with app_state.ai.chat_history_lock:
            app_state.ai.chat_history[chat_key] = [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": user_message},
            ]

        rollback = MagicMock(
            side_effect=lambda: ra._rollback_chat_user_message(chat_key, user_message)
        )
        with self.app.test_request_context("/api/chat"):
            response = ra._stream_chat_response(
                "test-key",
                [],
                "stream-request-token",
                chat_key,
                inflight_key,
                result_holder,
                release_once,
                on_abort=rollback,
            )
        return response, chat_key, inflight_key, result_holder, rollback, slot

    @staticmethod
    def _assert_stream_slot_released(slot):
        assert slot.acquire(blocking=False), "stream semaphore was not released"
        slot.release()

    def test_unstarted_stream_close_finalizes_state_and_rolls_back_user_turn(self):
        """R1: closing before iteration must not leak inflight state or a user turn."""
        from routes import api_analysis as ra

        response, chat_key, inflight_key, result_holder, rollback, slot = self._make_stream_response(
            "unstarted"
        )
        try:
            response.close()
            response.close()  # Flask may invoke close more than once; cleanup remains idempotent.

            self.assertNotIn(inflight_key, ra.chat_fetch_inflight)
            self.assertTrue(result_holder["done"].is_set())
            self.assertIsNone(result_holder["result"])
            self.assertIsInstance(result_holder["error"], ra._ChatStreamAbortedError)
            cached_result, cached_error = ra.chat_result_cache[inflight_key][1:]
            self.assertIsNone(cached_result)
            self.assertIsInstance(cached_error, ra._ChatStreamAbortedError)
            rollback.assert_called_once_with()
            self._assert_stream_slot_released(slot)
            with app_state.ai.chat_history_lock:
                self.assertEqual(app_state.ai.chat_history[chat_key], [{"role": "system", "content": "system prompt"}])
        finally:
            response.close()

    @patch("routes.api_analysis.stream_mistral_chat")
    def test_partial_stream_close_discards_partial_reply_and_rolls_back_user_turn(self, mock_stream):
        """R1: a disconnect after a delta must not be cached or persisted as success."""
        from routes import api_analysis as ra

        mock_stream.return_value = iter([{"type": "delta", "text": "partial reply"}])
        response, chat_key, inflight_key, result_holder, rollback, slot = self._make_stream_response(
            "partial"
        )
        try:
            self.assertIn("partial reply", next(response.response))
            response.close()

            self.assertNotIn(inflight_key, ra.chat_fetch_inflight)
            self.assertTrue(result_holder["done"].is_set())
            self.assertIsNone(result_holder["result"])
            self.assertIsInstance(result_holder["error"], ra._ChatStreamAbortedError)
            cached_result, cached_error = ra.chat_result_cache[inflight_key][1:]
            self.assertIsNone(cached_result)
            self.assertIsInstance(cached_error, ra._ChatStreamAbortedError)
            rollback.assert_called_once_with()
            self._assert_stream_slot_released(slot)
            with app_state.ai.chat_history_lock:
                self.assertEqual(app_state.ai.chat_history[chat_key], [{"role": "system", "content": "system prompt"}])
        finally:
            response.close()

    @patch("routes.api_analysis.stream_mistral_chat")
    def test_done_stream_close_keeps_completed_reply(self, mock_stream):
        """R1: closing after `done` must preserve the completed stream result."""
        from routes import api_analysis as ra

        mock_stream.return_value = iter(
            [
                {"type": "delta", "text": "partial reply"},
                {"type": "done", "text": "completed reply"},
            ]
        )
        response, chat_key, inflight_key, result_holder, rollback, slot = self._make_stream_response(
            "done"
        )
        try:
            self.assertIn("partial reply", next(response.response))
            self.assertIn("completed reply", next(response.response))
            response.close()

            self.assertNotIn(inflight_key, ra.chat_fetch_inflight)
            self.assertTrue(result_holder["done"].is_set())
            self.assertEqual(result_holder["result"], "completed reply")
            self.assertIsNone(result_holder["error"])
            cached_result, cached_error = ra.chat_result_cache[inflight_key][1:]
            self.assertEqual(cached_result, "completed reply")
            self.assertIsNone(cached_error)
            rollback.assert_not_called()
            self._assert_stream_slot_released(slot)
            with app_state.ai.chat_history_lock:
                self.assertEqual(
                    app_state.ai.chat_history[chat_key],
                    [
                        {"role": "system", "content": "system prompt"},
                        {"role": "user", "content": "Will this turn be rolled back?"},
                        {"role": "assistant", "content": "completed reply"},
                    ],
                )
        finally:
            response.close()

    @patch("routes.api_analysis._call_mistral_chat_with_retry")
    def test_api_chat_basic_success(self, mock_chat):
        """Should succeed in generating a chat response and updating history."""
        mock_chat.return_value = "Mocked AI Response"
        test_symbol = "AAPL_BASIC"

        # Mock API credentials to bypass checks
        with patch("routes.api_analysis.extract_api_key", return_value="test-key-32-chars"):
            response = self.client.post(
                "/api/chat",
                json={
                    "market": "us",
                    "symbol": test_symbol,
                    "message": "What is the stock price?",
                    "request_token": "basic-request-001",
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertEqual(data.get("reply"), "Mocked AI Response")
        chat_key = self._chat_key("us", test_symbol)

        # Verify chat history contains both user and assistant messages exactly once
        with app_state.ai.chat_history_lock:
            history = app_state.ai.chat_history[chat_key]

        # History format: system, user initial, assistant initial, user message, assistant reply
        user_msgs = [m for m in history if m["role"] == "user"]
        assistant_msgs = [m for m in history if m["role"] == "assistant"]

        # User messages: initial question and "What is the stock price?"
        self.assertEqual(len(user_msgs), 2)
        self.assertEqual(user_msgs[-1]["content"], "What is the stock price?")

        # Assistant messages: initial greeting and "Mocked AI Response"
        self.assertEqual(len(assistant_msgs), 2)
        self.assertEqual(assistant_msgs[-1]["content"], "Mocked AI Response")

    @patch("routes.api_analysis._call_mistral_chat_with_retry", return_value="Current price used")
    def test_api_chat_context_prefers_current_price_over_previous_close(self, mock_chat):
        """The prompt's latest price must not silently use the previous close."""
        self._stock_info_mock.return_value = {
            "regularMarketPrice": 155.0,
            "regularMarketPreviousClose": 150.0,
        }

        with patch("routes.api_analysis.extract_api_key", return_value="test-key-32-chars"):
            response = self.client.post(
                "/api/chat",
                json={
                    "market": "us",
                    "symbol": "AAPL_CURPRICE",
                    "message": "最新価格を教えて",
                    "request_token": "current-price-001",
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )

        self.assertEqual(response.status_code, 200)
        messages = mock_chat.call_args.args[1]
        fresh_context = next(
            message["content"]
            for message in messages
            if message.get("role") == "user" and "latest known price=" in message.get("content", "")
        )
        self.assertIn("latest known price=155.0", fresh_context)

    @patch("routes.api_analysis._call_mistral_chat_with_retry")
    def test_api_chat_polling_deduplication(self, mock_chat):
        """Should not duplicate user messages when in-flight polling occurs."""
        test_symbol = "AAPL_POLL"
        request_token = "poll-request-0001"
        # Setup a blocking event to control when the background job completes
        block_event = threading.Event()

        def slow_chat(*args, **kwargs):
            block_event.wait()
            return self._make_chat_response("Slow Response")

        mock_chat.side_effect = slow_chat

        from concurrent.futures import ThreadPoolExecutor

        real_executor = ThreadPoolExecutor(max_workers=1)
        original_executor = app_state.execution.executor
        app_state.execution.executor = real_executor

        try:
            with patch("routes.api_analysis.extract_api_key", return_value="test-key-32-chars"):
                # Use a very small wait time to force timeout quickly
                with patch("routes.api_analysis.CHAT_PREPARE_WAIT_SEC", 0.01):
                    # Send initial request (returns fetching: True)
                    response1 = self.client.post(
                        "/api/chat",
                        json={
                            "market": "us",
                            "symbol": test_symbol,
                            "message": "Hello AI",
                            "request_token": request_token,
                        },
                        environ_base={"REMOTE_ADDR": "127.0.0.1"},
                    )
                    self.assertEqual(response1.status_code, 200)
                    data1 = json.loads(response1.data)
                    self.assertTrue(data1.get("fetching"))

                    # Send second request representing client polling
                    response2 = self.client.post(
                        "/api/chat",
                        json={
                            "market": "us",
                            "symbol": test_symbol,
                            "message": "Hello AI",
                            "request_token": request_token,
                        },
                        environ_base={"REMOTE_ADDR": "127.0.0.1"},
                    )
                    self.assertEqual(response2.status_code, 200)
                    data2 = json.loads(response2.data)
                    self.assertTrue(data2.get("fetching"))
        finally:
            # Always release the background thread and shut down the executor
            # to prevent worker thread deadlocks if any assertion fails.
            block_event.set()
            try:
                real_executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            app_state.execution.executor = original_executor

        # Verify chat history contains user messages (and duplicate is deduplicated)
        chat_key = self._chat_key("us", test_symbol)
        with app_state.ai.chat_history_lock:
            history = app_state.ai.chat_history[chat_key]

        user_msgs = [m for m in history if m["role"] == "user"]
        self.assertEqual(
            len(user_msgs), 2
        )  # system initial setup user + 1x Hello AI (second is deduplicated)
        self.assertEqual(user_msgs[-1]["content"], "Hello AI")

    @patch("routes.api_analysis._call_mistral_chat_with_retry")
    def test_api_chat_cache_fast_path(self, mock_chat):
        """Should serve completed responses directly from cache on subsequent calls."""
        mock_chat.return_value = "Cached Reply"
        test_symbol = "AAPL_CACHE"
        request_token = "cache-request-001"
        with patch("routes.api_analysis.extract_api_key", return_value="test-key-32-chars"):
            response1 = self.client.post(
                "/api/chat",
                json={
                    "market": "us",
                    "symbol": test_symbol,
                    "message": "Cache me",
                    "request_token": request_token,
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
            self.assertEqual(response1.status_code, 200)

            # Reset mock to verify it is NOT called again
            mock_chat.reset_mock()

            # Second request should hit cache
            response2 = self.client.post(
                "/api/chat",
                json={
                    "market": "us",
                    "symbol": test_symbol,
                    "message": "Cache me",
                    "request_token": request_token,
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
            self.assertEqual(response2.status_code, 200)
            data2 = json.loads(response2.data)
            self.assertEqual(data2.get("reply"), "Cached Reply")

            # Verify the mock chat was not called again
            mock_chat.assert_not_called()

            # Verify no duplicate assistant messages in history
            chat_key = self._chat_key("us", test_symbol)
            with app_state.ai.chat_history_lock:
                history = app_state.ai.chat_history[chat_key]

            assistant_msgs = [m for m in history if m["role"] == "assistant"]
            self.assertEqual(
                len(assistant_msgs), 2
            )  # initial assistant + Cached Reply (exactly 1 copy)
            self.assertEqual(assistant_msgs[-1]["content"], "Cached Reply")

    @patch("routes.api_analysis._call_mistral_chat_with_retry")
    def test_api_chat_closes_db_connection(self, mock_chat):
        """Background worker thread must close the thread-local database connection when done."""
        mock_chat.return_value = "Done"
        test_symbol = "AAPL_CLOSE"

        # Patch chat_history close method directly
        original_close = app_state.ai.chat_history.close
        mock_close = MagicMock()
        app_state.ai.chat_history.close = mock_close

        try:
            with patch("routes.api_analysis.extract_api_key", return_value="test-key-32-chars"):
                self.client.post(
                    "/api/chat",
                    json={
                        "market": "us",
                        "symbol": test_symbol,
                        "message": "Close Connection",
                        "request_token": "close-request-001",
                    },
                    environ_base={"REMOTE_ADDR": "127.0.0.1"},
                )

            # The chat_history.close() method must have been called in the finally block
            mock_close.assert_called()
        finally:
            app_state.ai.chat_history.close = original_close

    @patch("routes.api_analysis._call_mistral_chat_with_retry")
    def test_distinct_chat_operations_do_not_share_a_symbol_result(self, mock_chat):
        """A new question must not receive a completed result from another request."""
        mock_chat.side_effect = ["First answer", "Second answer"]
        payload = {"market": "us", "symbol": "AAPL_ISOLATED"}
        with patch("routes.api_analysis.extract_api_key", return_value="test-key-32-chars"):
            first = self.client.post(
                "/api/chat",
                json={**payload, "message": "First question", "request_token": "first-request-001"},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
            second = self.client.post(
                "/api/chat",
                json={
                    **payload,
                    "message": "Second question",
                    "request_token": "second-request-01",
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )

        self.assertEqual(json.loads(first.data)["reply"], "First answer")
        self.assertEqual(json.loads(second.data)["reply"], "Second answer")
        self.assertEqual(mock_chat.call_count, 2)

    def test_api_chat_rejects_invalid_symbol(self):
        """R3: /api/chat must reject malformed symbols before reaching the LLM."""
        with patch("routes.api_analysis.extract_api_key", return_value="test-key-32-chars"):
            response = self.client.post(
                "/api/chat",
                json={
                    "market": "us",
                    "symbol": "../not-a-ticker/with spaces",
                    "message": "Hello",
                    "request_token": "invalid-symbol-001",
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error_code"], 1001)  # ErrorCode.INVALID_SYMBOL

    def test_api_chat_rejects_invalid_market(self):
        """R3: /api/chat must reject unknown markets."""
        with patch("routes.api_analysis.extract_api_key", return_value="test-key-32-chars"):
            response = self.client.post(
                "/api/chat",
                json={
                    "market": "not-a-market",
                    "symbol": "AAPL",
                    "message": "Hello",
                    "request_token": "invalid-market-001",
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error_code"], 1002)  # ErrorCode.INVALID_MARKET
