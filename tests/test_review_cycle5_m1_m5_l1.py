"""Regression tests for review cycle 5 (M-1 / M-5 / L-1).

M-1: the prompt-embedding symbol allow-list was an ambiguous inline char class
(``[^\\w\\-.^=]``).  It is now a single literal-explicit definition
(``_SYMBOL_PROMPT_STRIP_RE``) behind one validation helper (``_safe_symbol``).
These tests lock the allowed character set (including ``^`` for index symbols
and ``=`` for forex, which a naive rewrite could drop), the stripping of
disallowed/prompt-injection characters, truncation, and non-string handling.

M-5: request-token / fetch-id generation used predictable ``Math.random()``
fallbacks.  Token generation is centralized in ``createRequestToken`` (Web
Crypto) and never fabricates a weak token; the chart fetch-correlation id uses a
crypto UUID / monotonic counter.  These tests assert, at the source level, that
the token/id code carries no ``Math.random`` once comments are stripped.

L-1: the SSE diff publish decision used ``sum(len(v) for v in diff.values())``
with an ambiguous "size" unit.  ``_build_sse_diff`` returns lists of changed
rows, so the unit is an explicit changed-entry count (presence), not bytes/chars.
These tests lock that value contract and the presence-based decision.
"""

from __future__ import annotations

import re
from pathlib import Path

from bg.common import _build_sse_diff
from routes.api_analysis import _SYMBOL_PROMPT_STRIP_RE, _safe_symbol

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# M-1: symbol allow-list for prompt embedding
# ---------------------------------------------------------------------------


class TestSafeSymbolCharClass:
    def test_allows_symbol_characters(self):
        # Word chars, dot (7203.T), equals (forex EURUSD=X), caret (index
        # ^GSPC) and hyphen (BRK-B) must all survive.
        for symbol in ("AAPL", "7203.T", "^GSPC", "EURUSD=X", "BRK-B", "6758.T"):
            assert _safe_symbol(symbol) == symbol

    def test_strips_disallowed_characters(self):
        # Everything outside the allow-list is removed: markup, whitespace,
        # control punctuation and prompt-injection metacharacters.
        assert _safe_symbol("AAPL<script>") == "AAPLscript"
        assert _safe_symbol("a b\nc\td") == "abcd"
        assert _safe_symbol("AAPL; DROP TABLE users", max_len=50) == "AAPLDROPTABLEusers"
        assert _safe_symbol("[sys] </prompt>") == "sysprompt"

    def test_truncates_to_max_len(self):
        assert _safe_symbol("A" * 50) == "A" * 15
        assert len(_safe_symbol("A" * 50)) == 15
        assert _safe_symbol("A" * 50, max_len=5) == "AAAAA"

    def test_edge_empty_and_none(self):
        assert _safe_symbol("") == ""
        assert _safe_symbol(None) == ""

    def test_char_class_matches_original_allowed_set(self):
        # Regression guard for the M-1 rewrite: the literal-explicit class must
        # keep exactly the original allowed set {word, ., =, ^, -}.  A naive
        # rewrite like ``[^\\w.\\-=]`` would silently drop ``^`` and break index
        # symbols such as ^GSPC.
        for ch in "azAZ09_.":
            assert _SYMBOL_PROMPT_STRIP_RE.sub("", ch) == ch
        for ch in ("=", "^", "-"):
            assert _SYMBOL_PROMPT_STRIP_RE.sub("", ch) == ch
        for ch in "<>[]{}()/\\ \t\n;,'\"!@#$%&*+?|~`":
            assert _SYMBOL_PROMPT_STRIP_RE.sub("", ch) == ""

    def test_symbol_regex_is_not_a_range_interpretation(self):
        # The caret and hyphen are escaped/literal: a caret stays a caret (not
        # the negation or a range) and a hyphen stays a hyphen.
        assert _safe_symbol("^GSPC") == "^GSPC"
        assert _safe_symbol("BRK-B") == "BRK-B"
        assert _safe_symbol("-^=-") == "-^=-"


# ---------------------------------------------------------------------------
# M-5: token / fetch-id generation must not use weak randomness
# ---------------------------------------------------------------------------

_JS_COMMENT_LINE = re.compile(r"//[^\n]*")
_JS_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)


def _js_code(path: Path) -> str:
    """Return the file's source with JS comments removed."""
    source = path.read_text(encoding="utf-8")
    source = _JS_COMMENT_BLOCK.sub("", source)
    source = _JS_COMMENT_LINE.sub("", source)
    return source


