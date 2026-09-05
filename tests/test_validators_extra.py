"""Coverage-focused tests for utils/validators.py edge cases."""

import logging

import utils.validators as v

_PROVIDER_ERROR_MESSAGE = "(AIサービスから有効な応答を取得できませんでした)"


def test_extract_chat_content_empty():
    assert v.extract_chat_content(None) == "応答が空です"
    assert v.extract_chat_content("") == "応答が空です"


def test_extract_chat_content_error_object():
    resp = {"object": "error", "message": "boom"}
    assert v.extract_chat_content(resp) == _PROVIDER_ERROR_MESSAGE


def test_extract_chat_content_error_dict():
    resp = {"error": {"message": "x"}}
    assert v.extract_chat_content(resp) == _PROVIDER_ERROR_MESSAGE


def test_extract_chat_content_error_string():
    resp = {"error": "plain"}
    assert v.extract_chat_content(resp) == _PROVIDER_ERROR_MESSAGE


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
    resp: dict = {"choices": []}
    result = v.extract_chat_content(resp)
    assert result == _PROVIDER_ERROR_MESSAGE


def test_extract_chat_content_redacts_provider_diagnostics(caplog):
    secret = "provider-diagnostic-secret-4921"

    with caplog.at_level(logging.WARNING, logger="utils.validators"):
        error_result = v.extract_chat_content({"error": {"message": secret}})
        malformed_result = v.extract_chat_content({"choices": {secret: "unexpected"}})

    assert error_result == _PROVIDER_ERROR_MESSAGE
    assert malformed_result == _PROVIDER_ERROR_MESSAGE
    assert secret not in caplog.text


def test_safe_parse_analysis_skips_repair_for_provider_error():
    """A provider failure must not trigger another paid repair request."""
    secret = "provider-diagnostic-secret-7845"

    def unexpected_repair(*_args, **_kwargs):
        raise AssertionError("repair must not be called for a provider error")

    result = v.safe_parse_analysis_result(
        {"error": {"message": secret}},
        api_key="test-key",
        repair_func=unexpected_repair,
    )

    assert result["fallback_used"] is True
    assert secret not in str(result)


def test_validate_portfolio_input_fx_rate_too_high():
    errs = v.validate_portfolio_input(1, 100, 10**9)
    assert any("fx_rate" in e for e in errs)


def test_validate_portfolio_input_total_too_high():
    from constants import PORTFOLIO_TOTAL_VALUE_MAX

    huge_shares = PORTFOLIO_TOTAL_VALUE_MAX // 1000 + 1
    errs = v.validate_portfolio_input(huge_shares, 1000)
    assert len(errs) >= 1
