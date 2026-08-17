"""Coverage for services/search/langsearch.py HTTP-facing logic.

The original test file only covered the pure helpers; this adds the
request/retry/circuit-breaker branches of ``_request_json_post``,
``_langsearch_post_json``, ``langsearch_search``, ``langsearch_rerank``
and ``_collect_langsearch_items`` without any real network I/O.
"""

import unittest
from unittest.mock import MagicMock, patch

import requests

from app_state import app_state
from services.search import langsearch as ls


class RequestJsonPostTestCase(unittest.TestCase):
    def _resp(self, status, body):
        resp = MagicMock()
        resp.ok = 200 <= status < 400
        resp.status_code = status
        resp.json.return_value = body
        return resp

    def test_success_returns_parsed(self):
        resp = self._resp(200, {"code": 200, "data": {}})
        with patch.object(ls.requests, "post", return_value=resp) as mock_post:
            out = ls._request_json_post("http://x", {}, {})
        self.assertEqual(out, {"code": 200, "data": {}})
        mock_post.assert_called_once()

    def test_non_ok_with_msg(self):
        resp = self._resp(400, {"msg": "bad query"})
        with patch.object(ls.requests, "post", return_value=resp):
            with self.assertRaises(requests.HTTPError) as ctx:
                ls._request_json_post("http://x", {}, {})
        self.assertIn("bad query", str(ctx.exception))

    def test_non_ok_with_code_and_message(self):
        resp = self._resp(401, {"code": 401, "message": "unauthorized"})
        with patch.object(ls.requests, "post", return_value=resp):
            with self.assertRaises(requests.HTTPError) as ctx:
                ls._request_json_post("http://x", {}, {})
        self.assertIn("code=401", str(ctx.exception))
        self.assertIn("unauthorized", str(ctx.exception))

    def test_non_ok_with_non_dict_body(self):
        resp = self._resp(500, ["not", "a", "dict"])
        with patch.object(ls.requests, "post", return_value=resp):
            with self.assertRaises(requests.HTTPError) as ctx:
                ls._request_json_post("http://x", {}, {})
        self.assertIn("Unknown LangSearch error", str(ctx.exception))

    def test_non_ok_invalid_json_body(self):
        """Invalid JSON keeps parsed={}, so the fallback uses HTTP status text."""
        resp = MagicMock()
        resp.ok = False
        resp.status_code = 503
        resp.json.side_effect = ValueError("bad json")
        with patch.object(ls.requests, "post", return_value=resp):
            with self.assertRaises(requests.HTTPError) as ctx:
                ls._request_json_post("http://x", {}, {})
        self.assertIn("HTTP 503", str(ctx.exception))

    def test_ok_with_application_error_code(self):
        resp = self._resp(200, {"code": "4010", "msg": "app error"})
        with patch.object(ls.requests, "post", return_value=resp):
            with self.assertRaises(requests.HTTPError) as ctx:
                ls._request_json_post("http://x", {}, {})
        self.assertIn("code=4010", str(ctx.exception))
        self.assertIn("app error", str(ctx.exception))

    def test_ok_with_non_numeric_code_ignored(self):
        resp = self._resp(200, {"code": "abc"})
        with patch.object(ls.requests, "post", return_value=resp):
            out = ls._request_json_post("http://x", {}, {})
        self.assertEqual(out, {"code": "abc"})


class RetryablePredicateTestCase(unittest.TestCase):
    def _http_error(self, status):
        resp = MagicMock()
        resp.status_code = status
        return requests.HTTPError("err", response=resp)

    def test_timeout_is_retryable(self):
        self.assertTrue(ls._langsearch_request_retryable(requests.Timeout("t")))

    def test_connection_error_is_retryable(self):
        self.assertTrue(ls._langsearch_request_retryable(requests.ConnectionError("c")))

    def test_429_and_503_are_retryable(self):
        self.assertTrue(ls._langsearch_request_retryable(self._http_error(429)))
        self.assertTrue(ls._langsearch_request_retryable(self._http_error(503)))

    def test_other_status_not_retryable(self):
        self.assertFalse(ls._langsearch_request_retryable(self._http_error(400)))
        self.assertFalse(ls._langsearch_request_retryable(self._http_error(500)))

    def test_http_error_without_response_not_retryable(self):
        self.assertFalse(
            ls._langsearch_request_retryable(requests.HTTPError("no response", response=None))
        )

    def test_balance_errors_not_retryable(self):
        for msg in ("insufficient balance", "quota exceeded", "balance not enough"):
            self.assertFalse(
                ls._langsearch_request_retryable(requests.HTTPError(msg, response=None))
            )

    def test_unrelated_exception_not_retryable(self):
        self.assertFalse(ls._langsearch_request_retryable(ValueError("boom")))


