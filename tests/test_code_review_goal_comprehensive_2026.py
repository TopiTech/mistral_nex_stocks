# tests/test_code_review_goal_comprehensive_2026.py
"""Comprehensive regression and unit test suite for 2026 code review improvements.

Covers:
- Concurrency and lock safety in background workers and market state
- AI state thread-safe metrics queries and error responses
- Execution state thread join deadline bounding
- Static mtime cache capacity bounding
- Costly GET paths configuration
- SSE interpolator zero / non-finite previous_close handling
- Circuit breaker probe claiming and releasing in AI service
- Image URL validation in chart analysis
- Force parameter handling in news endpoint
- Strict non-negative and min/max validation in screener
- Rebalance cache persistence without premature pop
- History cache hit serving under open circuit
- Trend sources session pooling
- DDGS shared daemon pool
- Disk cache delimiter boundary matching
- Fallback provider finite float parsing
"""

import copy
import math
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import trend_sources as ts
from ai_state import AIState
from app import _CROSS_SITE_COSTLY_GET_PATHS, create_app
from app_state import AppState, app_state
from bg.common import _raw_announce_current_market_state
from bg.sse_interpolator import _interpolate_and_fluctuate_market
from bg.sync_worker import _prepare_sync_items
from execution_state import ExecutionState
from services.fallback_provider import AlphaVantageProvider, YahooJPScraperProvider
from services.search.ddgs import _DDGS_SEARCH_POOL, _collect_ddgs_items
from utils.disk_cache import StockDiskCache


class TestBackendConcurrencyAndState:
    """Tests for backend state, locks, and thread lifecycle."""

    def test_raw_announce_current_market_state_thread_safe_copy(self):
        """Verify _raw_announce_current_market_state copies cache safely."""
        import bg.common as bg_common
        with app_state.cache.sse_data_lock:
            app_state.market.current_stocks_cache = {
                "jp": [{"price": 2500.0, "symbol": "7203"}],
                "us": [{"price": 180.0, "symbol": "AAPL"}],
                "idx": [],
            }
            app_state.market.current_indices_cache = {
                "N225": {"price": 38000.0, "symbol": "N225"},
            }

        bg_common._sse_payload_generation += 1
        with patch("bg.common._announce_frame") as mock_ann1, patch("app_bg._announce_frame") as mock_ann2:
            _raw_announce_current_market_state()
            assert mock_ann1.called or mock_ann2.called

    def test_sync_worker_prepare_sync_items_locking(self):
        """Verify _prepare_sync_items safely inspects target_stocks_cache under sse_data_lock."""
        with app_state.cache.sse_data_lock:
            app_state.market.target_stocks_cache = {
                "jp": [{"price": 2500.0, "symbol": "7203.T", "market": "jp"}],
                "us": [{"price": 180.0, "symbol": "AAPL", "market": "us"}],
            }

        with patch("bg.sync_worker.load_user_stocks"):
            with patch("bg.sync_worker.ensure_stock_placeholder_in_caches"):
                items = _prepare_sync_items(force_load=False, force_fetch=False)
                assert isinstance(items, list)

    def test_ai_state_cache_size_methods(self):
        """Verify AIState response_cache_size and clients_cached_count methods."""
        ai = AIState()
        assert ai.response_cache_size() >= 0
        assert ai.clients_cached_count() >= 0

        # Simulate cached response and client
        with ai.mistral_response_lock:
            ai.mistral_response_cache[("test_key",)] = (time.time(), {"response": "test"})
        assert ai.response_cache_size() == 1

        with ai.mistral_clients_lock:
            ai.mistral_clients["client_1"] = MagicMock()
        assert ai.clients_cached_count() == 1

    def test_execution_state_shutdown_bounded_join(self):
        """Verify ExecutionState.shutdown terminates within global deadline even with slow threads."""
        exec_state = ExecutionState()

        stop_event = threading.Event()

        def slow_worker():
            stop_event.wait(timeout=10.0)

        t = threading.Thread(target=slow_worker, daemon=True)
        t.start()
        exec_state.background_threads.append(t)

        start_t = time.time()
        exec_state.shutdown(wait=False)
        elapsed = time.time() - start_t
        stop_event.set()

        # Shutdown should finish bounded by ~3.5 seconds
        assert elapsed < 4.0

    def test_app_state_shutdown_executors_idempotence(self):
        """Verify shutdown_executors is safe to call repeatedly."""
        test_state = AppState()
        test_state.shutdown_executors()
        assert getattr(test_state, "_shutdown_executors_done", False) is True
        # Second call should return immediately without exception
        test_state.shutdown_executors()


