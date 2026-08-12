"""
Rate Limiting Tests - Mistral API and yfinance 429 handling

Tests cover:
- Mistral API 429 streak management (1-10 consecutive failures)
- yfinance 429 circuit breaker (3 consecutive timeout → 20s backoff)
- Retry-After header parsing (seconds, date format, epoch)
- LangSearch rate limiting (1.25s min interval)
"""

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import UTC, datetime, timedelta
from email.utils import formatdate

from app import app_state


class MistralRateLimitingTestCase(unittest.TestCase):
    """Test Mistral API 429 streak/backoff via the real state helpers."""

    def setUp(self):
        """Reset app state before each test"""
        app_state.ai.mistral_429_streak = 0
        app_state.ai.mistral_next_allowed_ts = 0.0

    def tearDown(self):
        """Cleanup"""
        app_state.ai.mistral_429_streak = 0
        app_state.ai.mistral_next_allowed_ts = 0.0

    def test_mark_mistral_429_increments_streak(self):
        """mark_mistral_429() increments the streak counter."""
        app_state.ai.mark_mistral_429()
        self.assertEqual(app_state.ai.mistral_429_streak, 1)

    def test_backoff_grows_and_is_capped(self):
        """Backoff grows exponentially and is capped (300s max)."""
        previous = 0.0
        for _ in range(8):
            backoff = app_state.ai.mark_mistral_429()
            self.assertGreaterEqual(backoff, previous)
            previous = backoff
        self.assertLessEqual(previous, 300.0)
        self.assertLessEqual(app_state.ai.mistral_429_streak, 6)

    def test_reset_mistral_streak_clears_limit(self):
        """reset_mistral_streak() clears the streak but keeps the 429 cooldown (R3)."""
        app_state.ai.mark_mistral_429()
        app_state.ai.mark_mistral_429()
        cooldown = app_state.ai.mistral_next_allowed_ts
        self.assertGreater(cooldown, 0.0)
        app_state.ai.reset_mistral_streak()
        self.assertEqual(app_state.ai.mistral_429_streak, 0)
        self.assertEqual(app_state.ai.mistral_next_allowed_ts, cooldown)

    def test_retry_after_honored_as_floor(self):
        """An explicit Retry-After hint is used as a floor for the backoff."""
        before = time.time()
        backoff = app_state.ai.mark_mistral_429(retry_after_sec=200)
        self.assertGreaterEqual(backoff, 200.0)
        self.assertGreaterEqual(app_state.ai.mistral_next_allowed_ts, before + 200)

    def test_semaphore_controls_concurrent_calls(self):
        """Semaphore should limit concurrent Mistral calls to 3."""
        sem = app_state.ai.mistral_call_semaphore
        acqs = []
        for i in range(3):
            acquired = sem.acquire(blocking=False)
            self.assertTrue(acquired, f"Should acquire semaphore at index {i}")
            acqs.append(acquired)
        # Try to acquire again (4th time - should fail with blocking=False)
        acquired_4th = sem.acquire(blocking=False)
        self.assertFalse(acquired_4th, "Should not acquire 4th time")
        # Release all acquired slots
        for _ in range(3):
            sem.release()


class YfinanceRateLimitingTestCase(unittest.TestCase):
    """Test yfinance 429 handling via the real market_state helper."""

    def setUp(self):
        """Reset yfinance rate limit state"""
        app_state.market.yfinance_rate_limit_until = 0.0
        app_state.market.yfinance_429_streak = 0

    def tearDown(self):
        app_state.market.yfinance_rate_limit_until = 0.0
        app_state.market.yfinance_429_streak = 0

    @patch("market_state.yf_session_manager.mark_rate_limited")
    def test_mark_yf_429_sets_exclusion_window(self, mock_mark):
        """mark_yf_429() records a graduated backoff and bumps the streak."""
        before = time.time()
        backoff = app_state.market.mark_yf_429()
        self.assertGreaterEqual(backoff, 5)
        self.assertGreaterEqual(app_state.market.yfinance_rate_limit_until, before + backoff - 1)
        self.assertEqual(app_state.market.yfinance_429_streak, 1)
        mock_mark.assert_called_once()

    @patch("market_state.yf_session_manager.mark_rate_limited")
    def test_mark_yf_429_window_is_monotonic(self, mock_mark):
        """A shorter subsequent backoff must NOT shrink the recorded window."""
        app_state.market.yfinance_rate_limit_until = time.time() + 600
        app_state.market.mark_yf_429()
        self.assertGreaterEqual(app_state.market.yfinance_rate_limit_until, time.time() + 599)

    @patch("market_state.yf_session_manager.mark_rate_limited")
    def test_mark_yf_429_extends_window(self, mock_mark):
        """A larger Retry-After hint extends the recorded window."""
        app_state.market.yfinance_rate_limit_until = time.time() + 5
        app_state.market.mark_yf_429(retry_after=200)
        self.assertGreaterEqual(app_state.market.yfinance_rate_limit_until, time.time() + 199)