class LangsearchPostJsonTestCase(unittest.TestCase):
    def setUp(self):
        app_state.ai.langsearch_min_interval_sec = 0.0
        app_state.ai.langsearch_next_allowed_ts = 0.0

    def test_circuit_open_skips_call(self):
        with patch.object(app_state.market, "is_circuit_open", return_value=True), self.assertRaises(
            requests.HTTPError
        ) as ctx:
            ls._langsearch_post_json("http://x", {}, {})
        self.assertIn("circuit is OPEN", str(ctx.exception))

    def test_success_reports_result(self):
        with (
            patch.object(app_state.market, "is_circuit_open", return_value=False),
            patch.object(ls, "_request_json_post", return_value={"ok": True}) as mock_post,
            patch.object(app_state.market, "report_circuit_result") as mock_report,
        ):
            out = ls._langsearch_post_json("http://x", {}, {})
        self.assertEqual(out, {"ok": True})
        mock_post.assert_called_once()
        mock_report.assert_called_once_with("langsearch", success=True)

    def test_429_marks_cooldown_and_reraises(self):
        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {}
        err = requests.HTTPError("rate limited", response=resp)
        with (
            patch.object(app_state.market, "is_circuit_open", return_value=False),
            patch.object(ls, "_request_json_post", side_effect=err),
            patch.object(ls, "_langsearch_mark_retry_after_429") as mock_mark,
            patch("tenacity.nap.time.sleep"),  # tenacity backoff sleeps are no-ops
        ):
            with self.assertRaises(requests.HTTPError):
                ls._langsearch_post_json("http://x", {}, {})
        mock_mark.assert_called()

    def test_429_retry_after_header_used(self):
        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {"Retry-After": "7"}
        err = requests.HTTPError("rate limited", response=resp)
        with (
            patch.object(app_state.market, "is_circuit_open", return_value=False),
            patch.object(ls, "_request_json_post", side_effect=err),
            patch.object(ls, "_langsearch_mark_retry_after_429") as mock_mark,
            patch("tenacity.nap.time.sleep"),
        ):
            with self.assertRaises(requests.HTTPError):
                ls._langsearch_post_json("http://x", {}, {})
        args = [c.args[0] for c in mock_mark.call_args_list]
        self.assertIn(7.0, args)

    def test_429_invalid_retry_after_falls_back_to_default(self):
        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {"Retry-After": "not-a-number"}
        err = requests.HTTPError("rate limited", response=resp)
        with (
            patch.object(app_state.market, "is_circuit_open", return_value=False),
            patch.object(ls, "_request_json_post", side_effect=err),
            patch.object(ls, "_langsearch_mark_retry_after_429") as mock_mark,
            patch("tenacity.nap.time.sleep"),
        ):
            with self.assertRaises(requests.HTTPError):
                ls._langsearch_post_json("http://x", {}, {})
        args = [c.args[0] for c in mock_mark.call_args_list]
        self.assertIn(None, args)

    def test_5xx_reports_circuit_failure(self):
        resp = MagicMock()
        resp.status_code = 500
        resp.headers = {}
        err = requests.HTTPError("server error", response=resp)
        with (
            patch.object(app_state.market, "is_circuit_open", return_value=False),
            patch.object(ls, "_request_json_post", side_effect=err),
            patch.object(app_state.market, "report_circuit_result") as mock_report,
        ):
            with self.assertRaises(requests.HTTPError):
                ls._langsearch_post_json("http://x", {}, {})
        mock_report.assert_called_once_with(
            "langsearch", success=False, threshold=3, open_sec=60
        )

    def test_timeout_reports_circuit_failure(self):
        """Timeout is retryable, so every attempt reports a circuit failure."""
        with (
            patch.object(app_state.market, "is_circuit_open", return_value=False),
            patch.object(ls, "_request_json_post", side_effect=requests.Timeout("slow")),
            patch.object(app_state.market, "report_circuit_result") as mock_report,
            patch("tenacity.nap.time.sleep"),
        ):
            with self.assertRaises(requests.Timeout):
                ls._langsearch_post_json("http://x", {}, {})
        self.assertGreaterEqual(mock_report.call_count, 1)
        mock_report.assert_called_with("langsearch", success=False, threshold=3, open_sec=60)

    def test_retries_after_429_then_succeeds(self):
        """A short Retry-After cooldown still allows the retry to succeed."""
        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {"Retry-After": "2"}
        err = requests.HTTPError("rate limited", response=resp)
        with (
            patch.object(app_state.market, "is_circuit_open", return_value=False),
            patch.object(ls, "_request_json_post", side_effect=[err, {"ok": True}]) as mock_post,
            patch.object(app_state.market, "report_circuit_result") as mock_report,
            patch.object(ls.time, "sleep"),  # slot cooldown wait
            patch("tenacity.nap.time.sleep"),  # tenacity backoff wait
        ):
            out = ls._langsearch_post_json("http://x", {}, {})
        self.assertEqual(out, {"ok": True})
        self.assertEqual(mock_post.call_count, 2)
        mock_report.assert_called_with("langsearch", success=True)


