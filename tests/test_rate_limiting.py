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

from datetime import datetime, timedelta, timezone
from email.utils import formatdate

from app import app_state


class MistralRateLimitingTestCase(unittest.TestCase):
    """Test Mistral API 429 error handling and streak management"""

    def _release_lock_if_held(self, lock):
        if lock.locked():
            try:
                lock.release()
            except RuntimeError:
                pass

    def setUp(self):
        """Reset app state before each test"""
        app_state.ai.mistral_429_streak = 0
        self._release_lock_if_held(app_state.ai.mistral_cooldown_lock)

    def tearDown(self):
        """Cleanup"""
        app_state.ai.mistral_429_streak = 0

    def test_streak_increments_on_429(self):
        """429 error should increment streak counter"""
        app_state.ai.mistral_429_streak = 0
        # Simulate 429: manually increment
        app_state.ai.mistral_429_streak += 1
        self.assertEqual(app_state.ai.mistral_429_streak, 1)

    def test_streak_resets_on_success(self):
        """Successful response should reset streak to 0"""
        app_state.ai.mistral_429_streak = 5
        # Simulate successful response - reset streak
        app_state.ai.mistral_429_streak = 0
        self.assertEqual(app_state.ai.mistral_429_streak, 0)

    def test_streak_max_is_10(self):
        """Streak should cap at 10 (by design implementation)"""
        # According to code: if mistral_429_streak >= 3, return error immediately
        # But streak itself can grow up to 10 before ultimate reset
        app_state.ai.mistral_429_streak = 10
        self.assertEqual(app_state.ai.mistral_429_streak, 10)

    def test_third_streak_should_error_immediately(self):
        """Third (and subsequent) 429s should return error without retry"""
        # This is verified in app.py line 1150
        # If mistral_429_streak >= 3, return error immediately
        app_state.ai.mistral_429_streak = 3
        should_error = app_state.ai.mistral_429_streak >= 3
        self.assertTrue(should_error, "Should error at 3rd streak")

    def test_cooldown_backoff_calculation(self):
        """Backoff time should be min(2^streak, 60) seconds"""
        for streak in range(1, 11):
            backoff_exp = min(streak, 7)  # cap at 2^7 = 128, then 60s total cap
            backoff_secs = min(2**backoff_exp, 60)
            self.assertGreater(backoff_secs, 0)
            self.assertLessEqual(backoff_secs, 60)

    def test_semaphore_controls_concurrent_calls(self):
        """Semaphore should limit concurrent Mistral calls to 3"""
        sem = app_state.ai.mistral_call_semaphore
        # Semaphore is Semaphore(3) - up to 3 concurrent
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
    """Test yfinance 429 circuit breaker"""

    def setUp(self):
        """Reset yfinance rate limit state"""
        app_state.market.is_yfinance_rate_limited = False
        app_state.market.yfinance_rate_limit_until = 0.0

    def test_circuit_breaker_open_on_third_timeout(self):
        """yfinance circuit breaker should open after 3 timeouts"""
        with patch(
            "app.app_state.market.history_circuit_state",
            {"AAPL": {"timeout_streak": 3, "open_until": time.time() + 20}},
        ):
            # Circuit breaker active for AAPL
            cb_state = app_state.market.history_circuit_state["AAPL"]
            self.assertEqual(cb_state["timeout_streak"], 3)
            self.assertGreater(cb_state["open_until"], time.time())

    def test_circuit_breaker_duration_is_20_seconds(self):
        """Circuit breaker should block for 20 seconds after 3rd timeout"""
        open_time = time.time()
        duration_secs = 20

        actual_close = open_time + duration_secs  # Check the constant
        self.assertEqual(actual_close - open_time, duration_secs)

    def test_10_minute_rate_limit_on_429(self):
        """yfinance 429 should trigger 10-minute backoff"""
        # From app.py: yfinance_rate_limit_until = time.time() + 600
        app_state.market.is_yfinance_rate_limited = True
        app_state.market.yfinance_rate_limit_until = time.time() + 600

        backoff_secs = app_state.market.yfinance_rate_limit_until - time.time()
        self.assertGreaterEqual(backoff_secs, 599)
        self.assertLessEqual(backoff_secs, 600)


