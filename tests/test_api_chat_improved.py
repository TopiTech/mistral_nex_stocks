"""Unit tests for the improved api_chat caching and deduplication in routes/api_analysis.py."""

import json
import sys
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.test_api_integration import APIIntegrationTestCase
from app_state import app_state


class APIChatImprovedTestCase(APIIntegrationTestCase):
    """Test improvements to the /api/chat endpoint."""

    def setUp(self):
        super().setUp()
        # Reset chat history for test run
        with app_state.ai.chat_history_lock:
            app_state.ai.chat_history.clear()

        # Reset caches
        from routes.api_analysis import chat_result_cache, chat_fetch_inflight

        chat_result_cache.clear()
        chat_fetch_inflight.clear()

    def tearDown(self):
        super().tearDown()
        with app_state.ai.chat_history_lock:
            app_state.ai.chat_history.clear()

    @patch("routes.api_analysis._call_mistral_chat_with_retry")
    def test_api_chat_basic_success(self, mock_chat):
        """Should succeed in generating a chat response and updating history."""
        mock_chat.return_value = "Mocked AI Response"

        # Mock API credentials to bypass checks
        with patch("routes.api_analysis.extract_api_key", return_value="test-key-32-chars"):
            response = self.client.post(
                "/api/chat",
                json={
                    "market": "us",
                    "symbol": "AAPL",
                    "message": "What is the stock price?",
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertEqual(data.get("reply"), "Mocked AI Response")

        # Verify chat history contains both user and assistant messages exactly once
        chat_key = "us:AAPL"
        with app_state.ai.chat_history_lock:
            history = app_state.ai.chat_history[chat_key]

        # History format: system, user initial, assistant initial, user message, assistant reply
        user_msgs = [m for m in history if m["role"] == "user"]
        assistant_msgs = [m for m in history if m["role"] == "assistant"]

        # User messages: "[対象銘柄: AAPL] この銘柄について質問します。" and "What is the stock price?"
        self.assertEqual(len(user_msgs), 2)
        self.assertEqual(user_msgs[-1]["content"], "What is the stock price?")

        # Assistant messages: "AAPL銘柄についてお答えします。" and "Mocked AI Response"
        self.assertEqual(len(assistant_msgs), 2)
        self.assertEqual(assistant_msgs[-1]["content"], "Mocked AI Response")

    @patch("routes.api_analysis._call_mistral_chat_with_retry")
    def test_api_chat_polling_deduplication(self, mock_chat):
        """Should not duplicate user messages when in-flight polling occurs."""
        # Setup a blocking event to control when the background job completes
        block_event = threading.Event()

        def slow_chat(*args, **kwargs):
            block_event.wait()
            return "Slow Response"

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
                            "symbol": "AAPL",
                            "message": "Hello AI",
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
                            "symbol": "AAPL",
                            "message": "Hello AI",
                        },
                        environ_base={"REMOTE_ADDR": "127.0.0.1"},
                    )
                    self.assertEqual(response2.status_code, 200)
                    data2 = json.loads(response2.data)
                    self.assertTrue(data2.get("fetching"))

                    # Now release the background thread so it can finish cleanly
                    block_event.set()
                    # Wait for the executor to complete
                    real_executor.shutdown(wait=True)
        finally:
            app_state.execution.executor = original_executor

        # Verify chat history contains user messages (and duplicate is deduplicated)
        chat_key = "us:AAPL"
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

        # Run first request (synchronously completed)
        with patch("routes.api_analysis.extract_api_key", return_value="test-key-32-chars"):
            response1 = self.client.post(
                "/api/chat",
                json={
                    "market": "us",
                    "symbol": "AAPL",
                    "message": "Cache me",
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
                    "symbol": "AAPL",
                    "message": "Cache me",
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
            self.assertEqual(response2.status_code, 200)
            data2 = json.loads(response2.data)
            self.assertEqual(data2.get("reply"), "Cached Reply")

            # Verify the mock chat was not called again
            mock_chat.assert_not_called()

            # Verify no duplicate assistant messages in history
            chat_key = "us:AAPL"
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
                        "symbol": "AAPL",
                        "message": "Close Connection",
                    },
                    environ_base={"REMOTE_ADDR": "127.0.0.1"},
                )

            # The chat_history.close() method must have been called in the finally block
            mock_close.assert_called()
        finally:
            app_state.ai.chat_history.close = original_close