class LangsearchSearchTestCase(unittest.TestCase):
    def test_empty_query_returns_empty(self):
        self.assertEqual(ls.langsearch_search("   ", "key"), [])

    def test_missing_api_key_raises(self):
        with self.assertRaises(ValueError):
            ls.langsearch_search("query", "")

    def test_success_returns_entries(self):
        with patch.object(
            ls, "_langsearch_post_json", return_value={"data": {"webPages": {"value": [{"url": "u"}]}}}
        ):
            out = ls.langsearch_search("q", "key")
        self.assertEqual(out, [{"url": "u"}])

    def test_http_error_429_records_error_and_reraises(self):
        resp = MagicMock()
        resp.status_code = 429
        err = requests.HTTPError("too many", response=resp)
        errors = []
        with (
            patch.object(ls, "_langsearch_post_json", side_effect=err),
            patch.object(ls, "_langsearch_mark_retry_after_429") as mock_mark,
        ):
            with self.assertRaises(requests.HTTPError):
                ls.langsearch_search("q", "key", errors_out=errors)
        self.assertEqual(len(errors), 1)
        mock_mark.assert_called_once()

    def test_other_exception_recorded_and_reraises(self):
        errors = []
        with patch.object(ls, "_langsearch_post_json", side_effect=ValueError("bad")):
            with self.assertRaises(ValueError):
                ls.langsearch_search("q", "key", errors_out=errors)
        self.assertEqual(len(errors), 1)


class LangsearchRerankTestCase(unittest.TestCase):
    def _docs(self, n=3):
        return [{"title": f"doc{i}", "summary": f"summary {i}"} for i in range(n)]

    def test_no_api_key_returns_documents(self):
        docs = self._docs()
        self.assertIs(ls.langsearch_rerank("q", docs, ""), docs)

    def test_too_few_documents_returns_documents(self):
        docs = self._docs(1)
        self.assertIs(ls.langsearch_rerank("q", docs, "key"), docs)

    def test_empty_query_returns_documents(self):
        docs = self._docs()
        self.assertIs(ls.langsearch_rerank("   ", docs, "key"), docs)

    def test_success_sorts_by_relevance_score(self):
        parsed = {
            "results": [
                {"index": 0, "relevance_score": 0.2},
                {"index": 1, "relevance_score": 0.9},
                {"index": 2, "relevance_score": 0.5},
            ]
        }
        with patch.object(ls, "_langsearch_post_json", return_value=parsed):
            out = ls.langsearch_rerank("q", self._docs(), "key")
        self.assertEqual([d["title"] for d in out], ["doc1", "doc2", "doc0"])
        self.assertEqual(out[0]["relevance_score"], 0.9)

    def test_out_of_range_index_skipped(self):
        parsed = {"results": [{"index": 99, "relevance_score": 1.0}]}
        with patch.object(ls, "_langsearch_post_json", return_value=parsed):
            out = ls.langsearch_rerank("q", self._docs(), "key")
        self.assertEqual(len(out), 3)
        self.assertNotIn("relevance_score", out[0])

    def test_no_scored_docs_returns_documents(self):
        with patch.object(ls, "_langsearch_post_json", return_value={"results": []}):
            out = ls.langsearch_rerank("q", self._docs(), "key")
        self.assertEqual(len(out), 3)

    def test_exception_returns_documents_and_warns(self):
        with (
            patch.object(ls, "_langsearch_post_json", side_effect=requests.Timeout("t")),
            patch.object(ls.logger, "warning") as mock_warn,
        ):
            out = ls.langsearch_rerank("q", self._docs(), "key")
        self.assertEqual(len(out), 3)
        mock_warn.assert_called()

    def test_results_in_data_dict(self):
        parsed = {"data": {"results": [{"index": 0, "relevance_score": 0.8}]}}
        with patch.object(ls, "_langsearch_post_json", return_value=parsed):
            out = ls.langsearch_rerank("q", self._docs(), "key")
        self.assertEqual(out[0]["relevance_score"], 0.8)

    def test_document_without_text_uses_placeholder(self):
        """Documents lacking summary/title get a placeholder text for reranking."""
        docs = [{"url": "u1"}, {"url": "u2"}]
        with patch.object(ls, "_langsearch_post_json") as mock_post:
            mock_post.return_value = {"results": [{"index": 0, "relevance_score": 0.5}]}
            ls.langsearch_rerank("q", docs, "key")
        self.assertIn("[no content]", mock_post.call_args[0][1]["documents"])