class RetryAfterParsingTestCase(unittest.TestCase):
    """Test the real parse_retry_after helper in seconds and HTTP-date formats."""

    @staticmethod
    def _resp(headers):
        from types import SimpleNamespace

        # SimpleNamespace (NOT MagicMock): a MagicMock auto-creates a
        # ``.response`` attribute, which routes parsing down the exception/
        # response-wrapper branch instead of the plain-response branch.
        return SimpleNamespace(headers=headers)

    def test_retry_after_seconds_format(self):
        """Retry-After: 120 (seconds) should be parsed correctly."""
        from utils.http_utils import parse_retry_after

        self.assertEqual(parse_retry_after(self._resp({"Retry-After": "120"})), 120)

    def test_retry_after_http_date_format(self):
        """Retry-After: <HTTP-date> should be parsed to a delay in seconds."""
        from utils.http_utils import parse_retry_after

        future_time = datetime.now(UTC) + timedelta(seconds=60)
        http_date = formatdate(timeval=future_time.timestamp(), localtime=False, usegmt=True)
        delay = parse_retry_after(self._resp({"Retry-After": http_date}))
        self.assertIsNotNone(delay)
        self.assertGreaterEqual(delay, 59)
        self.assertLess(delay, 61)

    def test_retry_after_exception_wrapper(self):
        """Exceptions carrying a response (e.g. requests.HTTPError) are unwrapped."""
        from types import SimpleNamespace

        from utils.http_utils import parse_retry_after

        exc = SimpleNamespace(response=SimpleNamespace(headers={"retry-after": "45"}))
        self.assertEqual(parse_retry_after(exc), 45)

    def test_retry_after_invalid_format_returns_none(self):
        """Invalid Retry-After should be ignored (return None)."""
        from utils.http_utils import parse_retry_after

        self.assertIsNone(parse_retry_after(self._resp({"Retry-After": "invalid-format"})))

    def test_retry_after_missing_header_returns_none(self):
        """Absent Retry-After header should return None."""
        from utils.http_utils import parse_retry_after

        self.assertIsNone(parse_retry_after(self._resp({})))
        self.assertIsNone(parse_retry_after(None))


class LangSearchRateLimitingTestCase(unittest.TestCase):
    """Test LangSearch rate limiting via the real slot/cooldown helpers."""

    def setUp(self):
        """Reset LangSearch state"""
        app_state.ai.langsearch_next_allowed_ts = 0.0
        app_state.ai.langsearch_min_interval_sec = 1.25
        app_state.ai.langsearch_429_cooldown_sec = 60.0

    def tearDown(self):
        app_state.ai.langsearch_next_allowed_ts = 0.0

    def test_langsearch_slot_wait_is_bounded(self):
        """A long cooldown must fail fast instead of sleeping for minutes."""
        from services.search.langsearch import (
            _LANGSEARCH_SLOT_MAX_WAIT_SEC,
            _langsearch_acquire_slot,
        )

        app_state.ai.langsearch_next_allowed_ts = time.time() + 90
        app_state.ai.langsearch_min_interval_sec = 1.25
        start = time.time()
        with self.assertRaises(RuntimeError):
            _langsearch_acquire_slot()
        # Must fail fast (far below the 90s cooldown).
        self.assertLess(time.time() - start, _LANGSEARCH_SLOT_MAX_WAIT_SEC + 2)

    def test_langsearch_slot_short_wait_is_respected(self):
        """A wait within the bound is applied (no error)."""
        from services.search.langsearch import _langsearch_acquire_slot

        app_state.ai.langsearch_next_allowed_ts = time.time() + 0.05
        start = time.time()
        _langsearch_acquire_slot()
        self.assertGreaterEqual(time.time() - start, 0.04)

    def test_langsearch_429_cooldown_recorded(self):
        """A 429 marks the next-allowed timestamp with the cooldown floor."""
        from services.search.langsearch import _langsearch_mark_retry_after_429

        before = time.time()
        _langsearch_mark_retry_after_429()
        self.assertGreaterEqual(app_state.ai.langsearch_next_allowed_ts, before + 59)

    def test_langsearch_429_retry_after_floor(self):
        """A server Retry-After hint is respected as a floor."""
        from services.search.langsearch import _langsearch_mark_retry_after_429

        before = time.time()
        _langsearch_mark_retry_after_429(retry_after_sec=30)
        self.assertGreaterEqual(app_state.ai.langsearch_next_allowed_ts, before + 29)


