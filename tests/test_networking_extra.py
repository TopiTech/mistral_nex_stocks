"""Additional coverage tests for utils/networking.py.

Tests edge cases not already covered by test_security_hardening.py.
"""

from utils.networking import (
    _is_loopback_ip,
    _normalize_extension_origin,
    mask_sensitive_url,
)


# ---------------------------------------------------------------------------
# _normalize_extension_origin
# ---------------------------------------------------------------------------

def test_normalize_extension_origin_none():
    assert _normalize_extension_origin(None) is None


def test_normalize_extension_origin_empty():
    assert _normalize_extension_origin("") is None


def test_normalize_extension_origin_whitespace():
    assert _normalize_extension_origin("  ") is None


def test_normalize_extension_origin_invalid_chrome():
    # Less than 32 hex chars
    result = _normalize_extension_origin("chrome-extension://tooshort")
    assert result is None


def test_normalize_extension_origin_valid_chrome():
    valid_id = "a" * 32
    result = _normalize_extension_origin(f"chrome-extension://{valid_id}")
    assert result == f"chrome-extension://{valid_id}"


def test_normalize_extension_origin_raw_id():
    valid_id = "b" * 32
    result = _normalize_extension_origin(valid_id)
    assert result == f"chrome-extension://{valid_id}"


def test_normalize_extension_origin_trailing_slash_stripped():
    valid_id = "c" * 32
    result = _normalize_extension_origin(f"chrome-extension://{valid_id}/")
    assert result == f"chrome-extension://{valid_id}"


def test_normalize_extension_origin_random_string():
    """A non-hex string that doesn't match should return None."""
    assert _normalize_extension_origin("not-a-valid-origin") is None


# ---------------------------------------------------------------------------
# _is_loopback_ip
# ---------------------------------------------------------------------------

def test_is_loopback_ip_empty():
    assert not _is_loopback_ip("")


def test_is_loopback_ip_localhost():
    assert _is_loopback_ip("localhost")


def test_is_loopback_ip_localhost_with_port():
    assert _is_loopback_ip("localhost:5000")


def test_is_loopback_ip_127():
    assert _is_loopback_ip("127.0.0.1")


def test_is_loopback_ip_127_with_port():
    assert _is_loopback_ip("127.0.0.1:5000")


def test_is_loopback_ip_ipv6():
    assert _is_loopback_ip("::1")


def test_is_loopback_ip_ipv6_brackets():
    assert _is_loopback_ip("[::1]")


def test_is_loopback_ip_ipv6_brackets_with_port():
    assert _is_loopback_ip("[::1]:5000")


def test_is_loopback_ip_external():
    assert not _is_loopback_ip("192.168.1.1")


def test_is_loopback_ip_external_ipv6():
    assert not _is_loopback_ip("2001:db8::1")


def test_is_loopback_ip_garbage():
    assert not _is_loopback_ip("not-an-ip")


def test_is_loopback_ip_multiple_ports():
    """If ip:port has more than 2 parts, not parsed as ip:port."""
    assert not _is_loopback_ip("127.0.0.1:5000:extra")


# ---------------------------------------------------------------------------
# mask_sensitive_url
# ---------------------------------------------------------------------------

def test_mask_sensitive_url_none():
    assert mask_sensitive_url("") == ""


def test_mask_sensitive_url_no_query():
    assert mask_sensitive_url("/api/stocks") == "/api/stocks"


def test_mask_sensitive_url_no_query_question_mark():
    assert mask_sensitive_url("/api/stocks?") == "/api/stocks?"


def test_mask_sensitive_url_masks_admin_token():
    result = mask_sensitive_url("/api/stocks?admin_token=supersecret")
    assert "supersecret" not in result
    assert "admin_token=[REDACTED]" in result


def test_mask_sensitive_url_masks_token():
    result = mask_sensitive_url("/api/stream?token=mykey123")
    assert "mykey123" not in result
    assert "token=[REDACTED]" in result


def test_mask_sensitive_url_masks_shutdown_token():
    result = mask_sensitive_url("/api/admin?shutdown_token=abc")
    assert "abc" not in result
    assert "shutdown_token=[REDACTED]" in result


def test_mask_sensitive_url_preserves_other_params():
    result = mask_sensitive_url("/api/stocks?force=true&market=us")
    assert result == "/api/stocks?force=true&market=us"


def test_mask_sensitive_url_mixed_params():
    result = mask_sensitive_url(
        "/api/stocks?force=true&token=secret&market=us"
    )
    assert "secret" not in result
    assert "force=true" in result
    assert "market=us" in result
    assert "token=[REDACTED]" in result


def test_mask_sensitive_url_empty_param():
    result = mask_sensitive_url("/api/stream?token=")
    assert "token=[REDACTED]" in result


def test_mask_sensitive_url_multiple_sensitive():
    result = mask_sensitive_url(
        "/api/admin?admin_token=a&shutdown_token=b&token=c"
    )
    assert "admin_token=[REDACTED]" in result
    assert "shutdown_token=[REDACTED]" in result
    assert "token=[REDACTED]" in result
