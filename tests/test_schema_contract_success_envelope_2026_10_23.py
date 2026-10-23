"""HTTP contract tests for the unified success envelope (U1/A-06 slice).

Every ``OPTIONS`` preflight across the API surface now answers through the
single ``utils.stock_payload.ok_response`` factory, so the body is always
exactly ``{\"ok\": True, \"success\": True}``. Both keys stay because the
frontend reads ``data?.ok || data?.success``. These tests pin the unified
envelope per endpoint so a future handler cannot silently revert to an
``ok``-only body.
"""

from __future__ import annotations

import pytest

from app import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client


OPTIONS_PATHS = [
    "/api/credentials",
    "/api/credentials/verify",
    "/api/health",
    "/api/cache-stats",
    "/api/metrics",
    "/api/csrf-token",
    "/api/shutdown",
    "/api/system/ai-usage",
    "/api/ai-technical-lines",
    "/api/stocks/add_ext",
]


@pytest.mark.parametrize("path", OPTIONS_PATHS)
def test_options_returns_unified_envelope(client, path):
    resp = client.options(path)
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "success": True}
