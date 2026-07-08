"""Coverage-focused tests for utils/validators.py edge cases."""
import utils.validators as v


def test_extract_chat_content_empty():
    assert v.extract_chat_content(None) == "応答が空です"
    assert v.extract_chat_content("") == "応答が空です"


def test_extract_chat_content_error_object():
    resp = {"object": "error", "message": "boom"}
    assert v.extract_chat_content(resp) == "boom"


def test_extract_chat_content_error_dict():
    resp = {"error": {"message": "x"}}
    assert v.extract_chat_content(resp) == "x"


def test_extract_chat_content_error_string():
    resp = {"error": "plain"}
    assert v.extract_chat_content(resp) == "plain"


def test_validate_portfolio_input_valid():
    assert v.validate_portfolio_input(10, 100, 150.0) == []


def test_validate_portfolio_input_negative_shares():
    errs = v.validate_portfolio_input(-1, 100)
    assert any("shares" in e for e in errs)


def test_validate_portfolio_input_bool_rejected():
    errs = v.validate_portfolio_input(True, 100)
    assert len(errs) >= 1


def test_validate_portfolio_input_too_large_shares():
    errs = v.validate_portfolio_input(10**12, 100)
    assert any("shares" in e for e in errs)


def test_validate_portfolio_input_non_numeric():
    errs = v.validate_portfolio_input("abc", 100)
    assert len(errs) >= 1


def test_validate_portfolio_input_negative_avg_price():
    errs = v.validate_portfolio_input(1, -5)
    assert any("avg_price" in e for e in errs)


def test_validate_portfolio_input_negative_fx_rate():
    errs = v.validate_portfolio_input(1, 100, -1)
    assert any("fx_rate" in e for e in errs)


def test_validate_portfolio_input_avg_price_too_high():
    from constants import PORTFOLIO_AVG_PRICE_MAX
    errs = v.validate_portfolio_input(1, PORTFOLIO_AVG_PRICE_MAX + 1)
    assert any("avg_price" in e for e in errs)


def test_extract_chat_content_no_choices():
    resp = {"choices": []}
    result = v.extract_chat_content(resp)
    assert "Unexpected" in result


def test_validate_portfolio_input_fx_rate_too_high():
    errs = v.validate_portfolio_input(1, 100, 10**9)
    assert any("fx_rate" in e for e in errs)


def test_validate_portfolio_input_total_too_high():
    from constants import PORTFOLIO_TOTAL_VALUE_MAX
    huge_shares = PORTFOLIO_TOTAL_VALUE_MAX // 1000 + 1
    errs = v.validate_portfolio_input(huge_shares, 1000)
    assert len(errs) >= 1
