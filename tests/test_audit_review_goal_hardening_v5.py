"""Tests verifying audit review goal hardening v5 fixes across backend, frontend, and tooling.

Covers:
1. pyproject.toml configuration including config package in pyrefly and mypy.
2. .github/workflows/ci.yml including config/ and config_store.py in pylint targets.
3. services/realtime/engine._handle_producer_update precision respects market="jp" hint and payload.
4. bg/sync/process._update_indices_data safety net triggers for None, "", "--", or missing prices.
5. routes/stocks/common._json_safe converts dict non-string keys (int, numpy scalar) to strings.
6. static/js/chart.js flushes and unloads clear debounced timers.
7. static/js/ui.js ensureStockDetails clears timeout in finally block.
8. static/js/api.js updateStocksFromSseData null-safety when data.stocks is omitted.
9. static/js/api_client.ts / api_client.js closeSSE resets sseReconnectAttempt to 0.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from unittest.mock import patch

import numpy as np

from bg.sync.process import _update_indices_data
from routes.stocks.common import _json_safe
from services.realtime.engine import RealtimeMarketEngine

ROOT = Path(__file__).resolve().parent.parent


def test_pyproject_pyrefly_includes_config_and_app_factory():
    """ISSUE-01: pyproject.toml must include both app_factory and config in pyrefly project-includes."""
    pyproject_path = ROOT / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    pyrefly_includes = data.get("tool", {}).get("pyrefly", {}).get("project-includes", [])
    assert "app_factory" in pyrefly_includes
    assert "config" in pyrefly_includes
    assert "config_store.py" in pyrefly_includes

    mypy_files = data.get("tool", {}).get("mypy", {}).get("files", [])
    assert "config" in mypy_files
    assert "config_store.py" in mypy_files


def test_ci_workflow_pylint_covers_config_package():
    """ISSUE-09: .github/workflows/ci.yml must include config/ and config_store.py in pylint targets."""
    ci_path = ROOT / ".github" / "workflows" / "ci.yml"
    ci_content = ci_path.read_text(encoding="utf-8")
    pylint_line = next(line for line in ci_content.splitlines() if "pylint --errors-only" in line)

    assert "config/" in pylint_line
    assert "config_store.py" in pylint_line
    assert "app_factory/" in pylint_line
    assert "bg/" in pylint_line


def test_engine_precision_respects_market_hint_and_payload():
    """ISSUE-02: RealtimeMarketEngine._handle_producer_update must round JPY to 2 decimals and US to 4."""
    engine = RealtimeMarketEngine()

    # 1. Non-standard symbol with market="jp" in payload -> decimals=2
    jp_payload = {
        "symbol": "CUSTOMJP",
        "market": "jp",
        "price": 1250.5555,
        "change": 12.3456,
        "change_percent": 1.2345,
    }
    with patch("services.realtime.engine._get_yfinance_previous_close", return_value=1238.21):
        engine._handle_producer_update(jp_payload)
    # price - prev_close = 1250.5555 - 1238.21 = 12.3455 -> round(12.3455, 2) = 12.35
    # change_pct = (12.3455 / 1238.21) * 100 = 0.997... -> round(0.997..., 2) = 1.0
    assert jp_payload["change"] == round(1250.5555 - 1238.21, 2)
    assert jp_payload["change_percent"] == 1.0

    # 2. Standard .T symbol without explicit market key -> decimals=2
    t_payload = {
        "symbol": "7203.T",
        "price": 2500.8888,
        "change": 15.6789,
        "change_percent": 0.63,
    }
    with patch("services.realtime.engine._get_yfinance_previous_close", return_value=2485.2):
        engine._handle_producer_update(t_payload)
    assert t_payload["change"] == round(2500.8888 - 2485.2, 2)

    # 3. US symbol (e.g. AAPL) -> decimals=4
    us_payload = {
        "symbol": "AAPL",
        "market": "us",
        "price": 220.1234,
        "change": 1.23456,
        "change_percent": 0.56,
    }
    with patch("services.realtime.engine._get_yfinance_previous_close", return_value=218.89):
        engine._handle_producer_update(us_payload)
    assert us_payload["change"] == round(220.1234 - 218.89, 4)

    # 4. Registration with market="jp" hints non-standard symbol -> decimals=2
    reg_token = engine._activate_registration("SPECIAL_TICKER", "jp")
    reg_payload = {
        "symbol": "SPECIAL_TICKER",
        "price": 500.1234,
        "change": 5.6789,
        "change_percent": 1.15,
    }
    with patch("services.realtime.engine._get_yfinance_previous_close", return_value=494.44):
        engine._handle_producer_update(reg_payload, registration=reg_token)
    assert reg_payload["change"] == round(500.1234 - 494.44, 2)


def test_update_indices_data_safety_net_triggers_on_none_empty_or_dash():
    """ISSUE-06: _update_indices_data triggers individual fetch for missing, None, '', or '--' prices."""
    # Setup dummy header data with prices requiring individual safety net
    idx_res = [
        {"symbol": "^N225", "price": "--"},
        {"symbol": "^DJI", "price": None},
        {"symbol": "USDJPY=X", "price": ""},
        {"symbol": "EURJPY=X", "price": 160.50},  # valid, should not trigger
    ]

    fetched_symbols: list[str] = []

    def mock_fetch(key: str, sym: str):
        fetched_symbols.append(key)
        return key, {"price": "100.00", "change": "+1.00", "percent": "+1.0%"}

    with (
        patch("bg.sync.process.app_state") as mock_app_state,
        patch("bg.sync.process._get_app_bg_attr", return_value=mock_fetch),
    ):
        mock_app_state.market.is_yf_rate_limited.return_value = False
        _update_indices_data(idx_res, [], [])

    # Since safety net budget is 2, it should have fetched the first 2 defective ones (N225, DJI)
    assert "N225" in fetched_symbols
    assert "DJI" in fetched_symbols
    assert "EURJPY" not in fetched_symbols


def test_json_safe_dict_keys_coerced_to_str():
    """ISSUE-08: _json_safe must convert dict non-string keys (e.g. int, np.int64) to strings."""
    data = {
        100: "hundred",
        np.int64(200): "two hundred",
        "normal_key": {
            np.int32(1): "one",
            "nested": [1, 2, np.float64(3.5)],
        },
    }
    sanitized = _json_safe(data)

    # All keys must be strings
    assert "100" in sanitized
    assert "200" in sanitized
    assert sanitized["100"] == "hundred"
    assert sanitized["200"] == "two hundred"
    assert "1" in sanitized["normal_key"]
    assert sanitized["normal_key"]["1"] == "one"

    # json.dumps must succeed without TypeError
    dumped = json.dumps(sanitized)
    assert '"100": "hundred"' in dumped
    assert '"200": "two hundred"' in dumped


def test_static_js_frontend_patterns():
    """ISSUE-03, ISSUE-04, ISSUE-05, ISSUE-07: Static inspection of JS/TS fixes."""
    chart_js = (ROOT / "static" / "js" / "chart.js").read_text(encoding="utf-8")
    ui_js = (ROOT / "static" / "js" / "ui.js").read_text(encoding="utf-8")
    api_js = (ROOT / "static" / "js" / "api.js").read_text(encoding="utf-8")
    api_client_ts = (ROOT / "static" / "js" / "api_client.ts").read_text(encoding="utf-8")
    api_client_js = (ROOT / "static" / "js" / "api_client.js").read_text(encoding="utf-8")
    realtime_client_js = (ROOT / "static" / "js" / "realtime_client.js").read_text(encoding="utf-8")

    # ISSUE-03: chart.js clears timers on beforeunload
    assert "_chartPrefPersistTimer = null;" in chart_js
    assert "_stockColorsPersistTimer = null;" in chart_js

    # ISSUE-04: ui.js ensureStockDetails has finally block for clearTimeout
    assert "finally {\n    clearTimeout(timeoutId);\n  }" in ui_js.replace("\r\n", "\n")

    # ISSUE-05: api.js updateStocksFromSseData checks data and data.stocks
    assert "const stocks = data.stocks || {};" in api_js or "const stocks = (data && data.stocks) || {};" in api_js

    # ISSUE-07: api_client closeSSE resets sseReconnectAttempt
    assert "this.sseReconnectAttempt = 0;" in api_client_ts
    assert "this.sseReconnectAttempt = 0;" in api_client_js

    # ISSUE-02: realtime_client.js roundChange and isJpSymbol accept market
    assert "function isJpSymbol(symbol, market)" in realtime_client_js
    assert "function roundChange(value, symbol, market)" in realtime_client_js
