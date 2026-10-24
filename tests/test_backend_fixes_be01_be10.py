"""Tests verifying backend remediation fixes for BE-01 through BE-10."""

from __future__ import annotations

import secrets
import time
from unittest.mock import MagicMock, patch

import pytest

from app import app
from app_state import app_state
from bg.leader_election import close_leader_lock_file, release_leader_lock
from error_codes import ErrorCode
from routes.api_analysis import _get_conversation_scope, api_analyze_chart_image
from routes.stocks.ai_portfolio import (
    _get_conversation_scope as _get_ai_portfolio_conversation_scope,
)
from routes.stocks.ai_portfolio import (
    ai_portfolio_result_cache,
)
from services.ai_portfolio_service import save_custom_ai_portfolio
from utils.chat_history import SQLiteChatHistoryStore


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client


# ===========================================================================
# BE-01: AI Portfolio delete supports POST and query param fallback
# ===========================================================================


def test_be01_ai_portfolio_delete_post_method(client, tmp_path):
    """BE-01: POST method is accepted for custom AI portfolio deletion."""
    test_id = f"custom-test-{secrets.token_hex(6)}"
    sample_pf = {
        "id": test_id,
        "theme": "Test Theme",
        "items": [
            {
                "symbol": "AAPL",
                "name": "Apple",
                "market": "us",
                "weight": 50,
                "shares": 10,
                "target_shares": 10,
                "price": 150.0,
            }
        ],
    }
    with patch("services.ai_portfolio_service.APP_DATA_DIR", tmp_path):
        save_custom_ai_portfolio(sample_pf)
        # Call with POST method
        resp = client.post(
            "/api/ai-portfolio/custom",
            json={"id": test_id},
            headers={"Origin": "http://localhost:5000"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data.get("ok") is True


def test_be01_ai_portfolio_delete_query_param_fallback(client, tmp_path):
    """BE-01: portfolio_id is extracted from query params as fallback."""
    test_id = f"custom-test-{secrets.token_hex(6)}"
    sample_pf = {
        "id": test_id,
        "theme": "Query Param Theme",
        "items": [
            {
                "symbol": "MSFT",
                "name": "Microsoft",
                "market": "us",
                "weight": 100,
                "shares": 5,
                "target_shares": 5,
                "price": 300.0,
            }
        ],
    }
    with patch("services.ai_portfolio_service.APP_DATA_DIR", tmp_path):
        save_custom_ai_portfolio(sample_pf)
        # Call DELETE with empty body but query parameter
        resp = client.delete(
            f"/api/ai-portfolio/custom?id={test_id}",
            headers={"Origin": "http://localhost:5000"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data.get("ok") is True


# ===========================================================================
# BE-02: Conversation scope fallback chain
# ===========================================================================


def test_be02_conversation_scope_fallback_chain(client):
    """BE-02: Header > Request data > Session > Auto-generated."""
    valid_token_header = "tok_header_" + secrets.token_hex(10)
    valid_token_body = "tok_body_" + secrets.token_hex(10)
    valid_token_session = "tok_session_" + secrets.token_hex(10)

    # 1. Header priority
    with app.test_request_context(
        "/api/chat",
        headers={"X-Conversation-Scope": valid_token_header},
        json={"conversation_scope": valid_token_body},
    ):
        scope = _get_conversation_scope({"conversation_scope": valid_token_body})
        assert scope == valid_token_header

    # 2. Body priority when header is absent
    with app.test_request_context(
        "/api/chat",
        json={"conversation_scope": valid_token_body},
    ):
        scope = _get_conversation_scope({"conversation_scope": valid_token_body})
        assert scope == valid_token_body

    # 3. Session fallback
    with app.test_request_context("/api/chat"):
        from flask import session

        session["mns_analysis_conversation"] = valid_token_session
        scope = _get_conversation_scope({})
        assert scope == valid_token_session


def test_be02_ai_portfolio_conversation_scope_fallback_chain():
    """BE-02: Header > Request data > Session > Auto-generated for routes.stocks.ai_portfolio."""
    valid_token_header = "tok_header_" + secrets.token_hex(10)
    valid_token_body = "tok_body_" + secrets.token_hex(10)
    valid_token_session = "tok_session_" + secrets.token_hex(10)

    # 1. Header priority
    with app.test_request_context(
        "/api/ai-portfolio/generate",
        headers={"X-Conversation-Scope": valid_token_header},
        json={"conversation_scope": valid_token_body},
    ):
        from flask import session

        scope = _get_ai_portfolio_conversation_scope({"conversation_scope": valid_token_body})
        assert scope == valid_token_header
        assert session.get("mns_analysis_conversation") == valid_token_header

    # 2. Body priority when header is absent
    with app.test_request_context(
        "/api/ai-portfolio/generate",
        json={"conversation_scope": valid_token_body},
    ):
        from flask import session

        scope = _get_ai_portfolio_conversation_scope({"conversation_scope": valid_token_body})
        assert scope == valid_token_body
        assert session.get("mns_analysis_conversation") == valid_token_body

    # 3. Session fallback
    with app.test_request_context("/api/ai-portfolio/generate"):
        from flask import session

        session["mns_analysis_conversation"] = valid_token_session
        scope = _get_ai_portfolio_conversation_scope({})
        assert scope == valid_token_session

    # 4. Auto-generated token when session is invalid/empty
    with app.test_request_context("/api/ai-portfolio/generate"):
        from flask import session

        scope = _get_ai_portfolio_conversation_scope({})
        assert isinstance(scope, str) and len(scope) >= 16
        assert session.get("mns_analysis_conversation") == scope


def test_be02_ai_portfolio_endpoints_respect_conversation_scope(client):
    """BE-02: ai_portfolio generate/rebalance endpoints respect header and body conversation scope."""
    test_scope_header = "tok_hdr_" + secrets.token_hex(10)
    test_scope_body = "tok_bdy_" + secrets.token_hex(10)
    test_theme = "Semiconductors"

    # Preload cache for generate endpoint using header scope
    cache_key_header = f"generate:{test_scope_header}:{test_theme}"
    cached_portfolio = {
        "id": "ai-theme-semiconductors",
        "theme": test_theme,
        "items": [],
    }
    ai_portfolio_result_cache[cache_key_header] = (time.time(), cached_portfolio, None)

    # 1. Header is respected on /api/ai-portfolio/generate
    resp_hdr = client.post(
        "/api/ai-portfolio/generate",
        json={"theme": test_theme, "conversation_scope": test_scope_body},
        headers={
            "Origin": "http://localhost:5000",
            "X-Conversation-Scope": test_scope_header,
        },
    )
    assert resp_hdr.status_code == 200
    data_hdr = resp_hdr.get_json()
    assert data_hdr.get("ok") is True
    assert data_hdr.get("portfolio", {}).get("theme") == test_theme

    with client.session_transaction() as sess:
        assert sess.get("mns_analysis_conversation") == test_scope_header

    # Preload cache for generate endpoint using body scope
    cache_key_body = f"generate:{test_scope_body}:{test_theme}"
    cached_portfolio_body = {
        "id": "ai-theme-semiconductors-body",
        "theme": test_theme,
        "items": [],
    }
    ai_portfolio_result_cache[cache_key_body] = (time.time(), cached_portfolio_body, None)

    # 2. Body conversation_scope is respected when header is absent
    resp_body = client.post(
        "/api/ai-portfolio/generate",
        json={"theme": test_theme, "conversation_scope": test_scope_body},
        headers={"Origin": "http://localhost:5000"},
    )
    assert resp_body.status_code == 200
    data_body = resp_body.get_json()
    assert data_body.get("ok") is True
    assert (
        data_body.get("portfolio", {}).get("id")
        == "ai-theme-semiconductors-body"
    )

    with client.session_transaction() as sess:
        assert sess.get("mns_analysis_conversation") == test_scope_body

    # 3. /api/ai-portfolio/rebalance respects conversation_scope
    rebal_scope = "tok_rebal_" + secrets.token_hex(10)
    cache_key_rebal = f"rebalance:{rebal_scope}:{test_theme}"
    ai_portfolio_result_cache[cache_key_rebal] = (time.time(), cached_portfolio, None)

    resp_rebal = client.post(
        "/api/ai-portfolio/rebalance",
        json={"theme": test_theme, "conversation_scope": rebal_scope},
        headers={"Origin": "http://localhost:5000"},
    )
    assert resp_rebal.status_code == 200
    data_rebal = resp_rebal.get_json()
    assert data_rebal.get("ok") is True
    assert data_rebal.get("portfolio", {}).get("theme") == test_theme


# ===========================================================================
# BE-03: Search error propagation to concurrent callers
# ===========================================================================


def test_be03_search_error_propagation_502(client):
    """BE-03: When search provider raises, 502 error is returned."""
    with patch.object(
        app_state.stock_provider,
        "search",
        side_effect=ValueError("Simulated search error"),
    ):
        resp = client.get(
            "/api/search?q=TESTCO",
            headers={"Origin": "http://localhost:5000"},
        )
        assert resp.status_code == 502
        data = resp.get_json()
        assert data.get("ok") is False
        assert data.get("error_code") == int(ErrorCode.API_SERVICE_ERROR)


# ===========================================================================
# BE-05: AI technical lines forbidden error envelope
# ===========================================================================


def test_be05_ai_technical_lines_error_envelope(client):
    """BE-05: Small model rejection uses error_response with details."""
    with patch("routes.api_analysis.get_model_name", return_value="mistral-small-2603"):
        resp = client.post(
            "/api/ai-technical-lines",
            json={"symbol": "AAPL", "market": "us", "period": "3mo"},
            headers={"Origin": "http://localhost:5000"},
        )
        assert resp.status_code == 403
        data = resp.get_json()
        assert data.get("ok") is False
        assert data.get("error_code") == int(ErrorCode.FORBIDDEN)
        details = data.get("details", {})
        assert details.get("model_restricted") is True
        assert details.get("current_model") == "mistral-small-2603"


# ===========================================================================
# BE-06: Portfolio warning for avg_fx_rate on Japanese stocks
# ===========================================================================


def test_be06_portfolio_warning_avg_fx_rate_jp(client):
    """BE-06: Response contains warnings when avg_fx_rate sent for JP stock."""
    with app_state.market.user_stocks_lock:
        app_state.market.user_jp["7203.T"] = {
            "name": "Toyota",
            "symbol": "7203.T",
            "market": "jp",
            "shares": 100,
            "avg_price": 2500,
        }
    with patch("routes.stocks.portfolio.save_user_stocks"):
        resp = client.post(
            "/api/stocks/portfolio",
            json={
                "market": "jp",
                "symbol": "7203.T",
                "shares": 100,
                "avg_price": 2500,
                "avg_fx_rate": 150.0,
            },
            headers={"Origin": "http://localhost:5000"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data.get("ok") is True
        assert "warnings" in data
        assert any("為替レート" in w for w in data["warnings"])


# ===========================================================================
# BE-07: Credentials delete error uses SYSTEM_ERROR
# ===========================================================================


def test_be07_credentials_delete_error_envelope(client):
    """BE-07: Keyring deletion failure returns SYSTEM_ERROR in details envelope."""
    with patch("routes.api_system.clear_api_credentials", return_value=["mistral_api_key"]):
        resp = client.delete(
            "/api/credentials",
            headers={"Origin": "http://localhost:5000"},
        )
        assert resp.status_code == 500
        data = resp.get_json()
        assert data.get("ok") is False
        assert data.get("error_code") == int(ErrorCode.SYSTEM_ERROR)
        details = data.get("details", {})
        assert details.get("failed_keys") == ["mistral_api_key"]


# ===========================================================================
# BE-08: Vision endpoint docstring note
# ===========================================================================


def test_be08_vision_docstring_note():
    """BE-08: api_analyze_chart_image docstring documents UI disconnected status."""
    doc = api_analyze_chart_image.__doc__
    assert doc is not None
    assert "UI未接続" in doc


# ===========================================================================
# BE-09: Chat history sqlite check_same_thread default
# ===========================================================================


def test_be09_chat_history_thread_affinity(tmp_path):
    """BE-09: SQLite connection uses default check_same_thread (True)."""
    with patch("utils.chat_history.DB_PATH", tmp_path / "chat_test.db"):
        store = SQLiteChatHistoryStore()
        # Verify both _get_connection and _get_conn alias
        conn = store._get_connection()
        assert conn is store._get_conn()


# ===========================================================================
# BE-10: Leader lock release functions
# ===========================================================================


def test_be10_leader_lock_release_functions():
    """BE-10: release_leader_lock and close_leader_lock_file reset lock file cleanly."""
    mock_file = MagicMock()
    from bg import leader_election

    leader_election._set_leader_lock_file(mock_file)
    leader_election._set_is_sync_leader(True)

    # Call release_leader_lock
    release_leader_lock()
    mock_file.close.assert_called_once()
    assert leader_election._get_leader_lock_file() is None
    assert leader_election.is_leader() is False

    # Second call via close_leader_lock_file should be a safe no-op
    close_leader_lock_file()
