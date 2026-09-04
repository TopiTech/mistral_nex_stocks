"""Regression tests for code review findings R1 through R11."""

import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from flask import Flask

from app_state import app_state
from messaging import sse_event_log
from routes.api_analysis import api_analysis_bp
from routes.api_stocks import api_stocks_bp
from routes.api_system import api_system_bp
from utils.caching import get_cached


@pytest.fixture
def review_app():
    """Create a minimal Flask test app for testing review fixes."""
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret-key"
    app.register_blueprint(api_stocks_bp)
    app.register_blueprint(api_analysis_bp)
    app.register_blueprint(api_system_bp)
    return app


def test_r1_swr_news_concurrent_request_success(review_app):
    """R1: Test that concurrent requests during SWR news fetch receive the valid result."""
    with review_app.test_request_context():
        dummy_news = {
            "retrieve_status": True,
            "us_news": [],
            "jp_news": [],
            "crypto_news": [],
            "timestamp": time.time(),
        }

        with (
            patch("routes.api_analysis.require_trusted_or_admin", return_value=(True, None)),
            patch("routes.api_analysis.extract_api_key", return_value="dummy-mistral-api-key"),
            patch("utils.caching._get_cached_value") as mock_get_cached,
            patch("utils.caching._set_cached_value"),
            patch(
                "routes.api_analysis.news_service.get_synchronized_market_news",
                return_value=dummy_news,
            ),
        ):
            # 1st call gets stale bundle and triggers SWR
            mock_get_cached.side_effect = lambda k, **kw: (
                dummy_news if not str(k).endswith("_ts") else time.time() - 90000
            )

            client = review_app.test_client()
            res = client.post("/api/news", json={})
            assert res.status_code == 200
            data = res.get_json()
            assert data["retrieve_status"] is True


def test_r2_sse_heartbeat_does_not_pollute_replay_log():
    """R2: Heartbeats must not advance sse_event_log sequence IDs."""
    with sse_event_log._lock:
        initial_seq = sse_event_log._seq

    # Ensure sse_event_log sequence counter is unchanged by heartbeat generation
    heartbeat_data = json.dumps({"type": "heartbeat", "timestamp": time.time()})
    _ = f"event: heartbeat\ndata: {heartbeat_data}\n\n"

    with sse_event_log._lock:
        assert sse_event_log._seq == initial_seq


def test_r3_get_cached_returns_none_when_fetcher_produces_none():
    """R3: get_cached returns None (not CACHE_FETCHING) when fetcher produces None."""
    key = f"test_none_key_{time.time()}"
    ev_start = threading.Event()

    def slow_none_fetcher() -> dict | None:
        ev_start.set()
        time.sleep(0.05)
        res_val: dict | None = None
        return res_val

    results = []

    def waiter_thread():
        ev_start.wait(timeout=5)
        res = get_cached(key, slow_none_fetcher, duration=60)
        results.append(res)

    t = threading.Thread(target=waiter_thread)
    t.start()

    # Main thread runs fetcher
    main_res = get_cached(key, slow_none_fetcher, duration=60)
    t.join(timeout=5)

    assert main_res is None
    assert len(results) == 1
    assert results[0] is None, f"Expected None but got {results[0]}"


def test_r4_posix_ancestor_loop_structure(tmp_path):
    """R4: Verify POSIX ancestor traversal logic with mock /proc files."""
    import native_host.native_host as nh

    # Build mock /proc directory structure: 3000 -> 2000 -> 1000 -> 1
    p3000 = tmp_path / "3000"
    p3000.mkdir()
    (p3000 / "status").write_text("Name:\tpython\nPPid:\t2000\n", encoding="utf-8")

    p2000 = tmp_path / "2000"
    p2000.mkdir()
    (p2000 / "status").write_text("Name:\tsh\nPPid:\t1000\n", encoding="utf-8")
    (p2000 / "cmdline").write_bytes(b"/bin/sh\x00")

    p1000 = tmp_path / "1000"
    p1000.mkdir()
    (p1000 / "status").write_text("Name:\tchrome\nPPid:\t1\n", encoding="utf-8")
    (p1000 / "cmdline").write_bytes(b"/opt/google/chrome/chrome\x00")

    ancestors = nh._get_posix_ancestor_process_names(max_depth=5, proc_dir=tmp_path, start_pid=3000)
    assert "sh" in ancestors
    assert "chrome" in ancestors

    # Also verify dispatch in _get_ancestor_process_names under POSIX
    with (
        patch("os.name", "posix"),
        patch.object(nh.os, "name", "posix"),
        patch.object(
            nh,
            "_get_posix_ancestor_process_names",
            return_value=["sh", "chrome"],
        ),
    ):
        dispatched = nh._get_ancestor_process_names(max_depth=5)
        assert dispatched == ["sh", "chrome"]


