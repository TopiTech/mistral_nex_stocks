"""Regression tests for residual defects found in the 2026-11-17 review.

Covered:
- a native-host manifest whose JSON root is not an object must degrade the
  extension-origin status instead of raising AttributeError through the
  trusted-origin gate (which 500'd every state-changing request);
- /api/analyze-chart-image must authenticate before reading/parsing a 7 MiB
  request body;
- /api/credentials/verify must distinguish provider outages (503/504) from an
  invalid API key (400).
"""

from __future__ import annotations

import json
import os
from unittest.mock import mock_open, patch

import httpx
import pytest

from app import app
from app_state import app_state
from error_codes import ErrorCode
from utils.networking import _load_allowed_extension_origins


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client


def test_non_object_manifest_root_degrades_without_raising():
    """A JSON array manifest root must not escape as AttributeError."""
    app_state._extension_origins_cache_ts = 0.0
    app_state._extension_origins_cache.clear()
    with (
        patch.dict(
            os.environ,
            {"MNS_ALLOWED_EXTENSION_ORIGINS": "", "MNS_EXTENSION_ORIGIN": ""},
            clear=False,
        ),
        patch("pathlib.Path.exists", return_value=True),
        patch("builtins.open", mock_open(read_data=json.dumps([1, 2]))),
    ):
        origins = _load_allowed_extension_origins()

    assert origins == set()
    assert app_state._extension_manifest_status["ok"] is False
    assert "manifest root must be a JSON object" in app_state._extension_manifest_status["error"]
    # Force the next caller to reload the real manifest instead of inheriting
    # this test's degraded cache entry.
    app_state._extension_origins_cache_ts = 0.0


def test_chart_image_authenticates_before_parsing_body(client):
    """An unauthenticated oversized body must fail at the auth gate, not 400."""
    with patch("routes.api_analysis.extract_api_key", return_value=""):
        res = client.post(
            "/api/analyze-chart-image",
            data="{bad json",
            content_type="application/json",
            headers={"Origin": "http://localhost:5000"},
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )

    assert res.status_code == 401
    body = res.get_json()
    assert body is not None
    assert body["ok"] is False
    assert body["error_code"] == int(ErrorCode.API_AUTH_FAILED)


@pytest.mark.parametrize(
    ("provider_error", "expected_status"),
    [
        (httpx.ConnectError("connection refused"), 503),
        (httpx.ReadTimeout("provider did not answer"), 504),
    ],
)
def test_credentials_verify_classifies_provider_outages(client, provider_error, expected_status):
    """Provider connectivity failures are not invalid API keys."""
    with patch(
        "routes.api_system.app_state.ai.get_or_create_mistral_client",
        side_effect=provider_error,
    ):
        res = client.post(
            "/api/credentials/verify",
            json={"mistral_api_key": "test-valid-key"},
            headers={"Origin": "http://localhost:5000"},
        )

    assert res.status_code == expected_status
    body = res.get_json()
    assert body is not None
    assert body["ok"] is False
    assert body["error_code"] == int(ErrorCode.API_SERVICE_ERROR)
    assert "test-valid-key" not in json.dumps(body)
