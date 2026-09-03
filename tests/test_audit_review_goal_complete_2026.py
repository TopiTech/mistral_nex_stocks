"""
Tests for comprehensive code review and remediation (2026).

Verifies:
1. _json_safe serialization with NumPy scalars (int64, float64, bool_), arrays, NaN/Inf, pd.NA/NaT.
2. Rejection of NumPy booleans in _finite_or_none, normalize_optional_number, _fmt, _fmt_vol.
3. Pydantic validation rejecting NumPy booleans in portfolio schemas.
4. Safe float conversion in AI tools rejecting NumPy booleans.
5. Frontend indicators, fullscreen modal accessibility, IME composition guards, and heatmap formatCompact.
"""

import json
import subprocess
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from routes.stocks.common import _json_safe
from services.ai_tools import _tool_calculate_technical_levels
from utils.normalization import _fmt, _fmt_vol, normalize_optional_number
from utils.stock_payload import _finite_or_none
from utils.validators import (
    AiPortfolioItemSchema,
    PortfolioInputSchema,
    validate_portfolio_input,
)


def test_json_safe_numpy_scalars_and_arrays():
    """Verify _json_safe serializes NumPy scalars and arrays safely with allow_nan=False."""
    payload = {
        "int64": np.int64(123456789),
        "int32": np.int32(42),
        "float64": np.float64(98.76),
        "nan_np": np.float64("nan"),
        "inf_np": np.float64("inf"),
        "bool_true": np.bool_(True),
        "bool_false": np.bool_(False),
        "native_bool": True,
        "native_int": 10,
        "native_float": 3.14,
        "native_nan": float("nan"),
        "native_inf": float("inf"),
        "pd_na": pd.NA,
        "pd_nat": pd.NaT,
        "arr_1d": np.array([1, 2, 3]),
        "arr_with_nan": np.array([1.5, np.nan, 2.5]),
        "nested_dict": {
            "val": np.int64(999),
            "flag": np.bool_(True),
            "inner_list": [np.float64(1.1), np.float64("nan"), pd.NA],
        },
    }

    safe = _json_safe(payload)

    # Basic assertions on converted primitives
    assert isinstance(safe["int64"], int)
    assert safe["int64"] == 123456789
    assert isinstance(safe["int32"], int)
    assert safe["int32"] == 42
    assert isinstance(safe["float64"], float)
    assert safe["float64"] == 98.76
    assert safe["nan_np"] is None
    assert safe["inf_np"] is None
    assert safe["bool_true"] is True
    assert safe["bool_false"] is False
    assert safe["native_bool"] is True
    assert safe["native_nan"] is None
    assert safe["native_inf"] is None
    assert safe["pd_na"] is None
    assert safe["pd_nat"] is None
    assert safe["arr_1d"] == [1, 2, 3]
    assert safe["arr_with_nan"] == [1.5, None, 2.5]
    assert safe["nested_dict"]["val"] == 999
    assert safe["nested_dict"]["flag"] is True
    assert safe["nested_dict"]["inner_list"] == [1.1, None, None]

    # json.dumps with allow_nan=False MUST succeed without TypeError or ValueError
    serialized = json.dumps(safe, allow_nan=False)
    assert "123456789" in serialized
    assert "null" in serialized
    assert "true" in serialized


def test_finite_or_none_rejects_numpy_booleans():
    """Verify _finite_or_none rejects np.bool_ without coercing to 1.0 or 0.0."""
    assert _finite_or_none(np.bool_(True)) is None
    assert _finite_or_none(np.bool_(False)) is None
    assert _finite_or_none(True) is None
    assert _finite_or_none(False) is None
    assert _finite_or_none(np.float64(50.5)) == 50.5


def test_normalization_helpers_reject_numpy_booleans():
    """Verify normalization functions reject np.bool_ without coercing to 1.0 or 1."""
    assert normalize_optional_number(np.bool_(True)) is None
    assert normalize_optional_number(np.bool_(False)) is None
    assert normalize_optional_number(np.bool_(True), allow_negative=True) is None
    assert normalize_optional_number(np.bool_(False), allow_negative=True) is None

    assert _fmt(np.bool_(True)) is None
    assert _fmt(np.bool_(False)) is None

    assert _fmt_vol(np.bool_(True)) is None
    assert _fmt_vol(np.bool_(False)) is None


def test_portfolio_validators_reject_numpy_booleans():
    """Verify Pydantic portfolio schema rejects np.bool_."""
    with pytest.raises((ValidationError, ValueError)):
        PortfolioInputSchema(
            symbol="AAPL",
            market="us",
            shares=np.bool_(True),  # type: ignore[arg-type]
            avg_price=100.0,
        )

    with pytest.raises((ValidationError, ValueError)):
        PortfolioInputSchema(
            symbol="AAPL",
            market="us",
            shares=10.0,
            avg_price=np.bool_(True),  # type: ignore[arg-type]
        )

    with pytest.raises((ValidationError, ValueError)):
        AiPortfolioItemSchema(
            symbol="AAPL",
            market="us",
            name="Apple",
            reason="Growth",
            weight_pct=np.bool_(True),  # type: ignore[arg-type]
            target_price=200.0,
        )

    errors = validate_portfolio_input(shares=np.bool_(True), avg_price=150.0)
    assert any("sharesは非負の数値である必要があります" in e for e in errors)


