"""Regression and audit verification tests for 2026 comprehensive code review fixes (R1-R14)."""

import math
import sqlite3
import threading
import time
from unittest.mock import MagicMock, patch

import pandas as pd
from flask import Flask, jsonify, request

from error_codes import ErrorCode
from error_handlers import _build_error_response
from route_helpers import (
    _RATE_LIMIT_MAX_ENTRIES,
    _rate_limit_distinct_token_counts,
    _rate_limit_lock,
    _rate_limit_store,
    _rate_limit_window_by_key,
    rate_limit,
)
from services.ai_portfolio_service import sanitize_ai_portfolio
from services.ai_service import call_mistral_chat
from services.realtime_engine import TradingViewWSClient
from services.search.ddgs import ddgs_text_search
from services.stock_provider import YFinanceProvider
from utils.chat_history import SQLiteChatHistoryStore
from utils.stock_payload import error_response
from utils.text_utils import _parse_json_request


# ---------------------------------------------------------------------------
# R1: Distinct-Token Counter Key in Rate Limiting Proactive Eviction
# ---------------------------------------------------------------------------
def test_r1_rate_limit_proactive_eviction_distinct_key():
    """Verify that proactive eviction correctly maintains the :distinct suffix in token counts."""
    app = Flask(__name__)

    @app.route("/test-r1", methods=["POST"])
    @rate_limit(max_requests=100, window_seconds=60)
    def _endpoint():
        return jsonify({"ok": True})

    with _rate_limit_lock:
        _rate_limit_store.clear()
        _rate_limit_distinct_token_counts.clear()
        _rate_limit_window_by_key.clear()

        # Simulate filling the store up to _RATE_LIMIT_MAX_ENTRIES with token keys
        now = time.time()
        for i in range(_RATE_LIMIT_MAX_ENTRIES):
            k = f"127.0.0.1:_endpoint:token:token_{i:04d}"
            _rate_limit_store[k] = [now - 10]
            _rate_limit_window_by_key[k] = 60

    client = app.test_client()
    # Trigger a request with a new distinct token to force proactive eviction
    resp = client.post("/test-r1", json={"request_token": "a" * 32})
    assert resp.status_code == 200

    with _rate_limit_lock:
        # Check that keys in _rate_limit_distinct_token_counts have the :distinct suffix
        for count_key in _rate_limit_distinct_token_counts:
            assert count_key.endswith(":distinct"), f"Key {count_key} is missing :distinct suffix"
        _rate_limit_store.clear()
        _rate_limit_distinct_token_counts.clear()
        _rate_limit_window_by_key.clear()


# ---------------------------------------------------------------------------
# R2: Server-Side Auto-Fetch in /api/ai-technical-lines
# ---------------------------------------------------------------------------
def test_r2_ai_technical_lines_auto_fetch():
    """Verify /api/ai-technical-lines correctly falls back to ohlc_data / chart_data."""
    from routes.api_analysis import api_analysis_bp

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["WTF_CSRF_ENABLED"] = False
    app.register_blueprint(api_analysis_bp)

    dummy_ohlc = [
        {
            "time": 1700000000 + i * 86400,
            "open": 100 + i,
            "high": 105 + i,
            "low": 99 + i,
            "close": 102 + i,
            "volume": 1000,
        }
        for i in range(10)
    ]
    mock_stock = {"ohlc_data": dummy_ohlc, "regularMarketPrice": 110.0}

    with (
        patch("routes.api_analysis.extract_api_key", return_value="test-mistral-key"),
        patch("routes.api_analysis.get_model_name", return_value="mistral-large-2512"),
        patch("routes.api_analysis.is_medium_or_large_model", return_value=True),
        patch("routes.api_analysis.fetch_stock", return_value=mock_stock),
        patch("routes.api_analysis.generate_ai_technical_lines") as mock_gen,
    ):
        mock_gen.return_value = {
            "symbol": "7203",
            "market": "jp",
            "period": "1mo",
            "technical_lines": [],
            "ai_analysis": "test",
        }
        client = app.test_client()
        resp = client.post(
            "/api/ai-technical-lines",
            json={"symbol": "7203", "market": "jp", "period": "1mo"},
            headers={"Origin": "http://localhost:5000"},
        )
        assert resp.status_code == 200
        # Verify generate_ai_technical_lines was called with dummy_ohlc, not an empty list
        mock_gen.assert_called_once()
        args = mock_gen.call_args[0]
        assert args[4] == dummy_ohlc


