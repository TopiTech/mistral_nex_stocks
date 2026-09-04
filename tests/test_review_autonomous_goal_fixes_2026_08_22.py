import logging
import queue
import time
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from app import _schedule_news_warmup_impl
from app_state import app_state
from crypto_utils import unprotect_data
from routes.api_analysis import api_analysis_bp
from routes.api_stocks import ai_portfolio_fetch_lock, ai_portfolio_result_cache, api_stocks_bp
from routes.api_system import _terminate_current_process


class TestReviewAutonomousGoalFixes20260822:
    def test_r1_ai_portfolio_rebalance_cache_popped_for_fresh_executions(self):
        """R1: Verify that AI portfolio rebalance results are not left in cache to block fresh rebalance."""
        app = Flask(__name__)
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        app.config["SECRET_KEY"] = "test-secret-key-32-chars-long-security"
        app.register_blueprint(api_stocks_bp)

        theme = "test_r1_theme"
        conversation_scope = "scope_test_r1_12345678"
        inflight_key = f"rebalance:{conversation_scope}:{theme}"

        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["mns_analysis_conversation"] = conversation_scope
            with patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)):
                with patch("routes.api_stocks.generate_ai_portfolio_by_theme") as mock_gen:
                    mock_gen.side_effect = [
                        {"id": theme, "version": 1, "items": []},
                        {"id": theme, "version": 2, "items": []},
                    ]

                    # First rebalance request (synchronous completion)
                    res1 = client.post("/api/ai-portfolio/rebalance", json={"theme": theme})
                    assert res1.status_code == 200
                    data1 = res1.get_json()
                    assert data1["ok"] is True
                    assert data1["portfolio"]["version"] == 1

                    # Verify that cache entry was popped
                    with ai_portfolio_fetch_lock:
                        assert inflight_key not in ai_portfolio_result_cache

                    # Second rebalance request must execute a fresh rebalance (version 2)
                    res2 = client.post("/api/ai-portfolio/rebalance", json={"theme": theme})
                    assert res2.status_code == 200
                    data2 = res2.get_json()
                    assert data2["ok"] is True
                    assert data2["portfolio"]["version"] == 2
                    assert mock_gen.call_count == 2

    def test_r1_ai_portfolio_rebalance_async_polling_cleans_cache(self):
        """R1: Verify async polling path retrieves rebalance result and clears the cache entry."""
        theme = "test_r1_async_theme"
        conversation_scope = "scope_test_r1_12345678"
        inflight_key = f"rebalance:{conversation_scope}:{theme}"

        app = Flask(__name__)
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        app.config["SECRET_KEY"] = "test-secret-key-32-chars-long-security"
        app.register_blueprint(api_stocks_bp)

        # Seed the result cache as if background job finished after client timed out
        expected_portfolio = {"id": theme, "status": "rebalanced", "items": []}
        with ai_portfolio_fetch_lock:
            ai_portfolio_result_cache[inflight_key] = (
                time.time(),
                expected_portfolio,
                None,
            )

        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["mns_analysis_conversation"] = conversation_scope
            with patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)):
                # Client polls for the result
                res = client.post("/api/ai-portfolio/rebalance", json={"theme": theme})
                assert res.status_code == 200
                data = res.get_json()
                assert data["ok"] is True
                assert data["portfolio"]["status"] == "rebalanced"

                # Verify cache is cleared after polling retrieval
                with ai_portfolio_fetch_lock:
                    assert inflight_key not in ai_portfolio_result_cache

    def test_r2_terminate_current_process_logging_shutdown_order(self):
        """R2: Verify _terminate_current_process logs before shutting down logging."""
        mock_logger = MagicMock(spec=logging.Logger)

        class ProcessExit(Exception):
            pass

        with (
            patch("routes.api_system.logging.shutdown") as mock_log_shutdown,
            patch("routes.api_system.os.name", "nt"),
            patch("routes.api_system.os._exit", side_effect=ProcessExit) as mock_exit,
            pytest.raises(ProcessExit),
        ):
            _terminate_current_process(mock_logger)

        mock_logger.info.assert_called_with("Exiting process on Windows")
        mock_log_shutdown.assert_called_once()
        mock_exit.assert_called_with(0)

    def test_r3_api_chat_fast_path_normalization_structured_content(self):
        """R3: Verify fast-path chat history deduplication normalizes structured assistant content."""
        app = Flask(__name__)
        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = "test-secret-key-32-chars-long-security"
        app.register_blueprint(api_analysis_bp)

        operation_token = "tok_test_r3_12345678_long"
        conversation_scope = "scope_test_r3_1234567890"
        inflight_key = f"chat:{conversation_scope}:{operation_token}"
        chat_key = f"{conversation_scope}:us:AAPL"

        # Existing history ending with structured list format of the same content
        structured_content = [{"type": "text", "text": "This is the AI analysis."}]
        cached_ai_reply = structured_content

        from routes.api_analysis import chat_fetch_lock, chat_result_cache

        with chat_fetch_lock:
            chat_result_cache[inflight_key] = (
                time.time(),
                cached_ai_reply,
                None,
            )

        # Initialize mock chat_history
        mock_history_store = {chat_key: [{"role": "assistant", "content": structured_content}]}

        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["mns_analysis_conversation"] = conversation_scope

            with (
                patch("routes.api_analysis.require_trusted_or_admin", return_value=(True, None)),
                patch(
                    "routes.api_analysis.extract_api_key",
                    return_value="test_mistral_api_key_32_chars",
                ),
                patch.object(app_state.ai, "chat_history", mock_history_store),
            ):
                res = client.post(
                    "/api/chat",
                    json={
                        "symbol": "AAPL",
                        "market": "us",
                        "message": "test",
                        "request_token": operation_token,
                    },
                )
                assert res.status_code == 200
                data = res.get_json()
                assert data["reply"] == cached_ai_reply

                # History should NOT have appended a duplicate turn
                history = mock_history_store[chat_key]
                assert len(history) == 1

    def test_r4_schedule_news_warmup_handles_queue_full(self):
        """R4: Verify schedule_news_warmup catches queue.Full and does not crash."""
        mock_exec = MagicMock()
        mock_exec.submit.side_effect = queue.Full("ThreadPoolExecutor queue is full")

        with patch.object(app_state.execution, "news_executor", mock_exec):
            with (
                patch("app.get_langsearch_api_key", return_value=""),
                patch("app.get_tavily_api_key", return_value=""),
            ):
                # Must catch queue.Full without raising unhandled exception
                _schedule_news_warmup_impl()
                mock_exec.submit.assert_called_once()

    def test_r5_unprotect_data_empty_fernet_value(self):
        """R5: Verify unprotect_data safely returns empty string for empty Fernet value without logging ERROR."""
        with patch("crypto_utils.logger.error") as mock_err:
            res_empty_str = unprotect_data({"scheme": "fernet", "value": ""})
            assert res_empty_str == ""

            res_none_val = unprotect_data({"scheme": "fernet", "value": None})
            assert res_none_val == ""

            # Verify no ERROR was logged
            mock_err.assert_not_called()
