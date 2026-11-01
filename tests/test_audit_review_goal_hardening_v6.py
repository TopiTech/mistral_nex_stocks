"""Tests verifying audit review goal hardening v6 fixes across backend and frontend integration.

Covers:
1. RealtimeMarketEngine._handle_pts_update decimal rounding, delta calculation from previous close,
   and boolean scalar protection.
2. RealtimeMarketEngine store encapsulation (copy payloads in get_market_deltas and get_pts_deltas),
   and PTS delta change detection on pts_time and change_percent.
3. route_helpers._submit_in_app_context preserves g.request_id in background threads.
4. routes.stocks.stream SSE listener limit error frame envelope consistency with ErrorCode.
5. static/js/api.js sendChat composite stockKey extraction and drawer fallback.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

from flask import Flask, g

from error_codes import ErrorCode
from route_helpers import _submit_in_app_context
from services.realtime.engine import RealtimeMarketEngine

ROOT = Path(__file__).resolve().parent.parent


def test_realtime_pts_handle_update_precision_and_delta():
    """RealtimeMarketEngine._handle_pts_update rounds JPY PTS to 2 decimals,

    calculates change from prev_close when missing/zero, and rejects bool scalars.
    """
    engine = RealtimeMarketEngine()

    # 1. PTS quote with zero change: computes change against previous close
    payload1 = {
        "symbol": "7203.T",
        "price": 2550.5555,
        "change": 0.0,
        "change_percent": 0.0,
    }
    with patch("services.realtime.engine._get_yfinance_previous_close", return_value=2500.0):
        engine._handle_pts_update(payload1)

    assert payload1["price"] == 2550.56
    assert payload1["change"] == round(2550.5555 - 2500.0, 2)
    assert payload1["change_percent"] == round(((2550.5555 - 2500.0) / 2500.0) * 100, 2)
    assert payload1["previous_close"] == 2500.0

    # 2. PTS quote with existing non-zero change: rounds to 2 decimals
    payload2 = {
        "symbol": "6758.T",
        "price": 2800.1234,
        "change": 45.6789,
        "change_percent": 1.657,
    }
    with patch("services.realtime.engine._get_yfinance_previous_close", return_value=2754.44):
        engine._handle_pts_update(payload2)

    assert payload2["price"] == 2800.12
    assert payload2["change"] == 45.68
    assert payload2["change_percent"] == 1.66
    assert payload2["previous_close"] == 2754.44

    # 3. Reject boolean scalars (True / False) in numeric price or change
    payload_bool = {
        "symbol": "9984.T",
        "price": True,
        "change": False,
        "change_percent": False,
    }
    engine._handle_pts_update(payload_bool)
    assert payload_bool["price"] is True  # Unchanged / not converted to float 1.0


def test_realtime_deltas_store_encapsulation_and_pts_diff():
    """RealtimeMarketEngine get_market_deltas and get_pts_deltas must return copies

    and detect PTS changes in pts_time and change_percent.
    """
    engine = RealtimeMarketEngine()
    sym = "7203.T"

    # Seed market store
    engine.market_store[sym] = {
        "symbol": sym,
        "price": 2500.0,
        "change": 10.0,
        "change_percent": 0.4,
        "volume": 1000,
    }
    engine._dirty_symbols.add(sym)

    # Initial get_market_deltas
    deltas = engine.get_market_deltas()
    assert sym in deltas
    assert deltas[sym]["price"] == 2500.0

    # Mutation test: mutating returned dict must not mutate live store (SVC-20)
    deltas[sym]["price"] = 99999.0
    assert engine.market_store[sym]["price"] == 2500.0

    # Seed PTS store
    engine.pts_store[sym] = {
        "symbol": sym,
        "price": 2510.0,
        "volume": 500,
        "change": 20.0,
        "change_percent": 0.8,
        "pts_time": "18:00:00",
        "pts_trading": True,
    }
    engine._dirty_pts_symbols.add(sym)

    # Initial get_pts_deltas
    pts_deltas = engine.get_pts_deltas()
    assert sym in pts_deltas
    assert pts_deltas[sym]["pts_time"] == "18:00:00"

    # Mutation test for PTS store
    pts_deltas[sym]["price"] = 88888.0
    assert engine.pts_store[sym]["price"] == 2510.0

    # PTS delta test: update pts_time without price change (e.g. after-hours match)
    engine.pts_store[sym]["pts_time"] = "18:05:00"
    engine._dirty_pts_symbols.add(sym)

    subsequent_pts_deltas = engine.get_pts_deltas()
    assert sym in subsequent_pts_deltas
    assert subsequent_pts_deltas[sym]["pts_time"] == "18:05:00"

    # PTS delta test: update change_percent
    engine.pts_store[sym]["change_percent"] = 0.82
    engine._dirty_pts_symbols.add(sym)

    pct_pts_deltas = engine.get_pts_deltas()
    assert sym in pct_pts_deltas
    assert pct_pts_deltas[sym]["change_percent"] == 0.82


def test_submit_in_app_context_propagates_request_id():
    """_submit_in_app_context must capture g.request_id and restore it in the worker thread."""
    app = Flask("test_app_context")
    captured_req_id = None
    captured_in_app_context = False

    def job():
        nonlocal captured_req_id, captured_in_app_context
        captured_req_id = getattr(g, "request_id", None)
        captured_in_app_context = True

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        with app.test_request_context("/api/test"):
            g.request_id = "req-audit-v6-trace-12345"
            _submit_in_app_context(executor, job, app=app)

        # Wait for executor worker to finish
        executor.shutdown(wait=True)

        assert captured_in_app_context is True
        assert captured_req_id == "req-audit-v6-trace-12345"
    finally:
        executor.shutdown(wait=False)


def test_sse_error_frame_envelope_consistency():
    """SSE stream error frames must conform to unified ErrorCode envelope format."""
    from routes.stocks.stream import api_stocks_stream

    app = Flask("test_sse_envelope")
    app.secret_key = "test-secret"

    with app.test_request_context("/api/stocks/stream?mode=1"):
        g.request_id = "req-sse-test-429"

        with (
            patch("routes.stocks.stream.require_sse_auth", return_value=(True, "")),
            patch("routes.stocks.stream.app_state") as mock_app_state,
        ):
            mock_limiter = MagicMock()
            mock_reservation = MagicMock()
            mock_limiter.reserve.return_value = mock_reservation
            mock_limiter.listener_count.return_value = 100
            mock_app_state.sse_listener_limiter = mock_limiter

            # Make announcer.listener_context raise RuntimeError("too many SSE connections")
            mock_announcer = MagicMock()
            mock_announcer.listener_context.side_effect = RuntimeError("too many SSE connections")
            mock_app_state.sse_announcer_mode1 = mock_announcer

            response = api_stocks_stream()
            assert response.status_code == 200

            # Consume the generator frames
            lines = [chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk for chunk in response.response]
            joined = "".join(lines)
            assert "event: error" in joined

            # Extract JSON data from "data: {...}"
            for line in joined.splitlines():
                if line.startswith("data: "):
                    payload = json.loads(line[6:].strip())
                    assert payload["ok"] is False
                    assert payload["error"] == "too many SSE connections"
                    assert payload["error_flag"] is True
                    assert payload["error_code"] == int(ErrorCode.TOO_MANY_REQUESTS)
                    assert payload["code"] == str(ErrorCode.TOO_MANY_REQUESTS)
                    assert payload["details"] == {"reason": "too many SSE connections"}
                    break
            else:
                assert False, f"No data line found in error frame: {joined!r}"


def test_frontend_send_chat_symbol_market_extraction_in_api_js():
    """static/js/api.js sendChat must extract symbol and market from composite stockKey

    and fallback to drawerEl.dataset.stockKey.
    """
    api_js_path = ROOT / "static" / "js" / "api.js"
    content = api_js_path.read_text(encoding="utf-8")

    # Verify drawer dataset fallback exists
    assert 'drawerEl = document.getElementById("stock-detail-drawer")' in content
    assert "drawerEl?.dataset?.stockKey" in content

    # Verify composite stockKey colon parsing
    assert 'stockKey.includes(":")' in content
    assert 'stockKey.substring(0, colonIdx)' in content
    assert 'stockKey.substring(colonIdx + 1)' in content

    # Verify JP stock detection fallback
    assert 'resolvedSymbol.endsWith(".T")' in content
    assert r"/^\d{4}$/.test(resolvedSymbol)" in content