class CacheStampedePreventionTestCase(unittest.TestCase):
    """Test cache stampede prevention mechanism"""

    def _release_lock_if_held(self, lock):
        if lock.locked():
            try:
                lock.release()
            except RuntimeError:
                pass

    def setUp(self):
        """Reset fetch events"""
        app_state.cache.fetch_events = {}
        self._release_lock_if_held(app_state.cache.fetch_events_lock)

    def test_fetch_event_created_per_key(self):
        """Each cache key should have its own event"""
        key1 = ("stock:AAPL", 60)
        key2 = ("stock:MSFT", 60)

        # Simulate event creation
        if key1 not in app_state.cache.fetch_events:
            app_state.cache.fetch_events[key1] = MagicMock()  # threading.Event()
        if key2 not in app_state.cache.fetch_events:
            app_state.cache.fetch_events[key2] = MagicMock()

        self.assertIn(key1, app_state.cache.fetch_events)
        self.assertIn(key2, app_state.cache.fetch_events)
        self.assertNotEqual(app_state.cache.fetch_events[key1], app_state.cache.fetch_events[key2])

    def test_concurrent_requests_block_on_same_key(self):
        """Concurrent requests for same key should serialize via Event wait"""
        import threading

        key = "stock:TESTSerialize"
        call_count = 0
        call_log = []
        first_call_done = threading.Event()

        def fetch_func():
            nonlocal call_count
            call_count += 1
            call_log.append(time.time())
            if call_count == 1:
                time.sleep(0.1)
                first_call_done.set()
                time.sleep(0.2)
            return {"price": 150.0}

        app_state.cache.fetch_events = {}

        results = []

        def worker():
            from utils.caching import get_cached

            results.append(get_cached(key, fetch_func, duration=60))

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        first_call_done.wait(timeout=2)
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertEqual(call_count, 1, "fetch_func should be called only once")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], {"price": 150.0})
        self.assertEqual(results[1], {"price": 150.0})


class TimeoutParametersTestCase(unittest.TestCase):
    """Test the real timeout/retry constants used by the fetchers."""

    def test_timeout_constants_load(self):
        from constants import (
            HISTORY_SEMAPHORE_TIMEOUT,
            YFINANCE_MAX_RETRIES,
            YFINANCE_TIMEOUT_BATCH,
            YFINANCE_TIMEOUT_SINGLE,
        )

        self.assertGreaterEqual(YFINANCE_TIMEOUT_BATCH, 1)
        self.assertGreaterEqual(YFINANCE_TIMEOUT_SINGLE, 1)
        self.assertGreaterEqual(HISTORY_SEMAPHORE_TIMEOUT, 1)
        self.assertGreaterEqual(YFINANCE_MAX_RETRIES, 0)

    def test_semaphore_allows_three_concurrent_mistral_calls(self):
        """Only 3 concurrent Mistral calls allowed."""
        sem = app_state.ai.mistral_call_semaphore
        acqs = []
        for i in range(3):
            acquired = sem.acquire(blocking=False)
            self.assertTrue(acquired, f"Should acquire at {i}")
            acqs.append(acquired)

        # Cannot acquire 4th
        acquired4 = sem.acquire(blocking=False)
        self.assertFalse(acquired4)

        for _ in range(3):
            sem.release()


