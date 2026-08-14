import re
from unittest.mock import patch

from cryptography.fernet import InvalidToken

from app import create_app
from crypto_utils import protect_data
from error_codes import ErrorCode


# ===========================================================================
# R1: crypto_utils protect_data Keyring / Ephemeral fallback
# ===========================================================================
def test_r1_protect_data_keyring_fallback_on_fernet_error():
    """Test that protect_data accepts keyring scheme when Fernet fails."""
    with patch("cryptography.fernet.Fernet.encrypt", side_effect=InvalidToken("Corrupted key")), \
         patch("crypto_utils.KEYRING_AVAILABLE", True), \
         patch("crypto_utils.keyring.set_password", return_value=None), \
         patch("crypto_utils._is_windows", return_value=False):
        res = protect_data("my_secret_token", "test_key", master_key="dummy_master_key_for_testing_12345678")
        assert isinstance(res, dict)
        assert res.get("scheme") == "keyring"
        assert res.get("value") == ""


def test_r1_protect_data_ephemeral_fallback_on_fernet_error(monkeypatch):
    """Test that protect_data accepts ephemeral scheme when Fernet fails and MNS_EPHEMERAL_FALLBACK=1."""
    monkeypatch.setenv("MNS_EPHEMERAL_FALLBACK", "1")
    with patch("crypto_utils.KEYRING_AVAILABLE", False), \
         patch("crypto_utils._is_windows", return_value=False):
        # Passing an invalid master_key triggers the Fernet exception in protect_data
        res = protect_data("my_secret_token", "test_key", master_key="invalid_base64_not_32_bytes")
        assert isinstance(res, dict)
        assert res.get("scheme") == "ephemeral"
        assert res.get("value") == ""


# ===========================================================================
# R2: routes/api_stocks.py sse_ticket Cookie Secure / Partitioned attributes
# ===========================================================================
def test_r2_sse_ticket_cookie_secure_attributes_in_secure_mode(monkeypatch):
    """Test that POST /api/stocks/stream/ticket adds Secure and Partitioned when MNS_COOKIE_SECURE=1."""
    monkeypatch.setenv("MNS_COOKIE_SECURE", "1")
    app = create_app(skip_bootstrap=True)
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["_sse_sid"] = "test_session_id_12345"

        resp = client.post(
            "/api/stocks/stream/ticket",
            headers={"Origin": "http://127.0.0.1:5000"},
        )
        assert resp.status_code == 200
        cookie_header = resp.headers.get("Set-Cookie", "")
        assert "sse_ticket=" in cookie_header
        assert "Secure" in cookie_header
        assert "Partitioned" in cookie_header
        assert "SameSite=Strict" in cookie_header


def test_r2_sse_ticket_cookie_non_secure_in_dev_mode(monkeypatch):
    """Test that POST /api/stocks/stream/ticket omits Secure in default dev mode."""
    monkeypatch.delenv("MNS_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("MNS_PROD", raising=False)
    app = create_app(skip_bootstrap=True)
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["_sse_sid"] = "test_session_id_dev"

        resp = client.post(
            "/api/stocks/stream/ticket",
            headers={"Origin": "http://127.0.0.1:5000"},
        )
        assert resp.status_code == 200
        cookie_header = resp.headers.get("Set-Cookie", "")
        assert "sse_ticket=" in cookie_header
        # Should not have Secure flag in dev mode
        assert "Secure" not in cookie_header


# ===========================================================================
# R3: routes/api_stocks.py AI portfolio error not cached
# ===========================================================================
def test_r3_ai_portfolio_generate_error_not_cached():
    """Test that an error in generate_ai_portfolio_by_theme is NOT cached for 300 seconds."""
    from routes.api_stocks import ai_portfolio_fetch_lock, ai_portfolio_result_cache

    with ai_portfolio_fetch_lock:
        ai_portfolio_result_cache.clear()

    app = create_app(skip_bootstrap=True)
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as client:
        with patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)):
            # Attempt 1: generate fails
            with patch(
                "routes.api_stocks.generate_ai_portfolio_by_theme",
                side_effect=RuntimeError("Transient LLM 503"),
            ):
                resp1 = client.post(
                    "/api/ai-portfolio/generate",
                    json={"theme": "theme_err_test"},
                    headers={"Origin": "http://127.0.0.1:5000"},
                )
                assert resp1.status_code == 500

            # Verify the error was not stored in the result cache
            with ai_portfolio_fetch_lock:
                assert "generate_theme_err_test" not in ai_portfolio_result_cache

            # Attempt 2: generate succeeds immediately
            mock_success_portfolio = {
                "id": "theme_err_test",
                "name": "Test Portfolio",
                "items": [{"symbol": "AAPL", "market": "us", "target_price": 150.0, "weight_pct": 100.0}],
            }
            with patch(
                "routes.api_stocks.generate_ai_portfolio_by_theme",
                return_value=mock_success_portfolio,
            ):
                resp2 = client.post(
                    "/api/ai-portfolio/generate",
                    json={"theme": "theme_err_test"},
                    headers={"Origin": "http://127.0.0.1:5000"},
                )
                assert resp2.status_code == 200
                data2 = resp2.get_json()
                assert data2.get("ok") is True
                assert data2.get("portfolio") == mock_success_portfolio