class TestNoWeakTokenRandomness:
    def test_token_sources_carry_no_math_random(self):
        # The token/identifier generators must not reference Math.random in
        # code (only in explanatory comments, which are stripped here).
        for rel in (
            "static/js/api.js",
            "static/js/ui.js",
            "static/js/chart.js",
            "static/js/experimental/constellation-controller.js",
            "static/js/experimental/ai-dive-controller.js",
        ):
            code = _js_code(PROJECT_ROOT / rel)
            assert "Math.random" not in code, f"{rel} still uses Math.random in code"

    def test_experimental_controllers_delegate_to_create_request_token(self):
        # The weak ``mns_${Date.now()}...${Math.random()}...`` fallback is gone;
        # the controllers delegate to the single createRequestToken system.
        for rel in (
            "static/js/experimental/constellation-controller.js",
            "static/js/experimental/ai-dive-controller.js",
        ):
            code = _js_code(PROJECT_ROOT / rel)
            assert "createRequestToken()" in code
            assert "mns_${Date.now()" not in code

    def test_create_request_token_refuses_to_fabricate_weak_token(self):
        # When Web Crypto is unavailable, createRequestToken must stop the
        # request (throw) rather than fabricate a predictable token.
        code = _js_code(PROJECT_ROOT / "static/js/api.js")
        assert "function createRequestToken" in code
        assert "throw new Error" in code
        assert "Math.random" not in code


# ---------------------------------------------------------------------------
# L-1: SSE diff size/presence decision unit
# ---------------------------------------------------------------------------


class TestSseDiffUnitContract:
    def test_diff_values_are_lists_of_rows(self):
        # The publish decision unit is a changed-entry count: diff values are
        # lists of changed rows, so len() counts rows (not bytes/chars).
        stocks = {"us": [{"symbol": "AAPL", "price": 100, "snapshot_ts_ms": 1}]}
        diff = _build_sse_diff(stocks, {})
        assert set(diff) == {"us", "jp", "idx"}
        for market_rows in diff.values():
            assert isinstance(market_rows, list)
        assert isinstance(diff["us"][0], dict)

    def test_empty_diff_when_no_change(self):
        # No change -> every market list empty -> changed-entry count == 0, so
        # the announce path emits a keepalive rather than a diff payload.
        row = {"symbol": "AAPL", "price": 100, "snapshot_ts_ms": 1}
        stocks = {"us": [dict(row)], "jp": [], "idx": []}
        prev = {"us": {"AAPL": dict(row)}, "jp": {}, "idx": {}}
        diff = _build_sse_diff(stocks, prev)
        assert sum(len(rows) for rows in diff.values()) == 0

    def test_nonempty_diff_when_price_changes(self):
        prev_row = {"symbol": "AAPL", "price": 100, "snapshot_ts_ms": 1}
        curr_row = {"symbol": "AAPL", "price": 105, "snapshot_ts_ms": 1}
        stocks = {"us": [curr_row], "jp": [], "idx": []}
        prev = {"us": {"AAPL": prev_row}, "jp": {}, "idx": {}}
        diff = _build_sse_diff(stocks, prev)
        # changed-entry count > 0 drives the diff-payload branch.
        assert sum(len(rows) for rows in diff.values()) > 0
        assert diff["us"][0]["price"] == 105

    def test_presence_decision_matches_entry_count(self):
        # The decision is presence-based: "at least one changed row".  This must
        # agree with the changed-entry count used in bg/common.py.
        unchanged = _build_sse_diff({}, {"us": {}, "jp": {}, "idx": {}})
        assert sum(len(rows) for rows in unchanged.values()) == 0
        assert not any(unchanged.values())

        added = _build_sse_diff(
            {"us": [{"symbol": "MSFT", "price": 1}], "jp": [], "idx": []}, {}
        )
        assert sum(len(rows) for rows in added.values()) > 0
        assert any(added.values())

    def test_removed_symbol_produces_marker_row(self):
        prev = {"us": {"AAPL": {"symbol": "AAPL", "price": 1}}, "jp": {}, "idx": {}}
        diff = _build_sse_diff({"us": [], "jp": [], "idx": []}, prev)
        assert diff["us"] == [{"symbol": "AAPL", "_removed": True}]
        assert sum(len(rows) for rows in diff.values()) == 1
