"""Regression tests: lone UTF-16 surrogates must not reach a UTF-8 encoder.

``json.loads`` accepts ``"\\ud800"`` escapes, so an HTTP client can deliver a
``str`` that is not a Unicode scalar value. Every later ``text.encode("utf-8")``
(cache-key sanitising, the rate limiter's token digest, Fernet encryption,
SQLite text binding) raises ``UnicodeEncodeError``, which surfaced as an
unhandled HTTP 500 from a malformed body. The JSON boundary now rejects such
bodies, and cache-key sanitising is total.
"""

import json

from app import app
from utils.caching import sanitize_cache_key
from utils.text_utils import _contains_lone_surrogate, _parse_json_request

LONE_HIGH = "\ud800"
LONE_LOW = "\udc00"


def _ascii_body(payload: dict) -> bytes:
    """Serialize like a real client: ``ensure_ascii`` escapes a lone surrogate."""
    return json.dumps(payload).encode("ascii")


def test_contains_lone_surrogate_detects_nested_value():
    assert _contains_lone_surrogate({"a": [{"b": LONE_HIGH}]}) is True


def test_contains_lone_surrogate_detects_dict_key():
    assert _contains_lone_surrogate({LONE_LOW: 1}) is True


def test_contains_lone_surrogate_accepts_pair_encoded_astral_text():
    """A surrogate *pair* decodes to one scalar value and must be accepted."""
    emoji = json.loads('"\\ud83d\\ude00"')
    assert len(emoji) == 1
    assert _contains_lone_surrogate({"message": f"ok {emoji}"}) is False


def test_json_boundary_rejects_lone_surrogate_body():
    body = _ascii_body({"message": LONE_HIGH})
    with app.test_request_context(
        "/api/chat", method="POST", data=body, content_type="application/json"
    ):
        assert _parse_json_request() is None


def test_json_boundary_keeps_valid_unicode_body():
    expected = {"message": "ok 😀 日本株"}
    with app.test_request_context(
        "/api/chat",
        method="POST",
        data=_ascii_body(expected),
        content_type="application/json",
    ):
        assert _parse_json_request() == expected


def test_rate_limited_endpoint_does_not_500_on_surrogate_token(client):
    """The rate limiter hashed ``request_token`` before schema validation ran."""
    body = _ascii_body(
        {
            "symbol": "AAPL",
            "market": "us",
            "message": "AAPL",
            "request_token": LONE_HIGH + "a" * 24,
        }
    )
    resp = client.post("/api/chat", data=body, content_type="application/json")
    assert resp.status_code != 500
    payload = resp.get_json()
    assert payload is not None
    assert payload["ok"] is False


def test_add_stock_rejects_surrogate_payload_as_malformed(client):
    body = _ascii_body({"symbol": "AAPL", "market": "us", "name": LONE_HIGH})
    resp = client.post(
        "/api/stocks/add",
        data=body,
        content_type="application/json",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        headers={"Origin": "http://localhost:5000"},
    )
    assert resp.status_code == 400
    payload = resp.get_json()
    assert payload["ok"] is False
    assert payload["details"]["reason"] == "JSON形式が不正です"


def test_sanitize_cache_key_is_total_and_injective():
    high = sanitize_cache_key(f"search_{LONE_HIGH}")
    low = sanitize_cache_key(f"search_{LONE_LOW}")
    assert isinstance(high, str) and high
    assert isinstance(low, str) and low
    assert high != low
    assert sanitize_cache_key("search_A") == "search_A"


def test_sanitize_cache_key_hashes_over_long_surrogate_key():
    key = sanitize_cache_key("k" * 300 + LONE_HIGH)
    assert key
    assert key != sanitize_cache_key("k" * 300 + LONE_LOW)
