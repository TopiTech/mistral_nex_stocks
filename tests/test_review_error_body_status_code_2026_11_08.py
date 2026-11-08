"""Regression tests for untrusted Mistral error-body classification.

The capacity classifier runs inside the SDK exception handler. A non-numeric
``status_code`` in a provider/proxy error body used to raise ``ValueError``
there, escaping the handler as an unhandled 500 instead of the structured
error response (and skipping 429 backoff accounting).
"""

from services.ai_service import _is_mistral_capacity_error


def test_capacity_classifier_accepts_numeric_and_numeric_string_status():
    assert _is_mistral_capacity_error({"error": {"status_code": 429}}) is True
    assert _is_mistral_capacity_error({"error": {"status_code": "429"}}) is True


def test_capacity_classifier_ignores_untrusted_non_numeric_status():
    # Pre-fix this raised ``ValueError: invalid literal for int()``.
    assert _is_mistral_capacity_error({"error": {"status_code": "unavailable"}}) is False
    assert _is_mistral_capacity_error({"error": {"status_code": None}}) is False
    assert _is_mistral_capacity_error({"error": {"status_code": True}}) is False
    assert _is_mistral_capacity_error({"error": {"status_code": ["429"]}}) is False


def test_capacity_classifier_keeps_non_status_signals():
    assert _is_mistral_capacity_error({"error": {"type": "service_tier_capacity_exceeded"}}) is True
    assert _is_mistral_capacity_error({"error": {"code": 3505}}) is True
    assert _is_mistral_capacity_error({"error": "not-an-object"}) is False
    assert _is_mistral_capacity_error(None) is False
