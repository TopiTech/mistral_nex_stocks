"""Coverage-focused tests for utils/normalization.py edge cases."""

import utils.normalization as norm
import pandas as pd


def test_normalize_symbol_variants():
    assert norm.normalize_symbol(" aapl ") == "AAPL"
    assert norm.normalize_symbol(".T") == ".T"
    assert norm.normalize_symbol(None) == ""


def test_normalize_market_default_and_invalid():
    assert norm.normalize_market("us") == "us"
    assert norm.normalize_market(None, default="jp") == "jp"
    assert norm.normalize_market(None) == "us"


def test_normalize_text_strips_and_default():
    assert norm.normalize_text("  hello  ") == "hello"
    assert norm.normalize_text(None) == ""
    assert norm.normalize_text(None, default="x") == "x"


def test_normalize_symbol_for_market_jp_suffix():
    assert norm.normalize_symbol_for_market("6758", "jp") == "6758.T"
    assert norm.normalize_symbol_for_market("AAPL", "us") == "AAPL"


def test_is_valid_symbol_rules():
    assert norm.is_valid_symbol("AAPL") is True
    assert norm.is_valid_symbol("") is False
    assert norm.is_valid_symbol("../etc") is False
    assert norm.is_valid_symbol("A/B") is False


def test_normalize_optional_number_rules():
    assert norm.normalize_optional_number("1.5") == 1.5
    assert norm.normalize_optional_number(None) is None
    assert norm.normalize_optional_number("bad") is None
    assert norm.normalize_optional_number(0) is None
    assert norm.normalize_optional_number(-1) is None


def test_normalize_symbol_non_string():
    assert norm.normalize_symbol(123) == "123"


def test__fmt_edge_cases():
    assert norm._fmt(None) is None
    assert norm._fmt(float("nan")) is None
    assert norm._fmt("abc") is None


def test__fmt_vol_edge_cases():
    assert norm._fmt_vol(None) is None
    assert norm._fmt_vol(float("nan")) is None
    assert norm._fmt_vol("abc") is None


def test_normalize_history_frame_missing_close():
    import pandas as pd

    df = pd.DataFrame({"Open": [1.0]}, index=pd.to_datetime(["2024-01-01"]))
    out = norm.normalize_history_frame(df)
    assert out.empty


def test_normalize_history_frame_empty_and_non_df():
    assert norm.normalize_history_frame(None).empty
    assert norm.normalize_history_frame(pd.DataFrame()).empty
    assert norm.normalize_history_frame({"a": 1}).empty


def test_normalize_history_frame_valid():
    df = pd.DataFrame(
        {"Close": [1.0, 2.0, None]},
        index=pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"]),
    )
    out = norm.normalize_history_frame(df)
    assert not out.empty
    assert len(out) == 2


def test_is_valid_symbol_too_long():
    assert norm.is_valid_symbol("A" * 16) is False


def test_is_valid_symbol_invalid_chars():
    assert norm.is_valid_symbol("A/B") is False


def test_normalize_optional_number_non_positive():
    assert norm.normalize_optional_number(0) is None
    assert norm.normalize_optional_number(-5) is None


def test_normalize_history_frame_non_dataframe():
    assert norm.normalize_history_frame("not a df").empty