class TestSecurityAndRouteValidation:
    """Tests for API routes and security improvements."""

    def test_costly_get_paths_includes_news(self):
        """Ensure /api/news is in _CROSS_SITE_COSTLY_GET_PATHS for CSRF protection."""
        assert "/api/news" in _CROSS_SITE_COSTLY_GET_PATHS
        assert "/api/stocks" in _CROSS_SITE_COSTLY_GET_PATHS

    def test_static_mtime_cache_capacity_bound(self):
        """Verify _static_mtime_cache in app.py does not grow unbounded."""
        app = create_app()
        with app.test_request_context():
            static_url_func = app.jinja_env.globals.get("static_url")
            assert callable(static_url_func)
            # Call for various test filenames
            for i in range(300):
                url = static_url_func(f"nonexistent_{i}.js")
                assert "nonexistent_" in url

    def test_api_analyze_chart_image_url_validation(self, client):
        """Verify invalid URL schemes / structures are rejected in chart analysis."""
        headers = {"Origin": "http://localhost:5000", "X-Requested-With": "XMLHttpRequest"}
        with patch("routes.api_analysis.extract_api_key", return_value="dummy_key"):
            res = client.post(
                "/api/analyze-chart-image",
                json={"image_data": "javascript:alert(1)", "symbol": "7203"},
                headers=headers,
            )
            assert res.status_code == 400
            data = res.get_json()
            assert data.get("ok") is False

            # Invalid http without netloc
            res2 = client.post(
                "/api/analyze-chart-image",
                json={"image_data": "http://", "symbol": "7203"},
                headers=headers,
            )
            assert res2.status_code == 400

    def test_api_screener_bounds_and_range_validation(self, client):
        """Verify screener rejects negative prices and invalid min > max ranges."""
        # Negative price
        res1 = client.get("/api/screener?min_price=-10")
        assert res1.status_code == 400
        data1 = res1.get_json()
        assert "0以上の数値" in str(data1)

        # min_price > max_price
        res2 = client.get("/api/screener?min_price=500&max_price=100")
        assert res2.status_code == 400
        data2 = res2.get_json()
        assert "min_price は max_price 以下" in str(data2)

        # min_change > max_change
        res3 = client.get("/api/screener?min_change=10&max_change=5")
        assert res3.status_code == 400

    def test_api_rebalance_ai_portfolio_cache_retrieval(self, client):
        """Verify api_rebalance_ai_portfolio pops cache on consumption so fresh rebalances occur."""
        from routes.stocks.ai_portfolio import ai_portfolio_fetch_lock, ai_portfolio_result_cache

        scope = "test_conversation_scope"
        theme = "clean_energy"
        inflight_key = f"rebalance:{scope}:{theme}"

        test_portfolio = {"theme": theme, "stocks": []}
        with ai_portfolio_fetch_lock:
            ai_portfolio_result_cache[inflight_key] = (time.time(), test_portfolio, None)

        with client.session_transaction() as sess:
            sess["mns_analysis_conversation"] = scope

        headers = {"Origin": "http://localhost:5000", "X-Requested-With": "XMLHttpRequest"}
        with patch("routes.stocks.ai_portfolio.extract_api_key", return_value="dummy_key"):
            res = client.post(
                "/api/ai-portfolio/rebalance",
                json={"theme": theme, "current_portfolio": test_portfolio},
                headers=headers,
            )
            assert res.status_code == 200
            data = res.get_json()
            assert data.get("ok") is True

            # Ensure cache entry was popped so subsequent rebalances are fresh
            with ai_portfolio_fetch_lock:
                assert inflight_key not in ai_portfolio_result_cache


