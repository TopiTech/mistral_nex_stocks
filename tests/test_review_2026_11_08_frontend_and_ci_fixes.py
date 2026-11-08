"""Regression tests for the frontend, CI and native-host findings of the
full-repository review (goal-20261108-full-review-v4).

Each test pins one root-cause fix and fails against the pre-fix code.
"""

from __future__ import annotations

import json
import os
import pathlib
import time
from unittest.mock import patch

import pytest

from app import app
from route_helpers import (
    _rate_limit_distinct_token_counts,
    _rate_limit_lock,
    _rate_limit_store,
    _rate_limit_window_by_key,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
REMOTE = {"REMOTE_ADDR": "192.168.1.216"}
NEWS_TOKEN = "news-polling-token-0123456789abcdef"


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client


def _reset_rate_limit_state() -> None:
    with _rate_limit_lock:
        _rate_limit_store.clear()
        _rate_limit_window_by_key.clear()
        _rate_limit_distinct_token_counts.clear()


@pytest.fixture(autouse=True)
def _clear_rate_limit_state():
    _reset_rate_limit_state()
    yield
    _reset_rate_limit_state()


# ---------------------------------------------------------------------------
# FE-02 — /api/news must not charge every poll of one job against the quota
# ---------------------------------------------------------------------------


def _seed_news_cache() -> None:
    from utils.caching import _set_cached_value

    # No search keys configured -> "ddgs_only" is the strategy the route picks.
    _set_cached_value("news_bundle_latest_ddgs_only", {"items": []}, duration=86400)
    _set_cached_value("news_bundle_latest_ddgs_only_ts", time.time(), duration=86400)


def _post_news(client, body: dict):
    return client.post("/api/news", json=body, environ_base=REMOTE)


def test_news_polls_with_one_token_never_return_429(client):
    """25 polls of one news job (over the 20/min quota) must all succeed."""
    from routes import api_analysis

    _seed_news_cache()
    with (
        patch.object(api_analysis, "extract_api_key", return_value="test-api-key"),
        patch.object(api_analysis, "require_trusted_or_admin", return_value=(True, None)),
    ):
        statuses = []
        for _ in range(25):
            resp = _post_news(client, {"request_token": NEWS_TOKEN})
            statuses.append(resp.status_code)

    assert set(statuses) == {200}, statuses


def test_news_distinct_tokens_still_consume_the_quota(client):
    """The polling exemption must not weaken the endpoint quota for new work."""
    from routes import api_analysis

    _seed_news_cache()
    with (
        patch.object(api_analysis, "extract_api_key", return_value="test-api-key"),
        patch.object(api_analysis, "require_trusted_or_admin", return_value=(True, None)),
    ):
        statuses = []
        for index in range(21):
            resp = _post_news(client, {"request_token": f"distinct-news-token-{index:04d}"})
            statuses.append(resp.status_code)

    assert statuses[:20] == [200] * 20, statuses
    assert statuses[20] == 429, statuses


def test_news_frontend_sends_one_token_for_the_job_and_its_polls():
    """The client half of the contract: a stable token on the POST and the polls."""
    source = (ROOT / "static" / "js" / "api.js").read_text(encoding="utf-8")
    assert "const newsRequestToken = createRequestToken();" in source
    assert source.count("body: JSON.stringify({ request_token: newsRequestToken })") == 2


# ---------------------------------------------------------------------------
# FE-03 — resetAllStocks must not fire a second POST while one is in flight
# ---------------------------------------------------------------------------


def test_reset_all_stocks_guards_the_request_not_just_the_confirm_click():
    source = (ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8")
    assert "let resetAllStocksInFlight = false;" in source
    guard = source.index("if (resetAllStocksInFlight) return;")
    confirm_branch = source.index("if (resetBtn && !resetBtn.__isConfirming) {")
    # The guard must run before the confirm branch re-arms the button.
    assert guard < confirm_branch
    assert "resetAllStocksInFlight = true;" in source
    assert "resetAllStocksInFlight = false;" in source


# ---------------------------------------------------------------------------
# FE-06 — the deferred drawer refresh must be cancellable
# ---------------------------------------------------------------------------


def test_drawer_refresh_timer_is_cleared_when_the_drawer_closes():
    source = (ROOT / "static" / "js" / "ui.js").read_text(encoding="utf-8")
    assert "let stockDetailDrawerRefreshTimer = null;" in source
    close_start = source.index("function closeStockDetailDrawer() {")
    close_body = source[close_start : close_start + 400]
    assert "clearTimeout(stockDetailDrawerRefreshTimer);" in close_body
    assert "stockDetailDrawerRefreshTimer = setTimeout(" in source


# ---------------------------------------------------------------------------
# FE-08 — the per-card "add to my portfolio" button needs an in-flight guard
# ---------------------------------------------------------------------------


def test_per_card_add_button_guards_double_submit():
    source = (ROOT / "static" / "js" / "ai_portfolio.js").read_text(encoding="utf-8")
    handler_start = source.index('addBtn.addEventListener("click", () => {')
    handler = source[handler_start : handler_start + 400]
    assert "if (addBtn.disabled) return;" in handler
    assert "addBtn.disabled = true;" in handler
    assert "addBtn.disabled = false;" in handler


# ---------------------------------------------------------------------------
# CI-POSIX-1 / CI-SRI-1 / CI-GEN-1 — the workflow gates must actually gate
# ---------------------------------------------------------------------------


def _ci_workflow() -> str:
    return (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def test_posix_selector_matches_the_0o600_permission_tests():
    """``-k "0600"`` never matched ``..._0o600``; the selector must list it."""
    workflow = _ci_workflow()
    selector_line = next(
        line for line in workflow.splitlines() if "-k \"posix or flock" in line
    )
    assert "0o600" in selector_line, selector_line


def test_sri_gate_accepts_any_quoting_style():
    """A single-quoted CDN src must not slip past the integrity check."""
    workflow = _ci_workflow()
    assert '["\']?https:' in workflow
    assert '["\']?sha384-' in workflow


def test_test_job_installs_node_modules_so_the_build_test_cannot_skip():
    workflow = _ci_workflow()
    test_job = workflow[workflow.index("  test:") : workflow.index("  posix-persistence:")]
    assert "run: npm ci" in test_job


# ---------------------------------------------------------------------------
# NH-01 — the rate-limit window must survive a fresh host process
# ---------------------------------------------------------------------------


def test_rate_limit_window_survives_a_new_host_process(tmp_path):
    """``sendNativeMessage`` spawns one process per message; the window must persist."""
    import native_host.native_host as nh_module
    from tests.test_support.native_host_state import isolated_native_host_rate_limit

    with isolated_native_host_rate_limit(nh_module) as ledger:
        old_max = nh_module._NATIVE_RATE_LIMIT_MAX
        try:
            nh_module._NATIVE_RATE_LIMIT_MAX = 2
            assert nh_module._check_rate_limit() is True
            assert nh_module._check_rate_limit() is True
            # Simulate the next message arriving in a brand-new host process:
            # the in-memory window is empty, only the ledger remembers.
            with nh_module._rate_limit_lock:
                nh_module._rate_limit_timestamps.clear()
            assert nh_module._check_rate_limit() is False
        finally:
            nh_module._NATIVE_RATE_LIMIT_MAX = old_max

        payload = json.loads(ledger.read_text(encoding="utf-8"))
        assert len(payload["ipc"]) == 2


def test_token_budget_survives_a_new_host_process(tmp_path):
    import native_host.native_host as nh_module
    from tests.test_support.native_host_state import isolated_native_host_rate_limit

    with isolated_native_host_rate_limit(nh_module):
        for _ in range(3):
            assert nh_module._token_action_allowed() is True
        with nh_module._rate_limit_lock:
            nh_module._token_action_timestamps.clear()
        assert nh_module._token_action_allowed() is False


def test_rate_limit_ledger_is_owner_only(tmp_path):
    import native_host.native_host as nh_module
    from tests.test_support.native_host_state import isolated_native_host_rate_limit

    with isolated_native_host_rate_limit(nh_module) as ledger:
        assert nh_module._check_rate_limit() is True
        if os.name != "nt":
            assert ledger.stat().st_mode & 0o077 == 0


def test_corrupt_ledger_does_not_break_the_limiter():
    import native_host.native_host as nh_module
    from tests.test_support.native_host_state import isolated_native_host_rate_limit

    with isolated_native_host_rate_limit(nh_module) as ledger:
        ledger.write_text("{not json", encoding="utf-8")
        assert nh_module._check_rate_limit() is True
        assert json.loads(ledger.read_text(encoding="utf-8"))["ipc"]


def test_unwritable_ledger_falls_back_to_the_in_process_window(tmp_path):
    import native_host.native_host as nh_module
    from tests.test_support.native_host_state import isolated_native_host_rate_limit

    with isolated_native_host_rate_limit(nh_module):
        blocked = tmp_path / "blocked"
        blocked.mkdir()
        with patch.object(nh_module, "_RATE_LIMIT_LEDGER_PATH", blocked / "ledger.json"):
            # A directory in place of the file makes os.open fail with OSError.
            (blocked / "ledger.json").mkdir()
            assert nh_module._check_rate_limit() is True
            assert nh_module._rate_limit_ledger_disabled is True


# ---------------------------------------------------------------------------
# NH-07 — a read-only data directory must not kill the host at import time
# ---------------------------------------------------------------------------


def test_log_handler_degrades_instead_of_raising(tmp_path):
    import native_host.native_host as nh_module

    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "native_host.log").mkdir()  # a directory where the file must go

    assert nh_module._build_file_handler(blocked) is None


def test_log_handler_is_created_for_a_writable_directory(tmp_path):
    import native_host.native_host as nh_module

    handler = nh_module._build_file_handler(tmp_path / "logs")
    try:
        assert handler is not None
        assert (tmp_path / "logs" / "native_host.log").exists()
    finally:
        if handler is not None:
            handler.close()