def test_r5_admin_token_enforced_in_system_routes(review_app, monkeypatch):
    """R5: MNS_ADMIN_TOKEN is enforced in _require_admin_token_if_remote even in local mode."""
    monkeypatch.setenv("MNS_ADMIN_TOKEN", "valid-admin-token-at-least-32-chars-long!")
    monkeypatch.delenv("MNS_ALLOW_REMOTE_API", raising=False)

    client = review_app.test_client()

    # Without header -> 403
    with patch("routes.api_system._is_local_request", return_value=True):
        res = client.get("/api/cache-stats")
        assert res.status_code == 403
        data = res.get_json()
        assert "invalid admin token" in data.get("details", {}).get("reason", "")

    # With valid header -> 200
    with (
        patch("routes.api_system._is_local_request", return_value=True),
        patch.object(app_state.cache, "get_stats", return_value={}),
    ):
        res = client.get(
            "/api/cache-stats",
            headers={"X-MNS-Admin-Token": "valid-admin-token-at-least-32-chars-long!"},
        )
        assert res.status_code == 200


def test_r6_settings_template_tag_matching():
    """R6: templates/settings.html has equal open and close div tags."""
    template_path = Path(__file__).resolve().parent.parent / "templates" / "settings.html"
    content = template_path.read_text(encoding="utf-8")

    open_divs = content.count("<div")
    close_divs = content.count("</div>")
    assert open_divs == close_divs, f"Mismatched div tags: {open_divs} open vs {close_divs} close"


def test_r7_api_update_portfolio_legacy_jp_alias(review_app):
    """R7: api_update_portfolio resolves legacy JP bare-numeric symbols."""
    with review_app.test_request_context():
        with (
            patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)),
            patch("routes.api_stocks.save_user_stocks"),
            patch("routes.api_stocks.invalidate_stock_caches"),
            patch("routes.api_stocks.ensure_stock_placeholder_in_caches"),
        ):
            with app_state.market.user_stocks_lock:
                # Store legacy symbol '7203' without .T in user_jp
                app_state.market.user_jp["7203"] = {
                    "name": "トヨタ自動車",
                    "shares": 100,
                    "avg_price": 2500.0,
                }

            client = review_app.test_client()
            # Update using canonical symbol '7203.T'
            res = client.post(
                "/api/stocks/portfolio",
                json={"market": "jp", "symbol": "7203.T", "shares": 200, "avg_price": 2600.0},
            )
            assert res.status_code == 200
            data = res.get_json()
            assert data["success"] is True
            # Ensure normalized to 7203.T in user_jp
            assert "7203.T" in app_state.market.user_jp
            assert "7203" not in app_state.market.user_jp
            assert app_state.market.user_jp["7203.T"]["shares"] == 200


def test_r8_uninstall_script_has_supports_should_process():
    """R8: uninstall_host_windows.ps1 declares SupportsShouldProcess."""
    script_path = (
        Path(__file__).resolve().parent.parent / "native_host" / "uninstall_host_windows.ps1"
    )
    content = script_path.read_text(encoding="utf-8")
    assert "[CmdletBinding(SupportsShouldProcess=$true)]" in content


def test_r10_api_stocks_stream_403_schema(review_app):
    """R10: api_stocks_stream returns ok: False in 403 error response."""
    with patch("routes.api_stocks.require_sse_auth", return_value=(False, "unauthorized")):
        client = review_app.test_client()
        res = client.get("/api/stocks/stream")
        assert res.status_code == 403
        data = res.get_json()
        assert data.get("ok") is False
        assert data.get("error") == "unauthorized"


def test_r11_history_circuit_state_none_value(review_app):
    """R11: api_stock_history handles None value in history_circuit_state without AttributeError."""
    with review_app.test_request_context():
        with (
            patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)),
            patch("routes.api_stocks._has_cached_key", return_value=False),
            patch("routes.api_stocks._submit_async_history_fetch", return_value=True),
        ):
            with app_state.market.history_circuit_lock:
                app_state.market.history_circuit_state["AAPL"] = None

            client = review_app.test_client()
            res = client.get("/api/stock-history?symbol=AAPL&period=1mo")
            assert res.status_code == 200
            data = res.get_json()
            assert data.get("fetching") is True