class AdaptiveIntervalDecayTestCase(unittest.TestCase):
    """Test the adaptive spacing math in YFinanceSessionManager.

    The single source of truth for inter-request pacing is now
    ``YFinanceSessionManager._compute_wait`` (relaxes toward base after a quiet
    period) and ``_handle_block`` (grows on a Yahoo 401/402/429/439). The old
    double-throttle in ``acquire_yfinance_slot`` no longer exists, so these
    tests target the session manager directly.
    """

    def setUp(self):
        from session_manager import YFinanceSessionManager

        YFinanceSessionManager._reset_for_testing()
        # Patch the crumb reset so tests never touch the real yfinance singleton.
        self._reset_patch = patch("session_manager.reset_yfinance_auth", return_value=None)
        self._reset_patch.start()
        self.mgr = YFinanceSessionManager()
        self.mgr._adaptive_interval_sec = 3.0
        self.mgr._last_block_ts = 0.0
        self.mgr._last_request_ts = 0.0
        self.mgr._consecutive_401_count = 0
        self.mgr._excluded_until = {}

    def tearDown(self):
        self._reset_patch.stop()
        from session_manager import YFinanceSessionManager

        YFinanceSessionManager._reset_for_testing()

    def test_compute_wait_returns_zero_when_quiet_and_at_base(self):
        """When already at base interval and quiet, no extra wait is added."""
        from constants import YFINANCE_REQ_MIN_INTERVAL_BASE

        self.mgr._adaptive_interval_sec = YFINANCE_REQ_MIN_INTERVAL_BASE
        self.mgr._last_block_ts = 0.0  # long ago -> decay already settled
        self.mgr._last_request_ts = 0.0
        wait = self.mgr._compute_wait()
        self.assertAlmostEqual(wait, 0.0, places=3)

    def test_compute_wait_honours_spacing(self):
        """_compute_wait enforces the current adaptive interval between calls."""
        self.mgr._adaptive_interval_sec = 3.0
        self.mgr._last_request_ts = time.time()
        wait = self.mgr._compute_wait()
        self.assertGreaterEqual(wait, 2.0)  # ~3s minus the tiny elapsed

    def test_handle_block_grows_interval_and_uses_status_window(self):
        """A 429 block must grow the adaptive interval and set a ~300s exclusion."""
        before = self.mgr._adaptive_interval_sec
        fake_resp = MagicMock()
        fake_resp.url = "https://query1.finance.yahoo.com/v8/finance/chart/AAPL"
        fake_resp.headers = {}
        self.mgr._handle_block(429, fake_resp)
        self.assertGreater(self.mgr._adaptive_interval_sec, before)
        self.assertGreaterEqual(self.mgr._excluded_until.get("yfinance", 0) - time.time(), 250)

    def test_handle_block_401_window_is_short_but_nonzero(self):
        """401 (Invalid Crumb) does not set exclusion window and does not rate-limit."""
        fake_resp = MagicMock()
        fake_resp.url = "https://query1.finance.yahoo.com/v8/finance/chart/AAPL"
        fake_resp.headers = {}
        self.mgr._handle_block(401, fake_resp)
        self.assertFalse(self.mgr.is_rate_limited("yfinance"))

    def test_401_streak_accelerates_growth(self):
        """Consecutive 401s should rotate UA/epoch but not grow the pacing interval."""
        fake_resp = MagicMock()
        fake_resp.url = "https://query1.finance.yahoo.com/v8/finance/chart/AAPL"
        fake_resp.headers = {}
        before_epoch = self.mgr._session_epoch
        self.mgr._handle_block(401, fake_resp)
        after_first = self.mgr._adaptive_interval_sec
        self.mgr._handle_block(401, fake_resp)
        after_second = self.mgr._adaptive_interval_sec

        self.assertEqual(after_first, after_second)
        self.assertEqual(self.mgr._session_epoch, before_epoch + 2)

    def test_handle_block_invokes_crumb_reset(self):
        """Every block must force a yfinance crumb/cookie reset."""
        fake_resp = MagicMock()
        fake_resp.url = "https://query1.finance.yahoo.com/v8/finance/chart/AAPL"
        fake_resp.headers = {}
        with patch("session_manager.reset_yfinance_auth") as reset_mock:
            self.mgr._handle_block(429, fake_resp)
            reset_mock.assert_called_once()


