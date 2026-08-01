"""
test_doc_consistency.py - README <-> code default value consistency (M-1).

Ensures the README "環境変数リファレンス" table defaults stay in sync with the
actual defaults defined in constants.py (and services/search/ddgs.py). A
previous release review (M-1) found 6 stale rows; this test prevents regressions
by mechanically comparing the table against the code for the env vars the README
documents.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _code_defaults() -> dict[str, str]:
    """Extract env-var defaults from constants.py and ddgs.py."""
    defaults: dict[str, str] = {}
    for rel in ("constants.py", "services/search/ddgs.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        # _env_int("NAME", default, ...) / _env_float("NAME", default, ...)
        for name, default in re.findall(
            r'_env_(?:int|float)\(\s*"([A-Z0-9_]+)"\s*,\s*([0-9.]+)',
            text,
        ):
            defaults.setdefault(name, default)
    return defaults


def _readme_defaults() -> dict[str, str]:
    """Extract env-var defaults from the README table rows."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    rows: dict[str, str] = {}
    # Table row: | `NAME` | `default` | description |
    for name, default in re.findall(
        r"\|\s*`([A-Z0-9_]+)`\s*\|\s*`([^`]+)`\s*\|", text
    ):
        rows.setdefault(name, default.strip())
    return rows


@pytest.mark.parametrize(
    "env_name",
    [
        "DDGS_TIMEOUT",
        "MNS_MISTRAL_MIN_INTERVAL",
        "MNS_YFINANCE_SHORT_CACHE_TTL",
        "MNS_MAX_SSE_LISTENERS",
        "MNS_YFINANCE_SESSION_IDLE_TTL_SEC",
        "MNS_YFINANCE_SESSION_RECLAIM_INTERVAL_SEC",
        "MNS_YFINANCE_SESSION_POOL_MAX",
        "MNS_MISTRAL_API_TIMEOUT",
        "MNS_NEGATIVE_CACHE_TTL",
        "MNS_YFINANCE_REQ_MIN_INTERVAL_BASE",
        "MNS_YFINANCE_MAX_CONCURRENT_REQUESTS",
    ],
)
def test_readme_default_matches_code(env_name):
    """README default for env_name must equal the code default (M-1)."""
    code = _code_defaults()
    readme = _readme_defaults()
    assert env_name in code, f"{env_name} not found in code defaults"
    assert env_name in readme, f"{env_name} missing from README table"
    code_val = code[env_name]
    readme_val = readme[env_name]
    # Normalize numeric representation (e.g. 60.0 vs 60) for comparison.
    assert float(readme_val) == float(code_val), (
        f"README default for {env_name} is {readme_val!r} but code default is "
        f"{code_val!r}. Update README.md to match constants.py."
    )


def test_all_code_env_vars_documented_or_known():
    """Every tunable env var in constants.py should be documented in README.

    Known-exceptions list: env vars intentionally undocumented (internal /
    rarely used or app.py-owned). If a NEW tunable is added to constants.py it
    must be added to the README table (or to the exceptions list with a reason).
    """
    code = _code_defaults()
    readme = _readme_defaults()
    documented = set(code) & set(readme)
    # Env vars present in code but intentionally not documented in README.
    # WORKFLOW: when adding a NEW env var to constants.py, either add a README
    # table row (preferred) or add it to this set with a reason comment.
    known_undocumented = {
        "MNS_MISTRAL_API_KEY_MIN_LENGTH",
        "MNS_LANGSEARCH_API_KEY_MIN_LENGTH",
        "MNS_TAVILY_API_KEY_MIN_LENGTH",
        "MNS_STOCK_HISTORY_DISK_CACHE_TTL",
        "MNS_STOCK_HISTORY_CACHE_MAXSIZE",
        "MNS_STOCK_PAYLOAD_DISK_CACHE_TTL",
        "MNS_YFINANCE_TIMEOUT_BATCH",
        "MNS_YFINANCE_TIMEOUT_SINGLE",
        "MNS_YFINANCE_MAX_RETRIES",
        "MNS_YFINANCE_RETRY_WAIT",
        "MNS_YFINANCE_RETRY_BACKOFF_BASE",
        "MNS_YFINANCE_BACKOFF_INITIAL",
        "MNS_YFINANCE_BACKOFF_MAX",
        "MNS_YFINANCE_BACKOFF_MULTIPLIER",
        "MNS_YFINANCE_BATCH_CHUNK_PAUSE",
        "MNS_YFINANCE_MIN_INTERVAL",
        "MNS_YFINANCE_JITTER_FACTOR",
        "MNS_YFINANCE_ADAPTIVE_INTERVAL_FACTOR",
        "MNS_YFINANCE_SHORT_CACHE_TTL_RATE_LIMITED",
        "MNS_YFINANCE_REQ_MIN_INTERVAL_MAX",
        "MNS_YFINANCE_REQ_INTERVAL_GROWTH",
        "MNS_YFINANCE_REQ_INTERVAL_DECAY",
        "MNS_YFINANCE_REQ_INTERVAL_DECAY_AFTER",
        "MNS_CIRCUIT_BREAKER_THRESHOLD",
        "MNS_CIRCUIT_BREAKER_OPEN_SEC",
        "MNS_NEWS_CONTEXT_WAIT_TIMEOUT",
        "MNS_NEWS_PREPARE_WAIT_SEC",
        "MNS_CHAT_PREPARE_WAIT_SEC",
        "MNS_ANALYZE_RESEARCH_CONTEXT_MAX_CHARS",
        "MNS_CACHE_DURATION",
        "MNS_CACHE_DURATION_NEWS",
        "MNS_CACHE_DURATION_HEATMAP",
        "MNS_CACHE_DURATION_SEARCH",
        "MNS_CACHE_DURATION_TRENDING",
        "MNS_STATIC_MTIME_CACHE_TTL",
        "MNS_HISTORY_CACHE_DURATION_OPEN",
        "MNS_HISTORY_CACHE_DURATION_OPEN_LONG",
        "MNS_HISTORY_CACHE_DURATION_CLOSED",
        "MNS_HISTORY_CACHE_DURATION_CLOSED_LONG",
        "MNS_HISTORY_SEMAPHORE_TIMEOUT",
        "MNS_ANALYSIS_MAX_TOKENS",
        "MNS_ANALYSIS_MAX_TOKENS_FALLBACK",
        "MNS_CHAT_MAX_TOKENS",
        "MNS_CHAT_MAX_MSG_LENGTH",
        "MNS_CHAT_HISTORY_MAX_KEYS",
        "MNS_CHAT_HISTORY_MAX_MSGS",
        "MNS_NEWS_SUMMARY_MAX_TOKENS",
        "MNS_REPAIR_NEWS_MAX_TOKENS",
        "MNS_MISTRAL_MAX_TOKENS_CEIL",
        "MNS_SSE_HEARTBEAT_INTERVAL",
        "MNS_SSE_MARKET_CLOSED_SLEEP",
        "MNS_SSE_MARKET_OPEN_SLEEP",
        "MNS_SSE_YAHOO_FETCH_MARKET_CLOSED_SLEEP",
        "MNS_SSE_YAHOO_FETCH_MARKET_OPEN_SLEEP",
        "MNS_SSE_YAHOO_FETCH_NO_LISTENER_SLEEP",
    }
    missing = (set(code) - documented) - known_undocumented
    assert not missing, (
        f"Env vars defined in constants.py but missing from README: {sorted(missing)}"
    )