# ---------------------------------------------------------------------------
# R3: call_mistral_chat with None or Invalid API Key
# ---------------------------------------------------------------------------
def test_r3_call_mistral_chat_none_api_key():
    """Verify call_mistral_chat returns an error dict gracefully when api_key is None."""
    res = call_mistral_chat(api_key=None, messages=[{"role": "user", "content": "hello"}])
    assert isinstance(res, dict)
    assert "error" in res
    assert "missing or invalid" in res["error"]["message"].lower()

    res2 = call_mistral_chat(api_key="", messages=[{"role": "user", "content": "hello"}])
    assert isinstance(res2, dict)
    assert "error" in res2


# ---------------------------------------------------------------------------
# R4: Thread-Safe WebSocket Frame Transmission
# ---------------------------------------------------------------------------
def test_r4_tradingview_ws_safe_send():
    """Verify TradingViewWSClient synchronizes send calls using _send_lock."""
    client = TradingViewWSClient(symbols=["AAPL"])
    assert hasattr(client, "_send_lock")
    assert isinstance(client._send_lock, type(threading.Lock()))

    mock_ws = MagicMock()
    success = client._safe_send(mock_ws, "~m~10~m~test")
    assert success is True
    mock_ws.send.assert_called_once_with("~m~10~m~test")

    # None ws returns False safely
    assert client._safe_send(None, "msg") is False


# ---------------------------------------------------------------------------
# R5: _parse_json_request Chunked Handling
# ---------------------------------------------------------------------------
def test_r5_parse_json_request_chunked():
    """Verify _parse_json_request correctly parses JSON with chunked / None content_length."""
    app = Flask(__name__)

    with app.test_request_context(
        "/api/test",
        method="POST",
        json={"symbol": "AAPL", "market": "us"},
        content_type="application/json",
    ):
        with patch.object(request, "content_length", None):
            parsed = _parse_json_request()
            assert parsed == {"symbol": "AAPL", "market": "us"}


# ---------------------------------------------------------------------------
# R6: start_backend Breakaway Flag Fallback
# ---------------------------------------------------------------------------
def test_r6_start_backend_breakaway_fallback():
    """Verify start_backend retries without CREATE_BREAKAWAY_FROM_JOB on OSError."""
    from native_host import start_backend

    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_popen.side_effect = [PermissionError("[WinError 5] Access denied"), mock_proc]

        mock_pid = MagicMock()
        mock_pid.exists.return_value = False
        mock_pid.with_suffix.return_value = MagicMock()

        mock_legacy = MagicMock()
        mock_legacy.exists.return_value = False

        with (
            patch("native_host.start_backend.is_port_in_use", return_value=False),
            patch("native_host.start_backend.is_running", return_value=True),
            patch("native_host.start_backend.PID_FILE", mock_pid),
            patch("native_host.start_backend._LEGACY_PID_FILE", mock_legacy),
            patch("native_host.start_backend.APP") as mock_app,
            patch("native_host.start_backend.sys.executable", "python.exe"),
            patch("os.replace"),
            # _start() polls a real HTTP health endpoint after spawning the
            # (mocked) process; short-circuit it so the test does not block on
            # network timeouts (~2s+). The breakaway-flag fallback under test is
            # unaffected — it happens before the health probe.
            patch("native_host.start_backend.wait_for_backend_ready", return_value=False),
        ):
            mock_app.exists.return_value = True
            res = start_backend._start()
            assert res["ok"] is True
            assert mock_popen.call_count == 2


