"""Regression tests for code review improvements (lock ordering, error response details, lock scoping, accessibility)."""

from __future__ import annotations

from unittest.mock import patch

from app import app
from app_state import app_state
from error_codes import ErrorCode
from routes.stocks.ai_portfolio import (
    ai_portfolio_fetch_lock,
    ai_portfolio_result_cache,
)


def test_copy_ai_portfolio_lock_ordering_and_scoping() -> None:
    """Verify copy-to-my does not hold sse_data_lock or user_stocks_lock when invalidating caches."""
    orig_csrf = app.config.get("WTF_CSRF_ENABLED")
    app.config["WTF_CSRF_ENABLED"] = False
    try:
        locks_held_during_invalidate: dict[str, bool] = {}

        def mock_invalidate_stock_caches(sym: str) -> None:
            # Check whether sse_data_lock or user_stocks_lock is currently acquired by this thread
            locks_held_during_invalidate["sse_data_lock"] = app_state.cache.sse_data_lock._is_owned()
            locks_held_during_invalidate["user_stocks_lock"] = bool(
                getattr(app_state.market.user_stocks_lock, "_is_owned", lambda: False)()
            )

        with (
            patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)),
            patch("routes.api_stocks.save_user_stocks"),
            patch("routes.stocks.ai_portfolio.invalidate_stock_caches", side_effect=mock_invalidate_stock_caches),
            patch("routes.api_stocks._sync_realtime_symbol"),
            patch("routes.stocks.ai_portfolio._announce_watchlist_state"),
            patch("routes.api_stocks.schedule_sync_all_stocks_now"),
        ):
            client = app.test_client()

            with app_state.market.user_stocks_lock:
                app_state.market.user_us.pop("NVDA_LOCK_TEST", None)

            with app_state.cache.sse_data_lock:
                app_state.market.current_stocks_cache["us"] = [
                    {"symbol": "NVDA_LOCK_TEST", "name": "NVIDIA", "price": 120.0}
                ]
                app_state.market.target_stocks_cache["us"] = [
                    {"symbol": "NVDA_LOCK_TEST", "name": "NVIDIA", "price": 120.0}
                ]

            res = client.post(
                "/api/ai-portfolio/copy-to-my",
                json={
                    "items": [
                        {
                            "symbol": "NVDA_LOCK_TEST",
                            "market": "us",
                            "weight_pct": 20.0,
                            "target_price": 130.0,
                        }
                    ]
                },
            )
            assert res.status_code == 200
            data = res.get_json()
            assert data["ok"] is True

            # Neither sse_data_lock (Level 5) nor user_stocks_lock should be held
            # when calling invalidate_stock_caches (Level 3 cache_lock inside)
            assert locks_held_during_invalidate.get("sse_data_lock") is False
            assert locks_held_during_invalidate.get("user_stocks_lock") is False

            # Verify that holding details in SSE cache were updated correctly
            with app_state.cache.sse_data_lock:
                cur = next(
                    (
                        s
                        for s in app_state.market.current_stocks_cache.get("us", [])
                        if s.get("symbol") == "NVDA_LOCK_TEST"
                    ),
                    None,
                )
                assert cur is not None
                assert cur.get("avg_price") == 130.0
                assert cur.get("shares", 0) > 0
    finally:
        app.config["WTF_CSRF_ENABLED"] = orig_csrf
        with app_state.market.user_stocks_lock:
            app_state.market.user_us.pop("NVDA_LOCK_TEST", None)


def test_ai_portfolio_generate_worker_error_not_cached_and_details_provided() -> None:
    """Verify background worker error in generate_ai_portfolio returns detailed reason and is NOT cached."""
    test_theme = "error_test_theme_gen_r3"

    orig_csrf = app.config.get("WTF_CSRF_ENABLED")
    app.config["WTF_CSRF_ENABLED"] = False
    try:
        with ai_portfolio_fetch_lock:
            ai_portfolio_result_cache.clear()

        client = app.test_client()
        with (
            patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)),
            patch("routes.stocks.ai_portfolio.extract_api_key", return_value="dummy_key"),
            patch(
                "routes.api_stocks.generate_ai_portfolio_by_theme",
                side_effect=RuntimeError("Simulated LLM service failure"),
            ),
        ):
            res = client.post("/api/ai-portfolio/generate", json={"theme": test_theme})
            assert res.status_code == 500
            data = res.get_json()
            assert data["error_code"] == ErrorCode.INTERNAL_SERVER_ERROR.value
            assert data["details"]["reason"] == "AI ポートフォリオの生成に失敗しました"

            # Verify error was NOT cached per R3 spec
            with ai_portfolio_fetch_lock:
                assert not any(key.endswith(f":{test_theme}") for key in ai_portfolio_result_cache)
    finally:
        app.config["WTF_CSRF_ENABLED"] = orig_csrf
        with ai_portfolio_fetch_lock:
            ai_portfolio_result_cache.clear()


