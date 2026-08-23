"""Regression coverage for the 2026-08-22 remediation pass (R1/R3/R4/R5).

- R1: credential-save failures never expose keyring exception text.
- R3: AI portfolio generate/rebalance result caches are scoped per browser
  session (mirrors routes.api_analysis._get_conversation_scope).
- R4: crypto_utils never logs keyring exception text (may embed the secret).
- R5: AlphaVantage fallback never logs the apikey embedded in requests
  exception text.
"""

import logging
import time
from contextlib import nullcontext
from unittest.mock import patch

import pytest
from flask import Flask

from routes.api_stocks import (
    ai_portfolio_fetch_inflight,
    ai_portfolio_fetch_lock,
    ai_portfolio_result_cache,
    api_stocks_bp,
)


def _make_app():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SECRET_KEY"] = "test-secret-key-32-chars-long-security"
    app.register_blueprint(api_stocks_bp)
    return app


def _clear_portfolio_state():
    with ai_portfolio_fetch_lock:
        ai_portfolio_result_cache.clear()
        ai_portfolio_fetch_inflight.clear()


# ===========================================================================
# R1: credentials API must not re-log keyring exception text
# ===========================================================================
def test_r1_credentials_keyring_error_text_is_not_exposed_or_saved(caplog):
    from app import create_app
    from crypto_utils import KeyringError

    marker = "qz7m5rkp2"

    def assert_marker_absent(text):
        if marker in text:
            raise AssertionError("keyring exception text was exposed")

    app = create_app(skip_bootstrap=True)
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    caplog.set_level(logging.WARNING)

    with (
        app.test_client() as client,
        patch.dict(
            "routes.api_system.os.environ",
            {
                "MNS_ADMIN_TOKEN": "",
                "MNS_ALLOW_REMOTE_API": "0",
                "MNS_EPHEMERAL_FALLBACK": "",
            },
            clear=False,
        ),
        patch("crypto_utils.KEYRING_AVAILABLE", True),
        patch(
            "crypto_utils.keyring.set_password",
            side_effect=KeyringError(marker),
        ),
        patch("crypto_utils._is_windows", return_value=False),
        patch("credential_manager._keyring_available", return_value=False),
        patch(
            "credential_manager.config_store.config_update_lock",
            return_value=nullcontext(),
        ),
        patch(
            "credential_manager.config_store.load_config",
            return_value={"api_credentials": {}},
        ),
        patch("credential_manager.config_store.save_config") as config_save,
    ):
        response = client.post(
            "/api/credentials",
            json={"mistral_api_key": "a" * 40},
            headers={"Origin": "http://localhost:5000"},
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )

    response_text = response.get_data(as_text=True)
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert response.status_code == 500
    assert_marker_absent(response_text)
    assert_marker_absent(log_text)
    config_save.assert_not_called()


# ===========================================================================
# R3: session-scoped AI portfolio cache keys
# ===========================================================================
def test_r3_generate_result_not_shared_across_browser_sessions():
    """A second browser session must not receive the first session's cached generation."""
    _clear_portfolio_state()
    app = _make_app()
    theme = "r3_share_theme"
    portfolio_a = {"id": theme, "version": "A", "items": []}

    with app.test_client() as client:
        with patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)):
            # Session A generates and caches.
            with client.session_transaction() as sess:
                sess["mns_analysis_conversation"] = "scope_r3_session_a_123"
            with patch(
                "routes.api_stocks.generate_ai_portfolio_by_theme",
                return_value=portfolio_a,
            ):
                res_a = client.post("/api/ai-portfolio/generate", json={"theme": theme})
                assert res_a.status_code == 200
                assert res_a.get_json()["portfolio"]["version"] == "A"

            # Session B asks for the same theme within the TTL: must not get A's result.
            with client.session_transaction() as sess:
                sess["mns_analysis_conversation"] = "scope_r3_session_b_456"
            with patch(
                "routes.api_stocks.generate_ai_portfolio_by_theme",
                return_value={"id": theme, "version": "B", "items": []},
            ) as mock_gen_b:
                res_b = client.post("/api/ai-portfolio/generate", json={"theme": theme})
                assert res_b.status_code == 200
                assert res_b.get_json()["portfolio"]["version"] == "B"
                mock_gen_b.assert_called_once()
    _clear_portfolio_state()


