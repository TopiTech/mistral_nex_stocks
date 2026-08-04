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


# ---------------------------------------------------------------------------
# Dependency declaration consistency (release review finding)
# ---------------------------------------------------------------------------


def _strip_req_marker(line: str) -> str:
    """Strip a PEP 508 environment marker (e.g. ``; sys_platform == 'win32'``)."""
    return re.split(r";\s*", line.strip(), maxsplit=1)[0].strip()


def _parse_req_entry(line: str) -> tuple[str, str] | None:
    """Parse ``name>=x,<y`` (markers stripped) into ``(name, spec)``."""
    line = _strip_req_marker(line)
    if not line or line.startswith("#"):
        return None
    m = re.match(r"^([A-Za-z0-9_.-]+)([><=!~].*)?$", line)
    if not m:
        return None
    return m.group(1).lower(), (m.group(2) or "").strip()


def _ver_tuple(version: str) -> tuple[int, ...]:
    """Parse ``1.2.3a1``-style version into a comparable integer tuple.

    Pre-release and local segments (``rc1``, ``+local``) are stripped, which
    approximates PEP 440 for the exact released versions used in this repo.
    """
    core = version.split("+", 1)[0].split("-", 1)[0]
    parts: list[int] = []
    for seg in core.split("."):
        digits = "".join(ch for ch in seg if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _satisfies(version: str, spec: str) -> bool:
    """True if ``version`` satisfies a simple ``>=x,<y`` style specifier."""
    if not spec:
        return True
    vt = _ver_tuple(version)
    for clause in (c.strip() for c in spec.split(",") if c.strip()):
        op = next((o for o in (">=", "<=", "!=", "==", ">", "<") if clause.startswith(o)), None)
        if op is None:
            # Unsupported specifier (e.g. ``~=``) must fail loudly instead of
            # being silently treated as satisfied.
            return False
        bound = _ver_tuple(clause[len(op) :].strip())
        if op == ">=" and not vt >= bound:
            return False
        if op == ">" and not vt > bound:
            return False
        if op == "<=" and not vt <= bound:
            return False
        if op == "<" and not vt < bound:
            return False
        if op == "==" and vt != bound:
            return False
        if op == "!=" and vt == bound:
            return False
    return True


def _read_requirements_map(rel: str) -> dict[str, str]:
    """Map lowercase package name -> spec string for a requirements file."""
    entries: dict[str, str] = {}
    for line in (ROOT / rel).read_text(encoding="utf-8").splitlines():
        parsed = _parse_req_entry(line)
        if parsed:
            entries.setdefault(parsed[0], parsed[1])
    return entries


def _read_pyproject_dependencies() -> dict[str, str]:
    """Map lowercase package name -> spec string from pyproject.toml."""
    import tomllib

    with open(ROOT / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    deps: dict[str, str] = {}
    for entry in data.get("project", {}).get("dependencies", []) or []:
        parsed = _parse_req_entry(str(entry))
        if parsed:
            deps.setdefault(parsed[0], parsed[1])
    return deps


def test_locked_versions_satisfy_requirements_ranges():
    """Every version pinned in requirements-locked.txt must be within the
    range declared in requirements.txt.

    Regression: requirements.txt declared ``psutil>=5.9.8,<6.0`` while
    requirements-locked.txt pinned ``psutil==7.2.2``, so a fresh
    ``pip install -r requirements.txt`` resolved a different major line than
    the environment CI installs and verifies.
    """
    reqs = _read_requirements_map("requirements.txt")
    locked = {
        name: spec[2:].strip()
        for name, spec in _read_requirements_map("requirements-locked.txt").items()
        if spec.startswith("==")
    }
    missing = sorted(set(locked) - set(reqs))
    assert not missing, (
        "Packages pinned in requirements-locked.txt but missing from "
        f"requirements.txt: {missing}"
    )
    violations = [
        f"{name}=={version} is outside requirements.txt range {reqs[name]!r}"
        for name, version in sorted(locked.items())
        if name in reqs and not _satisfies(version, reqs[name])
    ]
    assert not violations, (
        "requirements-locked.txt pins versions that requirements.txt does not "
        "allow: " + "; ".join(violations)
    )


def test_requirements_txt_matches_pyproject_ranges():
    """requirements.txt and pyproject.toml must declare identical ranges for
    every shared dependency.

    Regression: psutil (``<6.0`` vs ``<8``) and ddgs (``>=9.14,<10.0`` vs
    ``>=9.9,<11.0``) had drifted between the two files.
    """
    reqs = _read_requirements_map("requirements.txt")
    pyproject = _read_pyproject_dependencies()
    missing = sorted(set(pyproject) - set(reqs))
    assert not missing, (
        f"Dependencies declared in pyproject.toml but missing from requirements.txt: {missing}"
    )
    # Normalize whitespace so harmless formatting differences (e.g. ``>=1.0, <2.0``)
    # do not cause spurious failures.
    mismatches = [
        f"{name}: requirements.txt={reqs[name]!r} pyproject.toml={pyproject[name]!r}"
        for name in sorted(set(reqs) & set(pyproject))
        if reqs[name].replace(" ", "") != pyproject[name].replace(" ", "")
    ]
    assert not mismatches, (
        "requirements.txt and pyproject.toml declare different ranges for: "
        + "; ".join(mismatches)
    )
