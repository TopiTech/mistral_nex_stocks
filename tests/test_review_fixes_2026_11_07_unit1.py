"""Regression tests for the 2026-11-07 exhaustive review fixes.

Unit 1 covers the environment/type foundation: guarded env parsing, the
canonical boolean spelling convention, gunicorn capacity accounting, and
leader-lock re-acquisition semantics.

Each test pins the exact regression found by the review:

- ``MNS_DEFAULT_USDJPY`` crashed ``import constants`` on malformed values.
- ``MNS_WORKER_VALIDATION=off`` silently kept the single-worker guard on.
- gunicorn duplicated that parsing, and its ``threads`` ceiling could sink
  below the required thread count at the documented listener maximum.
- An empty ``MNS_PROXY_FIX_X_FOR`` made ``wsgi`` treat the value as hops=0
  and hard-exit, unlike the shared ``_env_int`` semantics ("unset").
- The SSE ticket cookie, the local rate-limit warning, Google Trends browser
  mode, and the ephemeral-crypto fallback parsed booleans ad hoc, so the
  canonical truthy spelling ``on`` was silently ignored.
- Re-asserting leadership with an open mirror handle self-destructed on
  Windows: ``msvcrt`` cannot re-lock the same byte range through the same fd.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent

CANONICAL_TRUTHY = ("1", "true", "yes", "on")


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _load_gunicorn_config():
    spec = importlib.util.spec_from_file_location(
        "test_gunicorn_config_review_2026_11_07", ROOT / "gunicorn.conf.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# L1: constants.DEFAULT_USDJPY_RATE must survive malformed env values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    (("", 150.0), ("   ", 150.0), ("abc", 150.0), ("0", 0.01), ("200.5", 200.5)),
)
def test_default_usdjpy_env_parse_is_guarded(raw, expected):
    """A malformed MNS_DEFAULT_USDJPY must fall back instead of crashing import.

    ``float(os.environ.get(...))`` raised ValueError during ``import
    constants`` for a set-but-empty value, killing every entry point with an
    unrelated traceback. The guarded parse must keep the default for junk
    values and honor valid ones.
    """
    env = {**os.environ, "MNS_DEFAULT_USDJPY": raw, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(  # noqa: PLW1510 -- the returncode is asserted below
        [sys.executable, "-c", "import constants; print(constants.DEFAULT_USDJPY_RATE)"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(ROOT),
    )
    assert proc.returncode == 0, f"import constants failed for {raw!r}: {proc.stderr}"
    assert abs(float(proc.stdout.strip()) - expected) < 1e-9


# ---------------------------------------------------------------------------
# M41: "off" must disable the single-worker guard everywhere it is parsed
# ---------------------------------------------------------------------------


def test_worker_validation_off_disables_single_worker_guard():
    """``MNS_WORKER_VALIDATION=off`` must disable the guard like ``0`` does.

    The guard's own module accepted the canonical falsy spellings in one
    function and not in this one, so an operator setting ``off`` (the README
    spelling) still hit MultiWorkerConfigurationError in a legitimate
    single-process deployment.
    """
    from utils.worker_validation import worker_validation_enabled

    for falsy_spelling in ("0", "false", "no", "off"):
        assert worker_validation_enabled({"MNS_WORKER_VALIDATION": falsy_spelling}) is False
    for truthy_spelling in ("1", "true", "yes", "on", ""):
        assert worker_validation_enabled({"MNS_WORKER_VALIDATION": truthy_spelling}) is True
    assert worker_validation_enabled({}) is True


def test_gunicorn_worker_validation_off_allows_multi_worker(monkeypatch):
    """gunicorn's on_starting parses MNS_WORKER_VALIDATION independently and must honor "off"."""
    monkeypatch.setenv("MNS_WORKER_VALIDATION", "off")
    config = _load_gunicorn_config()
    # Must not sys.exit: the documented opt-out spelling disable the guard.
    config.on_starting(SimpleNamespace(num_workers=2, cfg=SimpleNamespace(threads=config.required_threads)))


def test_gunicorn_worker_validation_still_rejects_multi_worker_by_default(monkeypatch):
    monkeypatch.delenv("MNS_WORKER_VALIDATION", raising=False)
    config = _load_gunicorn_config()
    with pytest.raises(SystemExit):
        config.on_starting(
            SimpleNamespace(num_workers=2, cfg=SimpleNamespace(threads=config.required_threads))
        )


