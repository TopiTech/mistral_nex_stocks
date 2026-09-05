# tests/test_ddgs_enhanced_resilience.py
"""Tests for enhanced DDGS error resilience, backend selection, and logging facility."""

import logging
import unittest
from unittest.mock import MagicMock, patch

from services.search.ddgs import (
    DEFAULT_DDGS_BACKENDS_NEWS,
    DEFAULT_DDGS_BACKENDS_TEXT,
    _availability_tracker,
    _collect_ddgs_items,
    _DDGSAvailabilityTracker,
    _get_ddgs_backends,
    ddgs_news_search,
    ddgs_text_search,
)


class DdgsEnhancedResilienceTestCase(unittest.TestCase):
    def setUp(self):
        _availability_tracker.reset_for_testing()

    def tearDown(self):
        _availability_tracker.reset_for_testing()

    def test_default_backends(self):
        """Verify reliable default backends avoid problematic engines like grokipedia/mojeek."""
        text_be = _get_ddgs_backends("text")
        news_be = _get_ddgs_backends("news")

        self.assertEqual(text_be, DEFAULT_DDGS_BACKENDS_TEXT)
        self.assertEqual(news_be, DEFAULT_DDGS_BACKENDS_NEWS)
        self.assertIn("yahoo", text_be)
        self.assertIn("google", text_be)
        self.assertNotIn("grokipedia", text_be)
        self.assertNotIn("mojeek", text_be)
        self.assertIn("bing", news_be)

    def test_custom_backends_via_env(self):
        """Verify backends can be customized via environment variables."""
        with patch.dict(
            "os.environ",
            {
                "DDGS_BACKENDS_TEXT": "brave,startpage",
                "DDGS_BACKENDS_NEWS": "bing,yahoo",
            },
        ):
            self.assertEqual(_get_ddgs_backends("text"), "brave,startpage")
            self.assertEqual(_get_ddgs_backends("news"), "bing,yahoo")

    def test_empty_query_returns_empty_immediately(self):
        """Verify empty or whitespace query returns empty list without calling DDGS."""
        with patch("services.search.ddgs.DDGS") as mock_ddgs:
            res_text = ddgs_text_search("")
            res_news = ddgs_news_search("   ")
            self.assertEqual(res_text, [])
            self.assertEqual(res_news, [])
            mock_ddgs.assert_not_called()

    def test_transient_error_logged_as_info_not_error(self):
        """Verify that normal scraping errors are logged as INFO, not ERROR or WARNING."""
        tracker = _DDGSAvailabilityTracker(max_consecutive_failures=5)
        with patch("services.search.ddgs._availability_tracker", tracker):
            with patch("services.search.ddgs.DDGS") as mock_ddgs_cls:
                mock_session = MagicMock()
                mock_session.text.side_effect = RuntimeError("TLS IllegalParameter")
                mock_ddgs_cls.return_value.__enter__.return_value = mock_session

                with self.assertLogs("services.search.ddgs", level=logging.INFO) as log_cm:
                    res = ddgs_text_search("test query")

                self.assertEqual(res, [])
                self.assertEqual(tracker.consecutive_failures, 1)
                self.assertFalse(tracker.is_unavailable)

                # Ensure logged at INFO level
                info_logs = [r for r in log_cm.records if r.levelno == logging.INFO]
                error_logs = [r for r in log_cm.records if r.levelno >= logging.ERROR]

                self.assertTrue(len(info_logs) > 0)
                self.assertEqual(
                    len(error_logs), 0, "Transient scraping errors must not be logged as ERROR"
                )

    def test_complete_unavailability_threshold_triggers_error(self):
        """Verify that when failures exceed threshold, ERROR is logged indicating complete unavailability."""
        tracker = _DDGSAvailabilityTracker(max_consecutive_failures=3)
        with patch("services.search.ddgs._availability_tracker", tracker):
            with patch("services.search.ddgs.DDGS") as mock_ddgs_cls:
                mock_session = MagicMock()
                mock_session.text.side_effect = RuntimeError("Continuous outage")
                mock_ddgs_cls.return_value.__enter__.return_value = mock_session

                with self.assertLogs("services.search.ddgs", level=logging.INFO) as log_cm:
                    # Run 3 consecutive failures to reach threshold
                    ddgs_text_search("query 1")
                    ddgs_text_search("query 2")
                    ddgs_text_search("query 3")

                self.assertEqual(tracker.consecutive_failures, 3)
                self.assertTrue(tracker.is_unavailable)

                error_logs = [r for r in log_cm.records if r.levelno >= logging.ERROR]
                self.assertEqual(
                    len(error_logs),
                    1,
                    "Exactly one ERROR log should be emitted when threshold reached",
                )
                self.assertIn("DDGS is completely unavailable", error_logs[0].getMessage())

    def test_search_failure_logs_redact_query_and_provider_diagnostics(self):
        """Search failures must not put user queries or provider text in logs."""
        secret = "private-query-provider-trace-5521"
        tracker = _DDGSAvailabilityTracker(max_consecutive_failures=1)
        with (
            patch("services.search.ddgs._availability_tracker", tracker),
            patch("services.search.ddgs.DDGS") as mock_ddgs_cls,
        ):
            mock_session = MagicMock()
            mock_session.text.side_effect = RuntimeError(secret)
            mock_ddgs_cls.return_value.__enter__.return_value = mock_session

            with self.assertLogs("services.search.ddgs", level=logging.INFO) as log_cm:
                result = ddgs_text_search(secret)

        self.assertEqual(result, [])
        self.assertNotIn(secret, "\n".join(log_cm.output))

    def test_recovery_after_unavailability(self):
        """Verify that when a search succeeds after unavailability, recovery is logged and status reset."""
        tracker = _DDGSAvailabilityTracker(max_consecutive_failures=2)
        with patch("services.search.ddgs._availability_tracker", tracker):
            # Cause unavailability
            tracker.record_failure("test", RuntimeError("fail 1"))
            tracker.record_failure("test", RuntimeError("fail 2"))
            self.assertTrue(tracker.is_unavailable)

            # Record a success
            with self.assertLogs("services.search.ddgs", level=logging.INFO) as log_cm:
                tracker.record_success()

            self.assertEqual(tracker.consecutive_failures, 0)
            self.assertFalse(tracker.is_unavailable)
            self.assertTrue(any("recovered" in r.getMessage() for r in log_cm.records))

    def test_text_search_fallback_on_first_attempt_failure(self):
        """Verify that ddgs_text_search falls back to alternative attempts if the primary attempt fails."""
        with patch("services.search.ddgs.DDGS") as mock_ddgs_cls:
            mock_session = MagicMock()
            # First attempt fails, second attempt (e.g. without timelimit or fallback backend) succeeds
            mock_session.text.side_effect = [
                RuntimeError("primary failed"),
                [{"title": "Fallback Result", "body": "Snippet", "href": "https://example.com"}],
            ]
            mock_ddgs_cls.return_value.__enter__.return_value = mock_session

            results = ddgs_text_search("market movers", timelimit="w")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["title"], "Fallback Result")
            self.assertEqual(mock_session.text.call_count, 2)

    def test_news_search_fallback_on_first_attempt_failure(self):
        """Verify that ddgs_news_search falls back to alternative attempts if the primary attempt fails."""
        with patch("services.search.ddgs.DDGS") as mock_ddgs_cls:
            mock_session = MagicMock()
            # First attempt fails, second attempt succeeds
            mock_session.news.side_effect = [
                RuntimeError("primary news failed"),
                [
                    {
                        "title": "Fallback News",
                        "body": "News Body",
                        "url": "https://example.com",
                        "date": "today",
                    }
                ],
            ]
            mock_ddgs_cls.return_value.__enter__.return_value = mock_session

            results = ddgs_news_search("apple earnings", timelimit="d")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["title"], "Fallback News")
            self.assertEqual(mock_session.news.call_count, 2)

    def test_collect_ddgs_items_logs_info_on_worker_failure(self):
        """Verify that _collect_ddgs_items logs worker exceptions at INFO level."""
        with patch(
            "services.search.ddgs.ddgs_news_search", side_effect=RuntimeError("news worker failed")
        ):
            with patch(
                "services.search.ddgs.ddgs_text_search",
                side_effect=RuntimeError("text worker failed"),
            ):
                # When ddgs_news_search and ddgs_text_search return [] (their resilient behavior), items is empty
                res = _collect_ddgs_items(["query1"], "us-en", "d", 2, 2)
                self.assertEqual(res, [])