# ---------------------------------------------------------------------------
# R7: sanitize_ai_portfolio 100% Weight Invariant
# ---------------------------------------------------------------------------
def test_r7_sanitize_ai_portfolio_weight_invariant():
    """Verify sanitize_ai_portfolio guarantees the weights of positive items sum to 100.0%."""
    raw_portfolio = {
        "theme": "test",
        "items": [
            {"symbol": "AAPL", "market": "us", "weight_pct": 0.0, "target_price": 150},
            {"symbol": "MSFT", "market": "us", "weight_pct": 50.0, "target_price": 300},
            {"symbol": "NVDA", "market": "us", "weight_pct": 50.0, "target_price": 500},
        ],
    }
    sanitized = sanitize_ai_portfolio(raw_portfolio)
    assert len(sanitized["items"]) == 2
    total_w = sum(it["weight_pct"] for it in sanitized["items"])
    assert math.isclose(total_w, 100.0, abs_tol=1e-4)


# ---------------------------------------------------------------------------
# R8: _df_to_records Masks inf and -inf
# ---------------------------------------------------------------------------
def test_r8_df_to_records_masks_inf():
    """Verify YFinanceProvider._df_to_records masks inf and -inf floats to None."""
    provider = YFinanceProvider()
    df = pd.DataFrame(
        {
            "metric": ["val1", "val2", "val3"],
            "pe_ratio": [15.5, float("inf"), float("-inf")],
        }
    )
    records = provider._df_to_records(df)
    assert len(records) == 3
    assert records[0]["pe_ratio"] == 15.5
    assert records[1]["pe_ratio"] is None
    assert records[2]["pe_ratio"] is None


# ---------------------------------------------------------------------------
# R9: SQLite Chat History Transaction Rollback
# ---------------------------------------------------------------------------
def test_r9_chat_history_transaction_rollback():
    """Verify move_to_end and clear execute in transaction with automatic rollback."""
    store = SQLiteChatHistoryStore()

    # Insert a dummy session
    store.move_to_end("session_1")
    assert len(store) >= 1

    # Simulate operational error inside _execute_in_transaction callback
    with patch.object(
        store, "_execute_in_transaction", side_effect=sqlite3.OperationalError("locked")
    ):
        # Must catch cleanly and not raise unhandled exception
        store.move_to_end("session_2")
        store.clear()

    store.close()


# ---------------------------------------------------------------------------
# R10: CSP Report Standard Browser Format Unwrapping
# ---------------------------------------------------------------------------
def test_r10_api_csp_report_unwraps_csp_report_key():
    """Verify /api/csp-report unwraps standard {'csp-report': {...}} payload."""
    from routes.api_system import api_system_bp

    app = Flask(__name__)
    app.register_blueprint(api_system_bp)
    client = app.test_client()

    standard_csp_payload = {
        "csp-report": {
            "document-uri": "http://localhost:5000/",
            "blocked-uri": "http://evil.com/script.js",
            "violated-directive": "script-src",
        }
    }
    resp = client.post("/api/csp-report", json=standard_csp_payload)
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# R12: ddgs_text_search Exception Resilience
# ---------------------------------------------------------------------------
def test_r12_ddgs_text_search_exception_resilience():
    """Verify ddgs_text_search returns [] on any arbitrary exception."""
    with patch("services.search.ddgs.DDGS") as mock_ddgs:
        mock_ddgs.side_effect = RuntimeError("DDGS internal unexpected failure")
        res = ddgs_text_search("test query")
        assert res == []


# ---------------------------------------------------------------------------
# R13/R14: Error Response Code Serialization Consistency
# ---------------------------------------------------------------------------
def test_r13_r14_error_code_serialization_consistency():
    """Verify error_response and _build_error_response serialize ErrorCode consistently."""
    app = Flask(__name__)
    with app.test_request_context():
        # error_response with ErrorCode enum
        resp, _status = error_response(ErrorCode.BAD_REQUEST, status_code=400)
        data = resp.get_json()
        assert data["code"] == str(ErrorCode.BAD_REQUEST)
        assert data["error_code"] == 1400

        # _build_error_response with ErrorCode enum
        resp2, _status2 = _build_error_response(
            "custom message", 400, error_code=ErrorCode.INVALID_INPUT
        )
        data2 = resp2.get_json()
        assert data2["code"] == str(ErrorCode.INVALID_INPUT)
        assert data2["error_code"] == 1005

        # _build_error_response with string error code
        resp3, _status3 = _build_error_response("custom message", 400, error_code="CUSTOM_CODE")
        data3 = resp3.get_json()
        assert data3["code"] == "CUSTOM_CODE"