# ---------------------------------------------------------------------------
# L2: the gunicorn threads ceiling must include the SSE reserve headroom
# ---------------------------------------------------------------------------


def test_gunicorn_threads_ceiling_includes_listener_max_headroom(monkeypatch):
    """At MNS_MAX_SSE_LISTENERS=1000 the required threads (1006) must be representable.

    The old hard cap of 1000 was below the documented maximum of
    ``MNS_MAX_SSE_LISTENERS + 6``, so every env-configured threads value at the
    listener maximum failed on_starting; only a CLI value outside the clamp
    could boot.
    """
    monkeypatch.setenv("MNS_MAX_SSE_LISTENERS", "1000")
    monkeypatch.setenv("MNS_GUNICORN_THREADS", "1006")
    config = _load_gunicorn_config()
    assert config.sse_listener_limit == 1000
    assert config.required_threads == 1006
    assert config.threads == 1006
    # Must not sys.exit for the env-configured maximum.
    config.on_starting(SimpleNamespace(num_workers=1, cfg=SimpleNamespace(threads=1006)))


def test_gunicorn_threads_default_cap_still_applies_for_small_listeners(monkeypatch):
    monkeypatch.setenv("MNS_MAX_SSE_LISTENERS", "64")
    monkeypatch.setenv("MNS_GUNICORN_THREADS", "2000")
    config = _load_gunicorn_config()
    # Clamped to the documented ceiling when headroom is not required.
    assert config.threads == 1000


# ---------------------------------------------------------------------------
# M40: empty MNS_PROXY_FIX_X_FOR must behave like "unset", not hops=0
# ---------------------------------------------------------------------------


def test_wsgi_empty_proxy_fix_x_for_is_treated_as_default(monkeypatch):
    """A stray ``MNS_PROXY_FIX_X_FOR=`` line must not FATAL-exit wsgi.

    ``os.environ.get`` returns "" (not the default) for a set-but-empty
    variable, so int("") -> ValueError -> hops=0 -> hard exit, while every
    other layer treats the value as unset (default 1).
    """
    monkeypatch.setenv("MNS_ALLOW_REMOTE_API", "1")
    monkeypatch.setenv("MNS_ADMIN_TOKEN", "x" * 32)
    monkeypatch.setenv("MNS_PROXY_FIX", "1")
    monkeypatch.setenv("MNS_PROXY_FIX_X_FOR", "")
    import wsgi as wsgi_mod

    # Must not raise (SystemExit propagated as an import failure).
    importlib.reload(wsgi_mod)


def test_wsgi_invalid_proxy_fix_x_for_still_refuses_to_start(monkeypatch):
    monkeypatch.setenv("MNS_ALLOW_REMOTE_API", "1")
    monkeypatch.setenv("MNS_ADMIN_TOKEN", "x" * 32)
    monkeypatch.setenv("MNS_PROXY_FIX", "1")
    monkeypatch.setenv("MNS_PROXY_FIX_X_FOR", "not-a-number")
    import wsgi as wsgi_mod

    with pytest.raises(SystemExit):
        importlib.reload(wsgi_mod)
    # Restore a clean wsgi module for later tests.
    monkeypatch.setenv("MNS_PROXY_FIX_X_FOR", "1")
    importlib.reload(wsgi_mod)


# ---------------------------------------------------------------------------
# M42: canonical boolean spellings at the ad-hoc parse sites
# ---------------------------------------------------------------------------


def test_sse_ticket_cookie_accepts_on_spelling_for_secure(monkeypatch):
    """``MNS_COOKIE_SECURE=on`` must set the Secure attribute like ``=1`` does.

    security_config.py sets the session cookie Secure for the documented
    ``on`` spelling, while the SSE ticket cookie parsed the same variable with
    a tuple missing ``on`` — two different answers for one variable.
    """
    from app_factory.factory import create_app

    monkeypatch.setenv("MNS_COOKIE_SECURE", "on")
    app = create_app(skip_bootstrap=True)
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["_sse_sid"] = "test_session_id_on_spelling"
        resp = client.post(
            "/api/stocks/stream/ticket",
            headers={"Origin": "http://127.0.0.1:5000"},
        )
        assert resp.status_code == 200
        cookie_header = resp.headers.get("Set-Cookie", "")
        assert "sse_ticket=" in cookie_header
        assert "Secure" in cookie_header


