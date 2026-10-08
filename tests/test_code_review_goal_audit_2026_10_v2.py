"""tests/test_code_review_goal_audit_2026_10_v2.py

Comprehensive regression tests covering:
1. Early excepthook isolation and exc_type=None handling.
2. Alphanumeric Japanese stock code handling across normalization, AI tools, and screener.
3. TradingView symbol prefixing without double-prefixing and support for BZX/EDGX.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import _early_excepthook
from services.ai_tools import _normalize_market_symbol
from utils.normalization import is_jp_stock_code, normalize_symbol_for_market
from utils.tradingview_mapper import (
    get_tradingview_symbol_meta,
    resolve_exchange_prefix,
)


def test_early_excepthook_none_type_does_not_write(tmp_path: Path):
    """Ensure calling _early_excepthook with None, None, None does not write log entries."""
    with patch.dict("os.environ", {"MNS_DATA_DIR": str(tmp_path)}):
        mock_orig = MagicMock()
        with patch.object(sys, "__excepthook__", mock_orig):
            _early_excepthook(None, None, None)
        mock_orig.assert_called_once_with(None, None, None)
        assert not (tmp_path / "backend.log").exists()
        assert not (tmp_path / "error.log").exists()


def test_early_excepthook_override_does_not_touch_workspace(tmp_path: Path):
    """Ensure data_dir_override does not leak log files into the project base_dir."""
    project_root = Path(__file__).resolve().parent.parent
    backend_log = project_root / "backend.log"
    before_stat = backend_log.stat().st_mtime if backend_log.exists() else None

    with patch.dict("os.environ", {"MNS_LOG_DIR": str(tmp_path)}):
        mock_orig = MagicMock()
        with patch.object(sys, "__excepthook__", mock_orig):
            try:
                raise RuntimeError("Test isolated error")
            except RuntimeError:
                exc_type, exc_val, tb = sys.exc_info()
                _early_excepthook(exc_type, exc_val, tb)

        assert (tmp_path / "backend.log").exists()
        assert (tmp_path / "error.log").exists()
        content = (tmp_path / "backend.log").read_text(encoding="utf-8")
        assert "Test isolated error" in content

    if backend_log.exists() and before_stat is not None:
        after_stat = backend_log.stat().st_mtime
        assert before_stat == after_stat, "base_dir backend.log should not be modified during test"


def test_is_jp_stock_code_and_normalization():
    """Verify 4-digit numeric and 4-char alphanumeric JP stock codes are recognized."""
    # Traditional numeric
    assert is_jp_stock_code("7203") is True
    assert is_jp_stock_code("9984") is True
    # 2024+ alphanumeric JP codes
    assert is_jp_stock_code("130A") is True
    assert is_jp_stock_code("5595") is True
    assert is_jp_stock_code("141A") is True
    # Non-JP or invalid
    assert is_jp_stock_code("AAPL") is False
    assert is_jp_stock_code("^N225") is False
    assert is_jp_stock_code("") is False
    assert is_jp_stock_code(None) is False

    # Normalization with market='jp'
    assert normalize_symbol_for_market("7203", "jp") == "7203.T"
    assert normalize_symbol_for_market("130A", "jp") == "130A.T"
    assert normalize_symbol_for_market("7203.T", "jp") == "7203.T"
    assert normalize_symbol_for_market("130A.T", "jp") == "130A.T"
    assert normalize_symbol_for_market("AAPL", "us") == "AAPL"


def test_tradingview_mapper_no_double_prefix():
    """Verify tickers that already contain exchange colons are not double-prefixed."""
    sym, is_fb, pfx = get_tradingview_symbol_meta("TSE:7203")
    assert sym == "TSE:7203"
    assert is_fb is False
    assert pfx == "TSE"

    sym_nasdaq, is_fb_n, pfx_n = get_tradingview_symbol_meta("NASDAQ:AAPL")
    assert sym_nasdaq == "NASDAQ:AAPL"
    assert is_fb_n is False
    assert pfx_n == "NASDAQ"

    sym_forex, is_fb_f, pfx_f = get_tradingview_symbol_meta("FOREXCOM:SPXUSD")
    assert sym_forex == "FOREXCOM:SPXUSD"
    assert is_fb_f is False
    assert pfx_f == "FOREXCOM"


def test_tradingview_mapper_jp_stock_code():
    """Verify Japanese 4-char stock codes map to TSE: without .T suffix."""
    sym, is_fb, pfx = get_tradingview_symbol_meta("7203")
    assert sym == "TSE:7203"
    assert is_fb is False
    assert pfx == "TSE"

    sym_alpha, is_fb_a, pfx_a = get_tradingview_symbol_meta("130A")
    assert sym_alpha == "TSE:130A"
    assert is_fb_a is False
    assert pfx_a == "TSE"

    sym_t, is_fb_t, pfx_t = get_tradingview_symbol_meta("7203.T")
    assert sym_t == "TSE:7203"
    assert is_fb_t is False
    assert pfx_t == "TSE"


def test_tradingview_mapper_cboe_aliases():
    """Verify CBOE / BATS exchange aliases resolve properly."""
    assert resolve_exchange_prefix("BZX") == "BATS"
    assert resolve_exchange_prefix("BYX") == "BATS"
    assert resolve_exchange_prefix("EDGA") == "BATS"
    assert resolve_exchange_prefix("EDGX") == "BATS"


def test_ai_tools_normalize_market_symbol():
    """Verify _normalize_market_symbol properly infers JP market for alphanumeric symbols."""
    sym, mkt = _normalize_market_symbol({"symbol": "130A"})
    assert sym == "130A.T"
    assert mkt == "jp"

    sym2, mkt2 = _normalize_market_symbol({"symbol": "7203"})
    assert sym2 == "7203.T"
    assert mkt2 == "jp"

    sym3, mkt3 = _normalize_market_symbol({"symbol": "AAPL"})
    assert sym3 == "AAPL"
    assert mkt3 == "us"
