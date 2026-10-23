"""U6 regression: C-02/G-01 auth unification (2026-10-23).

- ``utils.networking.check_admin_token`` is the single constant-time
  primitive for every MNS_ADMIN_TOKEN comparison.
- The extension Bearer credential is validated only by
  ``credential_manager.require_extension_token`` (never the admin predicate).
- Direct ``secrets.compare_digest`` in production code lives only where the
  comparison itself is the primitive (networking, credential_manager) or a
  separate credential system (shutdown_manager SSE-ticket binding aside —
  shutdown tokens are their own one-time mechanism).
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from credential_manager import require_extension_token
from utils.networking import check_admin_token

REPO_ROOT = Path(__file__).resolve().parent.parent
_COMPARE_SWEEP_DIRS = ("app_factory", "bg", "routes", "services", "utils")
_COMPARE_SWEEP_FILES = (
    "app.py",
    "app_bg.py",
    "wsgi.py",
    "route_helpers.py",
    "credential_manager.py",
    "shutdown_manager.py",
)
# Files allowed to call secrets.compare_digest directly: the primitive's home,
# the extension-token helper's home, and the standalone shutdown-token system.
_COMPARE_ALLOWED = {
    "utils/networking.py",
    "credential_manager.py",
    "shutdown_manager.py",
}


def _iter_source_files():
    for dirname in _COMPARE_SWEEP_DIRS:
        yield from (REPO_ROOT / dirname).rglob("*.py")
    for name in _COMPARE_SWEEP_FILES:
        candidate = REPO_ROOT / name
        if candidate.exists():
            yield candidate


def test_compare_digest_only_in_owner_modules():
    offenders = []
    for path in _iter_source_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in _COMPARE_ALLOWED:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "compare_digest" in line.split("#", 1)[0]:
                offenders.append(f"{rel}:{lineno}")
    assert offenders == [], f"compare_digest outside owner modules: {offenders}"


def test_check_admin_token_matrix():
    with patch.dict(os.environ, {"MNS_ADMIN_TOKEN": "admin-token-0123456789abcdef"}, clear=False):
        assert check_admin_token("admin-token-0123456789abcdef") is True
        assert check_admin_token("admin-token-0123456789abcdeX") is False
        assert check_admin_token("short") is False
        assert check_admin_token("") is False
        assert check_admin_token(None) is False
    with patch.dict(os.environ, {"MNS_ADMIN_TOKEN": ""}, clear=False):
        assert check_admin_token("admin-token-0123456789abcdef") is False
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MNS_ADMIN_TOKEN", None)
        assert check_admin_token("anything") is False


def test_check_admin_token_never_raises_on_mismatched_input():
    with patch.dict(os.environ, {"MNS_ADMIN_TOKEN": "admin-token-0123456789abcdef"}, clear=False):
        assert check_admin_token("x" * 5000) is False


def test_require_extension_token_matrix():
    with patch(
        "credential_manager.get_or_create_extension_api_token",
        return_value="ext-token-abc123",
    ):
        ok_req = MagicMock()
        ok_req.headers = {"Authorization": "Bearer ext-token-abc123"}
        assert require_extension_token(ok_req) == (True, "")

        for headers in (
            {},
            {"Authorization": ""},
            {"Authorization": "ext-token-abc123"},
            {"Authorization": "Bearer wrong"},
            {"Authorization": "Bearer "},
            {"Authorization": "bearer ext-token-abc123"},
        ):
            bad_req = MagicMock()
            bad_req.headers = headers
            ok, reason = require_extension_token(bad_req)
            assert ok is False
            assert reason == "invalid or missing extension token"


def test_extension_token_uses_real_round_trip():
    from credential_manager import get_or_create_extension_api_token

    expected = get_or_create_extension_api_token()
    assert isinstance(expected, str) and len(expected) >= 32
    req = MagicMock()
    req.headers = {"Authorization": f"Bearer {expected}"}
    assert require_extension_token(req) == (True, "")


def test_admin_and_extension_credentials_are_not_interchangeable():
    with patch.dict(os.environ, {"MNS_ADMIN_TOKEN": "admin-token-0123456789abcdef"}, clear=False):
        with patch(
            "credential_manager.get_or_create_extension_api_token",
            return_value="ext-token-abc123",
        ):
            # The admin token is not accepted as an extension Bearer token.
            admin_as_ext = MagicMock()
            admin_as_ext.headers = {"Authorization": "Bearer admin-token-0123456789abcdef"}
            assert require_extension_token(admin_as_ext)[0] is False
            # The extension token is not accepted by the admin predicate.
            assert check_admin_token("ext-token-abc123") is False