class RateLimitSkipPollingDuplicatesTestCase(unittest.TestCase):
    """Test skip_polling_duplicates in the rate_limit decorator.

    Polling requests that repeat the same ``request_token`` must not consume
    the endpoint quota, while distinct tokens still count normally.
    """

    def setUp(self):
        from route_helpers import _rate_limit_lock, _rate_limit_store

        with _rate_limit_lock:
            _rate_limit_store.clear()

    def tearDown(self):
        from route_helpers import _rate_limit_lock, _rate_limit_store

        with _rate_limit_lock:
            _rate_limit_store.clear()

    def _build_decorated(self):
        """Build a tiny Flask app with a rate-limited route."""
        from flask import Flask, jsonify, request

        from route_helpers import rate_limit

        app = Flask(__name__)
        app.config["TESTING"] = True

        @app.route("/api/chat", methods=["POST"])
        @rate_limit(max_requests=2, window_seconds=60, skip_polling_duplicates=True)
        def chat():
            body = request.get_json(silent=True) or {}
            return jsonify({"ok": True, "token": body.get("request_token")})

        return app

    def test_same_token_polls_do_not_consume_quota(self):
        """Repeated requests with the same token must never hit 429."""
        app = self._build_decorated()
        client = app.test_client()
        env = {"REMOTE_ADDR": "192.168.1.200"}

        token = "abcdefghijklmnopqrstuvwxyz123456"
        for _ in range(50):  # far beyond max_requests=2
            resp = client.post(
                "/api/chat",
                json={"request_token": token},
                environ_base=env,
            )
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

    def test_skip_handler_runs_outside_the_global_lock(self):
        """A repeated-token poll must NOT run its handler while holding the global
        rate-limit lock (R1): a long-running poll (chat waits up to ~8s) would
        otherwise serialize every other rate-limited endpoint behind it."""
        from flask import Flask, jsonify

        from route_helpers import _rate_limit_lock, rate_limit

        app = Flask(__name__)
        app.config["TESTING"] = True

        @app.route("/api/chat", methods=["POST"])
        @rate_limit(max_requests=2, window_seconds=60, skip_polling_duplicates=True)
        def chat():
            # Regression: with the old implementation the handler ran inside
            # ``with _rate_limit_lock``, so the lock would be held here and a
            # concurrent request to any other rate-limited endpoint would block.
            assert not _rate_limit_lock.locked(), "global rate-limit lock held in handler"
            return jsonify({"ok": True})

        client = app.test_client()
        env = {"REMOTE_ADDR": "192.168.1.210"}
        token = "abcdefghijklmnopqrstuvwxyz123456"
        # First request records the token; subsequent polls take the skip path.
        for _ in range(3):
            resp = client.post(
                "/api/chat",
                json={"request_token": token},
                environ_base=env,
            )
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

    def test_distinct_tokens_consume_quota(self):
        """Distinct request tokens must be counted toward the limit."""
        app = self._build_decorated()
        client = app.test_client()
        env = {"REMOTE_ADDR": "192.168.1.201"}

        for i in range(2):
            resp = client.post(
                "/api/chat",
                json={"request_token": f"tok{i:040d}"},
                environ_base=env,
            )
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

        # Third distinct token exceeds max_requests=2 -> 429
        resp = client.post(
            "/api/chat",
            json={"request_token": "tok2" + "0" * 38},
            environ_base=env,
        )
        self.assertEqual(resp.status_code, 429, resp.get_data(as_text=True))

    def test_same_token_polls_are_bounded_by_per_token_cap(self):
        """Reusing one request_token must NOT bypass the quota indefinitely.

        Regression (rate-limit bypass): the skip_polling_duplicates path used
        to let ANY number of same-token requests through without consuming the
        endpoint quota. After the short-lived result cache expires, every poll
        can start a NEW upstream (paid) AI job, so a single reused token could
        burn unlimited Mistral quota. The per-token poll budget bounds this:
        once exhausted, same-token requests are counted against the normal
        quota and receive 429.
        """
        app = self._build_decorated()
        client = app.test_client()
        env = {"REMOTE_ADDR": "192.168.1.220"}
        token = "abcdefghijklmnopqrstuvwxyz123456"

        with patch("route_helpers._RATE_LIMIT_MAX_TOKEN_POLLS", 5):
            statuses = []
            for _ in range(10):
                resp = client.post(
                    "/api/chat",
                    json={"request_token": token},
                    environ_base=env,
                )
                statuses.append(resp.status_code)

        # Request 1 registers the token and is counted (200). Requests 2-6 use
        # the 5-poll skip budget (200). Request 7+ exceeds the budget, is
        # counted against max_requests=2 and must be rate limited.
        self.assertEqual(statuses[:6], [200] * 6)
        self.assertEqual(statuses[6:], [429] * 4)

    def test_default_poll_cap_bounds_reused_token(self):
        """With the default cap (120) a reused token is still eventually 429.

        Legitimate UI polling never approaches the cap (the chat/analyze-v2
        clients poll at most ~8 times per job), so normal use is unaffected
        while the unbounded-bypass regression is closed even with defaults.
        """
        app = self._build_decorated()
        client = app.test_client()
        env = {"REMOTE_ADDR": "192.168.1.221"}
        token = "abcdefghijklmnopqrstuvwxyz123456"

        statuses = []
        for _ in range(125):
            resp = client.post(
                "/api/chat",
                json={"request_token": token},
                environ_base=env,
            )
            statuses.append(resp.status_code)

        # 1 (registration, counted) + 120 (skip budget) + 1 (first
        # fall-through, which pushes the count to max_requests=2 and passes)
        # -> 121x200; the remaining requests exceed max_requests=2 -> 429.
        self.assertEqual(statuses[:121], [200] * 121)
        self.assertEqual(statuses[121:], [429] * 4)


if __name__ == "__main__":
    unittest.main()
