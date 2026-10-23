"""Unit tests verifying /api/screener unified response envelope.

Ensures:
- Screener response emits both `ok: True` and `success: True` in unified envelope.
- `total`, `totalFiltered`, `stocks`, and `missing` are preserved intact.
"""

from __future__ import annotations

import pytest

from app import create_app
from tests.test_support.state_reset import reset_app_state_internals


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
    reset_app_state_internals()


def test_screener_emits_unified_envelope(client):
    """Verify /api/screener returns both ok=True and success=True."""
    resp = client.get("/api/screener?market=all&limit=10")
    assert resp.status_code == 200
    data = resp.get_json()

    assert data is not None
    assert data.get("ok") is True
    assert data.get("success") is True
    assert "total" in data
    assert "totalFiltered" in data
    assert "stocks" in data
    assert "missing" in data
    assert isinstance(data["stocks"], list)