def test_r3_ai_portfolio_rebalance_error_not_cached():
    """Test that an error in rebalance is NOT cached for 300 seconds."""
    from routes.api_stocks import ai_portfolio_fetch_lock, ai_portfolio_result_cache

    with ai_portfolio_fetch_lock:
        ai_portfolio_result_cache.clear()

    app = create_app(skip_bootstrap=True)
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as client:
        with patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)):
            # Attempt 1: rebalance fails
            with patch(
                "routes.api_stocks.generate_ai_portfolio_by_theme",
                side_effect=RuntimeError("Rebalance Timeout"),
            ):
                resp1 = client.post(
                    "/api/ai-portfolio/rebalance",
                    json={"theme": "tech"},
                    headers={"Origin": "http://127.0.0.1:5000"},
                )
                assert resp1.status_code == 500

            # Verify error is not in cache
            with ai_portfolio_fetch_lock:
                assert "rebalance_tech" not in ai_portfolio_result_cache


# ===========================================================================
# R6: routes/api_stocks.py copy-to-my multi-US stock calculation
# ===========================================================================
def test_r6_copy_to_my_us_stocks_calculation():
    """Test that copy-to-my calculates shares for multiple US stocks accurately with single FX lookup."""
    app = create_app(skip_bootstrap=True)
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as client:
        with patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)):
            items = [
                {"symbol": "AAPL", "market": "us", "target_price": 200.0, "weight_pct": 50.0},
                {"symbol": "MSFT", "market": "us", "target_price": 400.0, "weight_pct": 50.0},
            ]

            with patch("routes.api_stocks.get_current_usdjpy_rate", return_value=(150.0, False)) as mock_fx, \
                 patch("routes.api_stocks.save_user_stocks", return_value=None):
                resp = client.post(
                    "/api/ai-portfolio/copy-to-my",
                    json={"items": items},
                    headers={"Origin": "http://127.0.0.1:5000"},
                )
                assert resp.status_code == 200
                data = resp.get_json()
                assert data.get("ok") is True
                assert data.get("added_count") == 2
                # FX should only be queried once
                assert mock_fx.call_count <= 1


# ===========================================================================
# R9: routes/api_analysis.py ai-technical-lines error response format
# ===========================================================================
def test_r9_ai_technical_lines_standard_error_response():
    """Test that /api/ai-technical-lines returns standard error_response format on failure."""
    app = create_app(skip_bootstrap=True)
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as client:
        with patch("routes.api_analysis.require_trusted_or_admin", return_value=(True, None)), \
             patch(
                 "routes.api_analysis.generate_ai_technical_lines",
                 return_value={"error": "LLM Service Unavailable (503)"},
             ), \
             patch("routes.api_analysis.extract_api_key", return_value="mock_api_key"):
            resp = client.post(
                "/api/ai-technical-lines",
                json={"symbol": "AAPL", "market": "us", "period": "1mo"},
                headers={"Origin": "http://127.0.0.1:5000"},
            )
            assert resp.status_code == 500
            data = resp.get_json()
            assert data.get("ok") is False
            assert data.get("error_code") == ErrorCode.INTERNAL_SERVER_ERROR.value
            assert data.get("details", {}).get("reason") == "LLM Service Unavailable (503)"


# ===========================================================================
# R10: native_host/install_host_windows.ps1 Test-SafePath regex
# ===========================================================================
def test_r10_install_host_windows_test_safe_path_regex():
    """Test the Test-SafePath regex for safe paths and unsafe traversal / shell meta-characters."""
    # Pattern used in install_host_windows.ps1
    pattern = re.compile(r"\.\.|[|><&;`\"]")

    # Safe paths
    assert not pattern.search(r"C:\Program Files\Python311\python.exe")
    assert not pattern.search(r"C:\Users\user\AppData\Local\Programs\Python\python.exe")
    assert not pattern.search(r"C:\develop\mistral_nex_stocks\native_host\native_host.py")

    # Unsafe paths
    assert pattern.search(r"..\..\Windows\System32\cmd.exe")
    assert pattern.search(r"C:\Python311\python.exe|calc.exe")
    assert pattern.search(r"C:\Python311\python.exe;calc.exe")
    assert pattern.search(r"C:\Python311\python.exe&calc.exe")
    assert pattern.search(r"C:\Python311\python.exe>out.txt")
    assert pattern.search(r"C:\Python311\python.exe<in.txt")
    assert pattern.search(r'C:\Python311\python.exe"test')
    assert pattern.search(r"C:\Python311\python.exe`test")