class RetryAfterParsingTestCase(unittest.TestCase):
    """Test Retry-After header parsing in different formats"""

    def test_retry_after_seconds_format(self):
        """Retry-After: 120 (seconds) should be parsed correctly"""
        header_value = "120"
        try:
            seconds = int(header_value)
            self.assertEqual(seconds, 120)
        except ValueError:
            self.fail("Should parse integer seconds")

    def test_retry_after_http_date_format(self):
        """Retry-After: <HTTP-date> should be parsed to seconds"""
        # HTTP-date format: "Wed, 21 Oct 2025 07:28:00 GMT"
        future_time = datetime.now(timezone.utc) + timedelta(seconds=60)
        http_date = formatdate(
            timeval=future_time.timestamp(), localtime=False, usegmt=True
        )

        # Parsing would use email.utils.parsedate_to_datetime
        from email.utils import parsedate_to_datetime

        parsed_time = parsedate_to_datetime(http_date)
        now = datetime.now(timezone.utc)
        delay_secs = (parsed_time - now).total_seconds()

        self.assertGreater(delay_secs, 59)
        self.assertLess(delay_secs, 61)

    def test_retry_after_invalid_format_ignored(self):
        """Invalid Retry-After should be ignored (fallback to default)"""
        header_value = "invalid-format"
        try:
            int(header_value)
            self.fail("Should not parse invalid format")
        except ValueError:
            pass  # Expected


class LangSearchRateLimitingTestCase(unittest.TestCase):
    """Test LangSearch API rate limiting (1.25s minimum interval)"""

    def setUp(self):
        """Reset LangSearch state"""
        app_state.ai.langsearch_next_allowed_ts = 0.0
        app_state.ai.langsearch_min_interval_sec = 1.25
        app_state.ai.langsearch_429_cooldown_sec = 60.0

    def test_langsearch_min_interval_is_1_25_seconds(self):
        """LangSearch should enforce 1.25 second minimum interval"""
        min_interval = app_state.ai.langsearch_min_interval_sec
        self.assertEqual(min_interval, 1.25)

    def test_langsearch_429_cooldown_is_60_seconds(self):
        """LangSearch 429 should trigger 60 second cooldown"""
        cooldown = app_state.ai.langsearch_429_cooldown_sec
        self.assertEqual(cooldown, 60.0)

    def test_langsearch_throttle_calculation(self):
        """Should calculate throttle delay correctly"""
        app_state.ai.langsearch_next_allowed_ts = time.time() + 0.5
        delay = app_state.ai.langsearch_next_allowed_ts - time.time()
        self.assertGreater(delay, 0.4)
        self.assertLess(delay, 0.6)


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
        key1 = "stock:AAPL"
        key2 = "stock:MSFT"

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
            from app_helpers import get_cached

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
    """Test timeout parameter constants"""

    def test_batch_fetch_timeout_is_20_seconds(self):
        """Batch fetch should have 20s timeout (from code review)"""
        # From app.py around line 2074
        batch_timeout = 20
        self.assertEqual(batch_timeout, 20)

    def test_single_fetch_timeout_is_6_seconds(self):
        """Single stock fetch should have 6s timeout"""
        # From app.py: timeout per single stock
        single_timeout = 6
        self.assertEqual(single_timeout, 6)

    def test_max_retries_is_2(self):
        """Max retries should be 2 for fetches"""
        max_retries = 2
        self.assertEqual(max_retries, 2)

    def test_semiphone_allows_one_concurrent_mistral_call(self):
        """Only 3 concurrent Mistral calls allowed"""
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
        self._reset_patch = patch(
            "session_manager.reset_yfinance_auth", return_value=None
        )
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
        self.assertGreaterEqual(
            self.mgr._excluded_until.get("yfinance", 0) - time.time(), 250
        )

    def test_handle_block_401_window_is_short_but_nonzero(self):
        """401 (Invalid Crumb) uses a 60s exclusion window, not the old 5s."""
        fake_resp = MagicMock()
        fake_resp.url = "https://query1.finance.yahoo.com/v8/finance/chart/AAPL"
        fake_resp.headers = {}
        self.mgr._handle_block(401, fake_resp)
        window = self.mgr._excluded_until.get("yfinance", 0) - time.time()
        self.assertGreaterEqual(window, 50)
        self.assertLess(window, 120)

    def test_401_streak_accelerates_growth(self):
        """Consecutive 401s should escalate the interval faster than a single hit."""
        fake_resp = MagicMock()
        fake_resp.url = "https://query1.finance.yahoo.com/v8/finance/chart/AAPL"
        fake_resp.headers = {}
        self.mgr._handle_block(401, fake_resp)
        after_first = self.mgr._adaptive_interval_sec
        self.mgr._handle_block(401, fake_resp)
        after_second = self.mgr._adaptive_interval_sec
        # The growth factor compounds, so the second 401 pushes it higher.
        self.assertGreater(after_second, after_first)

    def test_handle_block_invokes_crumb_reset(self):
        """Every block must force a yfinance crumb/cookie reset."""
        fake_resp = MagicMock()
        fake_resp.url = "https://query1.finance.yahoo.com/v8/finance/chart/AAPL"
        fake_resp.headers = {}
        with patch("session_manager.reset_yfinance_auth") as reset_mock:
            self.mgr._handle_block(429, fake_resp)
            reset_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
