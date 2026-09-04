"""Regression tests for code review fixes.

Tests for:
- R1: Deadlock in _set_cached_value (utils/caching.py)
- R2: Thread-local session close in fallback providers
- R3: Input validation for theme length in rebalance endpoint
- R4: Input validation for portfolio_id length in delete endpoint
"""

import threading
from unittest.mock import MagicMock

import pytest

from utils.caching import _set_cached_value, global_cache


class TestDeadlockFix:
    """R1: _set_cached_value must not deadlock when bucket does not exist."""

    def test_set_cached_value_no_deadlock(self):
        """_set_cached_value must complete without deadlock even when bucket is new."""
        duration = 99999  # Use a unique duration to ensure bucket creation
        key = "test_deadlock_key"

        completed = threading.Event()

        def run():
            _set_cached_value(key, "value", duration)
            completed.set()

        t = threading.Thread(target=run)
        t.start()
        t.join(timeout=5)
        assert completed.is_set(), "Deadlock detected: _set_cached_value did not complete"

    def test_set_cached_value_creates_bucket_and_stores(self):
        """_set_cached_value must create a new bucket and store the value."""
        duration = 99998
        key = "test_bucket_creation"
        _set_cached_value(key, "test_value", duration)

        with global_cache.cache_lock:
            assert duration in global_cache.caches
            from utils.caching import sanitize_cache_key

            assert global_cache.caches[duration].get(sanitize_cache_key(key)) == "test_value"


class TestFallbackProviderClose:
    """R2: Fallback providers must have close() methods."""

    def test_yahoo_web_provider_has_close(self):
        from services.fallback_provider import YahooWebScraperProvider

        provider = YahooWebScraperProvider()
        assert hasattr(provider, "close")
        provider.close()  # Should not raise

    def test_yahoo_jp_provider_has_close(self):
        from services.fallback_provider import YahooJPScraperProvider

        provider = YahooJPScraperProvider()
        assert hasattr(provider, "close")
        provider.close()  # Should not raise

    def test_nikkei225jp_provider_has_close(self):
        from services.fallback_provider import Nikkei225JPProvider

        provider = Nikkei225JPProvider()
        assert hasattr(provider, "close")
        provider.close()  # Should not raise

    def test_minkabu_provider_has_close(self):
        from services.fallback_provider import MinkabuProvider

        provider = MinkabuProvider()
        assert hasattr(provider, "close")
        provider.close()  # Should not raise

    def test_composite_provider_has_close(self):
        from services.fallback_provider import CompositeFallbackProvider

        provider = CompositeFallbackProvider()
        assert hasattr(provider, "close")
        provider.close()  # Should not raise

    def test_composite_close_calls_sub_providers(self):
        from services.fallback_provider import CompositeFallbackProvider

        provider = CompositeFallbackProvider()
        # Mock all sub-providers' close methods
        for sub in (provider.yahoo_web, provider.yahoo_jp, provider.nikkei225jp, provider.minkabu):
            sub.close = MagicMock()
        provider.close()
        for sub in (provider.yahoo_web, provider.yahoo_jp, provider.nikkei225jp, provider.minkabu):
            sub.close.assert_called_once()


class TestThemeLengthValidation:
    """R3: Rebalance endpoint must reject themes longer than 120 characters."""

    @pytest.fixture
    def client(self):
        import app as app_module

        flask_app = app_module.create_app()
        flask_app.config["TESTING"] = True
        with flask_app.test_client() as c:
            yield c

    def test_rebalance_rejects_long_theme(self, client):
        """POST /api/ai-portfolio/rebalance with theme > 120 chars must return 400."""
        long_theme = "x" * 121
        resp = client.post(
            "/api/ai-portfolio/rebalance",
            json={"theme": long_theme},
            content_type="application/json",
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "ok" in data and not data["ok"]


class TestPortfolioIdLengthValidation:
    """R4: Delete endpoint must reject portfolio_id longer than 256 characters."""

    @pytest.fixture
    def client(self):
        import app as app_module

        flask_app = app_module.create_app()
        flask_app.config["TESTING"] = True
        with flask_app.test_client() as c:
            yield c

    def test_delete_rejects_long_portfolio_id(self, client):
        """DELETE /api/ai-portfolio/custom with id > 256 chars must return 400."""
        long_id = "x" * 257
        resp = client.delete(
            "/api/ai-portfolio/custom",
            json={"id": long_id},
            content_type="application/json",
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "ok" in data and not data["ok"]