def test_r3_same_session_still_hits_cache_within_ttl():
    """Valid behavior retained: the same session re-requesting a theme reuses the cache."""
    _clear_portfolio_state()
    app = _make_app()
    theme = "r3_same_theme"

    with app.test_client() as client:
        with patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)):
            with client.session_transaction() as sess:
                sess["mns_analysis_conversation"] = "scope_r3_same_session_01"
            with patch(
                "routes.api_stocks.generate_ai_portfolio_by_theme",
                return_value={"id": theme, "version": 1, "items": []},
            ) as mock_gen:
                res1 = client.post("/api/ai-portfolio/generate", json={"theme": theme})
                assert res1.get_json()["portfolio"]["version"] == 1
                res2 = client.post("/api/ai-portfolio/generate", json={"theme": theme})
                assert res2.get_json()["portfolio"]["version"] == 1
                assert mock_gen.call_count == 1
    _clear_portfolio_state()


def test_r3_rebalance_cache_scoped_per_session():
    """Rebalance results seeded for session A must not be served to session B."""
    _clear_portfolio_state()
    app = _make_app()
    theme = "r3_rebalance_theme"
    scope_a = "scope_r3_rebal_a_12345678"
    expected = {"id": theme, "status": "rebalanced-A", "items": []}
    with ai_portfolio_fetch_lock:
        ai_portfolio_result_cache[f"rebalance:{scope_a}:{theme}"] = (
            time.time(),
            expected,
            None,
        )

    with app.test_client() as client:
        with patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)):
            # Session B polls: must run its own rebalance instead of consuming A's entry.
            with client.session_transaction() as sess:
                sess["mns_analysis_conversation"] = "scope_r3_rebal_b_65432109"
            with patch(
                "routes.api_stocks.generate_ai_portfolio_by_theme",
                return_value={"id": theme, "status": "rebalanced-B", "items": []},
            ) as mock_gen_b:
                res_b = client.post("/api/ai-portfolio/rebalance", json={"theme": theme})
                assert res_b.status_code == 200
                assert res_b.get_json()["portfolio"]["status"] == "rebalanced-B"
                mock_gen_b.assert_called_once()

            # Session A still gets its own seeded result afterwards.
            with client.session_transaction() as sess:
                sess["mns_analysis_conversation"] = scope_a
            res_a = client.post("/api/ai-portfolio/rebalance", json={"theme": theme})
            assert res_a.get_json()["portfolio"]["status"] == "rebalanced-A"
    _clear_portfolio_state()


# ===========================================================================
# R4: keyring exception text must never reach logs
# ===========================================================================
def test_r4_keyring_failure_log_contains_no_exception_text(caplog):
    from crypto_utils import KeyringError, _encode_secret

    secret_marker = "SUPER_SECRET_VALUE_FROM_KEYRING_ERROR"
    caplog.set_level(logging.WARNING)

    class FakeKeyringError(KeyringError):
        pass

    with patch("crypto_utils.KEYRING_AVAILABLE", True), \
         patch("crypto_utils.keyring.set_password", side_effect=FakeKeyringError(secret_marker)), \
         patch("crypto_utils._is_windows", return_value=False), \
         pytest.raises(RuntimeError):
        _encode_secret("some_api_key", "r4_test_key")

    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "Keyring protection failed" in joined
    assert secret_marker not in joined
    assert "FakeKeyringError" in joined


# ===========================================================================
# R5: AlphaVantage apikey redaction in exception logs
# ===========================================================================
def test_r5_alphavantage_exception_log_redacts_apikey(caplog):
    from services.fallback_provider import AlphaVantageProvider

    class _BoomResponse:
        status_code = 503
        text = ""

        def raise_for_status(self):
            import requests

            raise requests.exceptions.HTTPError("503 Server Error")

    caplog.set_level(logging.DEBUG)
    provider = AlphaVantageProvider()

    import requests

    error = requests.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='www.alphavantage.co', port=443): "
        "Max retries exceeded with url: /query?function=GLOBAL_QUOTE&symbol=AAPL&apikey=LEAKEDKEY123"
    )
    with patch("services.fallback_provider.get_alphavantage_api_key", return_value="LEAKEDKEY123"), \
         patch("requests.get", side_effect=error):
        assert provider.get_latest_quote("AAPL") is None

    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "AlphaVantage fallback failed" in joined
    assert "LEAKEDKEY123" not in joined
    assert "apikey=[REDACTED]" in joined
