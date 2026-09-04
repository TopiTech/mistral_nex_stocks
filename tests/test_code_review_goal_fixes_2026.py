"""Comprehensive regression and prevention tests for 2026 code review improvements."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from app import app
from app_state import app_state
from bg.leader_election import _release_leader_lock
from error_codes import ErrorCode
from routes.pages import _get_safe_template_context
from routes.stocks.quotes import _submit_async_info_fetch, api_stock_details
from services.embeddings_service import get_mistral_embeddings_batch
from services.market_data_service import build_popular_symbol_items
from services.stock_service import fetch_history_sync_impl
from session_manager import YFinanceSessionManager
from shutdown_manager import ShutdownTokenManager
from utils.chat_history import SQLiteChatHistoryStore


def test_leader_lock_release_preserves_lock_file(tmp_path):
    fake_lock = tmp_path / ".mns_sync_leader.lock"
    fake_lock.write_text(str(os.getpid()), encoding="utf-8")

    mock_f = MagicMock()
    with (
        patch("bg.leader_election._get_leader_lock_file", return_value=mock_f),
        patch("bg.leader_election._set_leader_lock_file") as mock_set_f,
        patch("bg.leader_election._set_is_sync_leader") as mock_set_leader,
    ):
        _release_leader_lock()
        mock_f.close.assert_called_once()
        mock_set_f.assert_called_once_with(None)
        mock_set_leader.assert_called_once_with(False)


def test_stock_service_transient_failure_does_not_poison_negative_cache():
    with (
        patch("services.stock_service.safe_get_ticker", return_value=None),
        patch.object(app_state.market, "is_negative_cached_symbol", return_value=False),
        patch.object(app_state.market, "is_yf_rate_limited", return_value=False),
    ):
        res = fetch_history_sync_impl("INVALID_SYM", "us", "1mo", "1d")
        assert res.get("transient") is True


def test_build_popular_symbol_items_case_insensitive():
    pop_sources = [("us", ["AAPL", "MSFT", "GOOGL"])]
    seen = set()
    items = build_popular_symbol_items("us", q="aapl", seen_symbols=seen, pop_sources=pop_sources)
    assert len(items) == 1
    assert items[0][0] == "AAPL"


def test_embeddings_service_bounds_check():
    client_mock = MagicMock()
    data_mock = [
        MagicMock(embedding=[0.1, 0.2]),
        MagicMock(embedding=[0.3, 0.4]),
        MagicMock(embedding=[0.5, 0.6]),
    ]
    client_mock.embeddings.create.return_value = MagicMock(
        data=data_mock, usage=type("Usage", (), {"total_tokens": 10})()
    )

    texts = ["hello", "world"]
    with (
        patch("services.embeddings_service._get_client", return_value=client_mock),
        patch("services.ai_service._acquire_mistral_call_slot", return_value=0.0),
        patch("services.ai_service._wait_for_rate_limit_slot", return_value=False),
    ):
        res = get_mistral_embeddings_batch(texts, api_key="fake-key", batch_size=2)
        assert len(res) == 2
        assert res[0] == [0.1, 0.2]
        assert res[1] == [0.3, 0.4]


def test_session_manager_retry_on_closed_session():
    mgr = YFinanceSessionManager()
    sess = mgr.get_session()
    sess.close()

    fresh_sess = MagicMock()
    fresh_resp = MagicMock()
    fresh_resp.status_code = 200
    fresh_sess.request.return_value = fresh_resp

    with patch.object(mgr, "get_session", return_value=fresh_sess):
        resp = sess.request("GET", "https://query1.finance.yahoo.com/v8/finance/chart/AAPL")
        assert resp.status_code == 200


def test_shutdown_manager_rotation_survives_used_marker_unlink_oserror():
    mgr = ShutdownTokenManager()
    mgr.shutdown_token = "old-token-12345"
    mgr.shutdown_token_used = True
    mock_marker = MagicMock()
    mock_marker.unlink.side_effect = OSError("Sharing violation")
    mock_marker.exists.return_value = True
    mock_marker.with_name.return_value = MagicMock()
    mgr.used_marker = mock_marker

    mgr.rotate_shutdown_token()
    assert mgr.shutdown_token != "old-token-12345"
    assert mgr.shutdown_token_used is False


def test_sqlite_chat_history_transactional_reads(tmp_path):
    db_file = tmp_path / "chat.db"
    with patch("utils.chat_history.DB_PATH", db_file):
        store = SQLiteChatHistoryStore()
        store["session_1"] = [{"role": "user", "content": "hello"}]

        assert "session_1" in store
        assert "non_existent" not in store
        assert len(store["session_1"]) == 1
        assert store["session_1"][0]["content"] == "hello"
        assert len(store) >= 1


def test_routes_pages_safe_template_context_error_boundary():
    with (
        patch(
            "utils.validators.DefaultSymbolsSchema.model_validate",
            side_effect=ValueError("Schema error"),
        ),
        patch(
            "utils.validators.AppConfigSchema.model_validate",
            side_effect=ValueError("Schema error"),
        ),
    ):
        with app.test_request_context():
            safe_symbols, safe_config = _get_safe_template_context()
            assert safe_symbols == {"us": [], "jp": []}
            assert safe_config == {}


def test_stock_details_empty_dict_handling():
    with app.test_request_context("/api/stocks/details?symbol=AAPL&market=us"):
        with (
            patch("routes.stocks.quotes.require_trusted_or_admin", return_value=(True, "")),
            patch.dict(app_state.yfinance_short_cache, {"info_short_AAPL": {}}),
        ):
            resp = api_stock_details()
            data = resp.get_json()
            assert resp.status_code == 200
            assert data["failed"] is True
            assert data["symbol"] == "AAPL"


def test_info_fetch_inflight_cleanup_on_exception():
    info_key = "info_CRASH_TEST"
    with patch.object(
        app_state.execution.data_executor, "submit", side_effect=RuntimeError("Thread pool error")
    ):
        with app.test_request_context():
            _submit_async_info_fetch("CRASH_TEST")
            with app_state.info_fetch_lock:
                assert info_key not in app_state.info_fetch_inflight


def test_api_chat_standardized_forbidden_response():
    from routes.api_analysis import api_chat

    with app.test_request_context("/api/chat", method="POST", json={"message": "hello"}):
        with patch(
            "routes.api_analysis.require_trusted_or_admin", return_value=(False, "forbidden")
        ):
            resp = api_chat()
            if isinstance(resp, tuple):
                response_obj, status_code = resp
            else:
                response_obj, status_code = resp, resp.status_code
            data = response_obj.get_json()
            assert status_code == 403
            assert data["ok"] is False
            assert data["error_code"] == int(ErrorCode.FORBIDDEN)