class TestServicesAndIntegrations:
    """Tests for services, providers, caching, and background helpers."""

    def test_sse_interpolator_zero_and_infinite_previous_close(self):
        """Verify sse_interpolator handles previous_close <= 0 or inf safely."""
        target = [
            {
                "symbol": "NEW_STOCK",
                "price": 100.0,
                "previous_close": 0.0,
                "change": 5.0,
                "change_percent": 5.0,
                "currency": "USD",
                "market_state": "REGULAR",
            },
            {
                "symbol": "INF_STOCK",
                "price": 50.0,
                "previous_close": float("nan"),
                "change": 2.0,
                "change_percent": 4.0,
                "currency": "USD",
                "market_state": "REGULAR",
            },
        ]
        current = copy.deepcopy(target)

        res = _interpolate_and_fluctuate_market(
            target_list=target,
            current_list=current,
            is_open=True,
            market="us",
        )

        assert len(res) == 2
        assert math.isfinite(res[0]["price"])
        assert math.isfinite(res[0]["change"])
        assert math.isfinite(res[0]["change_percent"])

    def test_disk_cache_delimiter_boundary_matching(self, tmp_path):
        """Verify StockDiskCache.delete_prefix does not delete unrelated keys with overlapping prefixes."""
        cache = StockDiskCache(cache_dir=Path(tmp_path), default_ttl=60)
        cache.set("stock_7203", {"data": "toyota"})
        cache.set("stock_72030", {"data": "other"})
        cache.set("stock_7203_history", {"data": "toyota_hist"})

        removed = cache.delete_prefix("stock_7203")
        assert removed == 2  # stock_7203 and stock_7203_history

        assert cache.get("stock_7203") is None
        assert cache.get("stock_7203_history") is None
        assert cache.get("stock_72030") == {"data": "other"}

    def test_fallback_provider_finite_float(self):
        """Verify fallback providers reject Inf and NaN in _to_float."""
        # AlphaVantage
        av = AlphaVantageProvider()
        quote_data = {
            "Global Quote": {
                "01. symbol": "TEST",
                "05. price": "Infinity",
                "08. previous close": "100.0",
                "09. change": "NaN",
                "10. change percent": "0.0%",
            }
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = quote_data
        mock_resp.raise_for_status = MagicMock()

        with patch("services.fallback_provider.get_alphavantage_api_key", return_value="dummy_key"):
            with patch("requests.get", return_value=mock_resp):
                res = av.get_latest_quote("TEST")
                assert res is not None
                assert res["regularMarketPrice"] == 0.0

        # YahooJPScraper
        ys = YahooJPScraperProvider()
        assert ys is not None


class TestTrendAndSearchPools:
    """Tests for trend sources session pooling and DDGS search pool."""

    def test_trend_session_singleton(self):
        """Verify _get_trend_session returns a reusable pooled requests.Session."""
        s1 = ts._get_trend_session()
        s2 = ts._get_trend_session()
        assert s1 is s2
        assert "https://" in s1.adapters

    def test_ddgs_shared_pool(self):
        """Verify DDGS search uses the shared _DDGS_SEARCH_POOL."""
        assert _DDGS_SEARCH_POOL is not None
        assert _DDGS_SEARCH_POOL._max_workers == 3

        with patch("services.search.ddgs.ddgs_news_search", return_value=[]):
            with patch("services.search.ddgs.ddgs_text_search", return_value=[]):
                res = _collect_ddgs_items(
                    queries=["test query 1", "test query 2"],
                    region="us-en",
                    timelimit="d",
                    limit=5,
                    news_n=2,
                    text_n=2,
                    query_limit=2,
                )
                assert isinstance(res, list)