def test_browser_trends_env_accepts_canonical_spellings(monkeypatch):
    """``MNS_USE_BROWSER_TRENDS=on`` must enable BrowserConfig like ``=1`` does.

    The strict ``== "1"`` comparison ignored the documented truthy spellings,
    so operators configuring browser mode with ``on``/``true`` silently got
    the plain HTTP client.
    """
    import trend_sources

    for spelling in CANONICAL_TRUTHY:
        monkeypatch.setenv("MNS_USE_BROWSER_TRENDS", spelling)
        # Clients are cached per market (M12); each spelling must construct
        # a fresh BrowserConfig, so clear the cache between iterations.
        trend_sources._GOOGLE_TRENDS_CLIENTS.clear()
        with (
            patch.object(trend_sources, "BrowserConfig") as browser_cfg,
            patch.object(trend_sources, "TrendReq"),
        ):
            trend_sources._google_trends_client("us")
        browser_cfg.assert_called_once_with(headless=True)


def test_canonical_truthy_spellings_are_parsed_at_every_ad_hoc_site():
    """Env spellings at fixed ad-hoc sites must match the env_helpers convention.

    Covers the sites whose behavior is a caller-visible flag or a boot-time
    gate that cannot be exercised hermetically: the local rate-limit
    startup warning, and the ephemeral-crypto fail-closed gate.
    """
    factory_src = _read("app_factory/factory.py")
    factory_match = re.search(
        r'MNS_DISABLE_LOCAL_RATE_LIMIT.*?in \(\n((?:\s*"[^"]+",?\n)+)\s*\)',
        factory_src,
        re.DOTALL,
    )
    assert factory_match is not None, "MNS_DISABLE_LOCAL_RATE_LIMIT truthy tuple not found"
    factory_values = re.findall(r'"([^"]+)"', factory_match.group(1))
    assert tuple(factory_values) == CANONICAL_TRUTHY

    master_key_src = _read("config/master_key.py")
    assert '"MNS_EPHEMERAL_FALLBACK", "").strip().lower() in ("1", "true", "yes", "on")' in master_key_src

    crypto_src = _read("crypto_utils.py")
    assert (
        '"MNS_EPHEMERAL_FALLBACK", "").strip().lower() in ("1", "true", "yes", "on")' in crypto_src
    )


# ---------------------------------------------------------------------------
# M43: leader re-acquisition must not self-destruct on Windows
# ---------------------------------------------------------------------------


def test_leader_reacquire_with_open_mirror_returns_true_and_keeps_handle(tmp_path):
    """Re-asserting leadership with an open mirror handle must succeed on Windows.

    ``msvcrt.locking`` cannot re-lock the same byte range through the same fd
    (EACCES even for the lock owner), so the old code returned False and, via
    its finally block, closed the mirror handle — silently releasing the lock
    we still held. flock (POSIX) is idempotent, so only Windows was broken.
    """
    import app_bg

    with patch("app_bg.APP_DATA_DIR", tmp_path):
        assert app_bg._try_acquire_leader_lock() is True
        handle = app_bg._LEADER_LOCK_FILE
        assert handle is not None

        # Re-election tick while still holding the lock.
        assert app_bg._try_acquire_leader_lock() is True
        assert app_bg._LEADER_LOCK_FILE is handle
        assert not handle.closed

        handle.close()
        app_bg._LEADER_LOCK_FILE = None


def test_leader_pid_is_refreshed_on_reacquire(tmp_path):
    """Re-acquisition refreshes the recorded owner pid in the lock file."""
    import app_bg

    with patch("app_bg.APP_DATA_DIR", tmp_path):
        assert app_bg._try_acquire_leader_lock() is True
        handle = app_bg._LEADER_LOCK_FILE
        with patch("os.getpid", return_value=654321):
            assert app_bg._try_acquire_leader_lock() is True
        handle.close()
        app_bg._LEADER_LOCK_FILE = None

        lock_content = (tmp_path / ".mns_sync_leader.lock").read_text(encoding="utf-8")
        assert lock_content.strip() == "654321"