class CollectLangsearchItemsTestCase(unittest.TestCase):
    def setUp(self):
        app_state.ai.langsearch_min_interval_sec = 0.0

    def test_no_api_key_returns_empty(self):
        self.assertEqual(ls._collect_langsearch_items(["q"], "", "d"), [])

    def test_collects_dedupes_and_limits(self):
        entries = [
            {"title": "a", "summary": "s", "url": "u1"},
            {"title": "a", "summary": "s", "url": "u1"},  # duplicate
            {"title": "b", "summary": "s", "url": "u2"},
            {"title": "c", "summary": "s", "url": "u3"},
            {"title": "d", "summary": "s", "url": "u4"},
        ]
        with patch.object(ls, "langsearch_search", return_value=entries):
            out = ls._collect_langsearch_items(["q1", "q2"], "key", "d", limit=3, query_limit=2)
        self.assertEqual(len(out), 3)

    def test_reranks_when_over_limit(self):
        entries = [{"title": f"d{i}", "summary": "s", "url": f"u{i}"} for i in range(6)]
        with (
            patch.object(ls, "langsearch_search", return_value=entries),
            patch.object(ls, "langsearch_rerank", return_value=entries) as mock_rerank,
        ):
            out = ls._collect_langsearch_items(
                ["q1", "q2"], "key", "d", max_results=6, limit=3, query_limit=2
            )
        self.assertEqual(len(out), 3)
        mock_rerank.assert_called_once()

    def test_search_failure_warns_and_continues(self):
        with (
            patch.object(ls, "langsearch_search", side_effect=requests.HTTPError("fail")),
            patch.object(ls.logger, "warning") as mock_warn,
        ):
            out = ls._collect_langsearch_items(["q1", "q2"], "key", "d")
        self.assertEqual(out, [])
        mock_warn.assert_called()


class LangsearchAcquireSlotTestCase(unittest.TestCase):
    def setUp(self):
        app_state.ai.langsearch_min_interval_sec = 2.0
        app_state.ai.langsearch_next_allowed_ts = 0.0

    def test_cooldown_beyond_max_wait_raises(self):
        app_state.ai.langsearch_next_allowed_ts = (
            ls.time.time() + ls._LANGSEARCH_SLOT_MAX_WAIT_SEC + 100
        )
        with self.assertRaises(RuntimeError):
            ls._langsearch_acquire_slot()

    def test_short_cooldown_sleeps(self):
        app_state.ai.langsearch_next_allowed_ts = ls.time.time() + 0.05
        with patch.object(ls.time, "sleep") as mock_sleep:
            ls._langsearch_acquire_slot()
        mock_sleep.assert_called_once()
        self.assertGreaterEqual(mock_sleep.call_args[0][0], 0.0)

    def test_mark_retry_after_429_sets_cooldown(self):
        before = app_state.ai.langsearch_next_allowed_ts
        ls._langsearch_mark_retry_after_429(retry_after_sec=5)
        self.assertGreaterEqual(app_state.ai.langsearch_next_allowed_ts, before + 5)


if __name__ == "__main__":
    unittest.main()