def test_ai_tools_safe_float_rejects_numpy_booleans():
    """Verify _safe_float in services/ai_tools.py rejects np.bool_."""
    mock_ticker = MagicMock()
    # Create DataFrame where Close has np.bool_(True) mixed in
    df = pd.DataFrame(
        {
            "Close": [
                100.0,
                102.0,
                101.0,
                105.0,
                np.bool_(True),  # Must be filtered out
                107.0,
                108.0,
            ]
        }
    )
    mock_ticker.history.return_value = df

    with patch("utils.market_utils.safe_get_ticker", return_value=mock_ticker):
        res = _tool_calculate_technical_levels({"symbol": "AAPL", "market": "us", "period": "1mo"})
        assert res.get("status") != "エラー"
        assert res.get("current_price") == 108.0


def test_frontend_accessibility_and_indicators():
    """Execute Node script to verify frontend indicator guards and modal visibility."""
    script = """
    import assert from "node:assert";
    import fs from "node:fs";

    // 1. Verify ui.js openFullscreenChart removes inert and hidden, and adds show
    const uiContent = fs.readFileSync("static/js/ui.js", "utf-8");
    assert(uiContent.includes('modal.removeAttribute("inert")'), "ui.js must remove inert");
    assert(uiContent.includes('modal.setAttribute("aria-hidden", "false")'), "ui.js must set aria-hidden false");
    assert(uiContent.includes('modal.classList.remove("hidden")'), "ui.js must remove hidden class");
    assert(uiContent.includes('modal.classList.add("show")'), "ui.js must add show class");

    // 2. Verify utils.js openModal and closeModal sync hidden
    const utilsContent = fs.readFileSync("static/js/utils.js", "utf-8");
    assert(utilsContent.includes('modal.classList.remove("hidden")'), "utils.js openModal must remove hidden");
    assert(utilsContent.includes('modal.classList.add("hidden")'), "utils.js closeModal must add hidden");
    assert(utilsContent.includes('e.isComposing || e.keyCode === 229'), "utils.js escape handler must guard IME");

    // 3. Verify index_main.js escape and market tabs guard IME
    const indexContent = fs.readFileSync("static/js/index_main.js", "utf-8");
    assert(indexContent.includes('e.isComposing || e.keyCode === 229'), "index_main.js escape handler must guard IME");
    assert(indexContent.includes('event.isComposing || event.keyCode === 229'), "index_main.js tab keydown must guard IME");

    // 4. Verify heatmap.js IME guards and formatCompact
    const heatmapContent = fs.readFileSync("static/js/heatmap.js", "utf-8");
    assert(heatmapContent.includes('const num = Number(value);'), "heatmap.js formatCompact must parse number");
    assert(heatmapContent.includes('if (e.isComposing || e.keyCode === 229) return;'), "heatmap.js node keydown must guard IME");
    assert(heatmapContent.includes('if (event.isComposing || event.keyCode === 229) return;'), "heatmap.js 3D onKeyDown must guard IME");

    // 5. Verify settings.js and ai_portfolio.js IME guards
    const settingsContent = fs.readFileSync("static/js/settings.js", "utf-8");
    assert(settingsContent.includes('if (e.isComposing || e.keyCode === 229) return;'), "settings.js tab keydown must guard IME");
    const portfolioContent = fs.readFileSync("static/js/ai_portfolio.js", "utf-8");
    assert(portfolioContent.includes('if (e.isComposing || e.keyCode === 229) return;'), "ai_portfolio.js tab keydown must guard IME");

    // 6. Verify chart.js indicator calculation functions
    const chartContent = fs.readFileSync("static/js/chart.js", "utf-8");
    assert(/!Array\\.isArray\\(series\\)\\s*\\|\\|\\s*!Number\\.isInteger\\(period\\)/.test(chartContent), "calculateSMA must guard arguments");
    assert(/!Array\\.isArray\\(series\\)/.test(chartContent), "calculateBollingerBands must guard series");
    assert(/!Number\\.isFinite\\(multiplier\\)/.test(chartContent), "calculateBollingerBands must guard multiplier");
    assert(/!Array\\.isArray\\(ohlcData\\)/.test(chartContent), "calculateHeikinAshi must guard arguments");

    console.log("All frontend assertions passed successfully!");
    """
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Frontend verification failed: {proc.stderr}\n{proc.stdout}"
