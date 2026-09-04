"""Regression tests for symbol normalization fixes (R1/R2).

Covers two confirmed defects in utils/normalization.py:

* R1: ``is_valid_symbol()`` raised ``TypeError`` for non-string values
  (``object of type 'int' has no len()``) instead of returning False.
* R2: fullwidth (NFKC-normalizable) symbols such as ``ＡＡＰＬ`` / ``７２０３``
  passed ``is_valid_symbol()`` (which NFKC-normalizes internally) but were
  stored unnormalized by the ingress path (``normalize_symbol`` did not apply
  NFKC), so the persisted watchlist key could never be resolved by yfinance /
  the realtime providers.
"""

import utils.normalization as norm
from route_helpers import _parse_stock_request

FULLWIDTH_AAPL = "\uff21\uff21\uff30\uff2c"  # ＡＡＰＬ
FULLWIDTH_7203 = "\uff17\uff12\uff10\uff13"  # ７２０３
FULLWIDTH_DOT = "\uff0e"  # ．


# ---------------------------------------------------------------------------
# R1: is_valid_symbol() must return bool and never raise
# ---------------------------------------------------------------------------


def test_is_valid_symbol_non_string_returns_false_or_bool():
    """Non-string values must not raise TypeError (regression for R1)."""
    # int, float, bool, list, dict all previously crashed or coerced
    assert isinstance(norm.is_valid_symbol(12345), bool)
    assert isinstance(norm.is_valid_symbol(1.5), bool)
    assert isinstance(norm.is_valid_symbol(True), bool)
    assert isinstance(norm.is_valid_symbol([]), bool)
    assert isinstance(norm.is_valid_symbol({}), bool)
    assert isinstance(norm.is_valid_symbol(0), bool)
    assert isinstance(norm.is_valid_symbol(b"AAPL"), bool)


def test_is_valid_symbol_int_consistent_with_str_form():
    """An int must behave exactly like its str form (consistency)."""
    assert norm.is_valid_symbol(12345) == norm.is_valid_symbol("12345")
    assert norm.is_valid_symbol(7203) == norm.is_valid_symbol("7203")


def test_is_valid_symbol_none_empty():
    assert norm.is_valid_symbol(None) is False
    assert norm.is_valid_symbol("") is False


def test_is_valid_symbol_ascii_rules_unchanged():
    assert norm.is_valid_symbol("AAPL") is True
    assert norm.is_valid_symbol("BRK.B") is True
    assert norm.is_valid_symbol("7203.T") is True
    assert norm.is_valid_symbol("A" * 16) is False
    assert norm.is_valid_symbol("../etc") is False
    assert norm.is_valid_symbol("A/B") is False
    assert norm.is_valid_symbol("A%20B") is False


# ---------------------------------------------------------------------------
# R2: NFKC normalization at ingress so stored == validated
# ---------------------------------------------------------------------------


def test_normalize_symbol_fullwidth_to_ascii():
    """Fullwidth ticker must collapse to the ASCII form yfinance can resolve."""
    assert norm.normalize_symbol(FULLWIDTH_AAPL) == "AAPL"
    assert norm.normalize_symbol(FULLWIDTH_AAPL.lower()) == "AAPL"


def test_normalize_symbol_fullwidth_jp_digits():
    """Fullwidth JP digits must normalize before the .T suffix is appended."""
    assert norm.normalize_symbol(FULLWIDTH_7203) == "7203"


def test_normalize_symbol_for_market_fullwidth_jp():
    assert norm.normalize_symbol_for_market(FULLWIDTH_7203, "jp") == "7203.T"
    assert norm.normalize_symbol_for_market("7203", "jp") == "7203.T"


def test_normalize_symbol_fullwidth_tv_prefix():
    """Fullwidth TradingView-prefixed input must map to the internal ticker."""
    # ＮＡＳＤＡＱ：ＡＡＰＬ -> "NASDAQ:AAPL" -> "AAPL"
    fullwidth_tv = "\uff2e\uff21\uff33\uff24\uff21\uff31\uff1a" + FULLWIDTH_AAPL
    assert norm.normalize_symbol(fullwidth_tv) == "AAPL"


def test_normalize_symbol_ascii_unchanged():
    assert norm.normalize_symbol("aapl") == "AAPL"
    assert norm.normalize_symbol("BRK.B") == "BRK.B"
    assert norm.normalize_symbol("^GSPC") == "^GSPC"
    assert norm.normalize_symbol(None) == ""
    assert norm.normalize_symbol(123) == "123"


def test_normalize_symbol_fullwidth_dot_ascii():
    """Fullwidth dot (．) inside a symbol must become a plain dot."""
    assert norm.normalize_symbol("ＢＲＫ" + FULLWIDTH_DOT + "Ｂ") == "BRK.B"


# ---------------------------------------------------------------------------
# R2: the ingress path (_parse_stock_request) must persist the normalized form
# ---------------------------------------------------------------------------


def test_parse_stock_request_fullwidth_us_symbol_normalized():
    parsed, error = _parse_stock_request(
        {"symbol": FULLWIDTH_AAPL, "name": "Apple", "market": "us"},
        require_name=True,
    )
    assert error is None
    assert parsed is not None
    assert parsed["symbol"] == "AAPL"
    assert parsed["raw_symbol"] == "AAPL"


def test_parse_stock_request_fullwidth_jp_symbol_normalized():
    parsed, error = _parse_stock_request(
        {"symbol": FULLWIDTH_7203, "name": "Toyota", "market": "jp"},
        require_name=True,
    )
    assert error is None
    assert parsed is not None
    assert parsed["symbol"] == "7203.T"


def test_parse_stock_request_ascii_symbol_unchanged():
    parsed, error = _parse_stock_request(
        {"symbol": "aapl", "name": "Apple", "market": "us"},
        require_name=True,
    )
    assert error is None
    assert parsed is not None
    assert parsed["symbol"] == "AAPL"