def test_ai_portfolio_rebalance_worker_error_not_cached_and_details_provided() -> None:
    """Verify background worker error in rebalance_ai_portfolio returns detailed reason and is NOT cached."""
    test_theme = "error_test_theme_reb_r3"

    orig_csrf = app.config.get("WTF_CSRF_ENABLED")
    app.config["WTF_CSRF_ENABLED"] = False
    try:
        with ai_portfolio_fetch_lock:
            ai_portfolio_result_cache.clear()

        client = app.test_client()
        with (
            patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)),
            patch("routes.stocks.ai_portfolio.extract_api_key", return_value="dummy_key"),
            patch(
                "routes.api_stocks.generate_ai_portfolio_by_theme",
                side_effect=RuntimeError("Simulated rebalance timeout"),
            ),
        ):
            res = client.post("/api/ai-portfolio/rebalance", json={"theme": test_theme})
            assert res.status_code == 500
            data = res.get_json()
            assert data["error_code"] == ErrorCode.INTERNAL_SERVER_ERROR.value
            assert data["details"]["reason"] == "AI ポートフォリオのリバランスに失敗しました"

            # Verify error was NOT cached per R3 spec
            with ai_portfolio_fetch_lock:
                assert not any(key.endswith(f":{test_theme}") for key in ai_portfolio_result_cache)
    finally:
        app.config["WTF_CSRF_ENABLED"] = orig_csrf
        with ai_portfolio_fetch_lock:
            ai_portfolio_result_cache.clear()


def test_add_stock_ext_lock_scoping() -> None:
    """Verify api_add_stock_ext runs cache invalidation and SSE broadcast outside user_stocks_lock."""
    locks_during_invalidate: dict[str, bool] = {}

    def mock_invalidate_stock_caches(sym: str) -> None:
        locks_during_invalidate["user_stocks_lock"] = bool(
            getattr(app_state.market.user_stocks_lock, "_is_owned", lambda: False)()
        )

    orig_csrf = app.config.get("WTF_CSRF_ENABLED")
    app.config["WTF_CSRF_ENABLED"] = False
    try:
        with (
            patch("utils.env_helpers._is_remote_api_enabled", return_value=False),
            patch("utils.networking._is_local_request", return_value=True),
            patch("utils.networking._is_loopback_ip", return_value=True),
            patch("utils.networking._is_allowed_shutdown_origin", return_value=True),
            patch("routes.api_stocks.get_or_create_extension_api_token", return_value="test-token-1234567890"),
            patch("routes.api_stocks.save_user_stocks"),
            patch("routes.stocks.views.invalidate_stock_caches", side_effect=mock_invalidate_stock_caches),
            patch("routes.stocks.views.ensure_stock_placeholder_in_caches"),
            patch("routes.stocks.views._announce_watchlist_state"),
            patch("routes.stocks.views._sync_realtime_symbol"),
            patch("routes.api_stocks.schedule_sync_all_stocks_now"),
        ):
            client = app.test_client()
            res = client.post(
                "/api/stocks/add_ext",
                headers={
                    "X-MNS-Extension-Request": "true",
                    "Authorization": "Bearer test-token-1234567890",
                    "Origin": "http://127.0.0.1:5000",
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
                json={"symbol": "EXT_LOCK_TEST", "market": "us", "name": "Ext Lock Test"},
            )
            assert res.status_code == 200
            data = res.get_json()
            assert data["ok"] is True
            # Invalidate must have run without user_stocks_lock held
            assert locks_during_invalidate.get("user_stocks_lock") is False
    finally:
        app.config["WTF_CSRF_ENABLED"] = orig_csrf
        with app_state.market.user_stocks_lock:
            app_state.market.user_us.pop("EXT_LOCK_TEST", None)


def test_add_stock_ext_rejects_origin_before_initializing_token() -> None:
    """An invalid Origin must not trigger first-use token/master-key writes."""
    orig_csrf = app.config.get("WTF_CSRF_ENABLED")
    app.config["WTF_CSRF_ENABLED"] = False
    try:
        with (
            patch("utils.env_helpers._is_remote_api_enabled", return_value=False),
            patch("utils.networking._is_allowed_shutdown_origin", return_value=False),
            patch(
                "routes.api_stocks.get_or_create_extension_api_token",
                return_value="unused-token",
            ) as get_token,
        ):
            response = app.test_client().post(
                "/api/stocks/add_ext",
                headers={"X-MNS-Extension-Request": "true"},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
                json={"symbol": "ORIGIN_ORDER_TEST", "market": "us"},
            )

        assert response.status_code == 403
        get_token.assert_not_called()
    finally:
        app.config["WTF_CSRF_ENABLED"] = orig_csrf


def test_template_markup_navigation_and_accessibility() -> None:
    """Verify settings navigation uses semantic anchor tags and drawer close buttons use aria-hidden."""
    client = app.test_client()

    # 1. Settings page navigation
    res_settings = client.get("/settings")
    assert res_settings.status_code == 200
    html_settings = res_settings.get_data(as_text=True)
    assert '<a href="/main" class="heatmap-nav-btn" id="back-btn">' in html_settings
    assert '<a href="/screener" class="heatmap-nav-btn" id="screener-btn">' in html_settings

    # 2. Main page drawer close button accessibility
    res_main = client.get("/main")
    assert res_main.status_code == 200
    html_main = res_main.get_data(as_text=True)
    assert 'id="closeStockDetailDrawerBtn"' in html_main
    assert '<span aria-hidden="true">&times;</span>' in html_main
    assert 'id="closeAiDrawerBtn"' in html_main
    assert 'id="closeFsChartModal"' in html_main
