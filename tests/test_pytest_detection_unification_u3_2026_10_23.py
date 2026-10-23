"""U3 regression: pytest detection unification (2026-10-23).

Guards that no non-test source sniffs the test runner via sys.modules and
that ``utils.env_helpers._is_testing()`` answers from explicit switches only
(MNS_SKIP_BOOTSTRAP / MNS_TESTING / TESTING env, Flask TESTING config).
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from flask import Flask

from services.realtime.scrapers import YahooJPRealtimeScraper
from utils.env_helpers import _is_testing

REPO_ROOT = Path(__file__).resolve().parent.parent
_SEAM_MARKERS = (
    '"pytest" in sys.modules',
    "'pytest' in sys.modules",
    '"unittest" in sys.modules',
    "'unittest' in sys.modules",
    "sys.modules.get(\"pytest\")",
    "sys.modules.get('pytest')",
)
_SEAM_SCAN_DIRS = ("app_factory", "bg", "routes", "services", "utils", "schemas")
_SEAM_SCAN_FILES = (
    "app.py",
    "app_bg.py",
    "wsgi.py",
    "route_helpers.py",
    "session_manager.py",
)


def _iter_source_files():
    for dirname in _SEAM_SCAN_DIRS:
        yield from (REPO_ROOT / dirname).rglob("*.py")
    for name in _SEAM_SCAN_FILES:
        candidate = REPO_ROOT / name
        if candidate.exists():
            yield candidate


def test_no_runner_sniffing_in_source():
    offenders = []
    for path in _iter_source_files():
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = line.split("#", 1)[0]
            for marker in _SEAM_MARKERS:
                if marker in stripped:
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{lineno}: {marker}"
                    )
    assert offenders == [], f"runner sniffing still present: {offenders}"


def test_is_testing_true_under_baseline_opt_out():
    assert os.environ.get("MNS_SKIP_BOOTSTRAP") == "1"
    assert _is_testing() is True


def test_is_testing_false_when_all_explicit_flags_off():
    with patch.dict(
        os.environ,
        {"MNS_SKIP_BOOTSTRAP": "", "MNS_TESTING": "", "TESTING": ""},
        clear=False,
    ):
        assert _is_testing() is False


def test_is_testing_honors_each_explicit_env_flag():
    base = {"MNS_SKIP_BOOTSTRAP": "", "MNS_TESTING": "", "TESTING": ""}
    for flag in ("MNS_SKIP_BOOTSTRAP", "MNS_TESTING", "TESTING"):
        env = dict(base)
        env[flag] = "1"
        with patch.dict(os.environ, env, clear=False):
            assert _is_testing() is True, flag


def test_is_testing_honors_flask_testing_config():
    flask_app = Flask(__name__)
    with patch.dict(
        os.environ,
        {"MNS_SKIP_BOOTSTRAP": "", "MNS_TESTING": "", "TESTING": ""},
        clear=False,
    ):
        assert _is_testing() is False
        flask_app.config["TESTING"] = True
        with flask_app.test_request_context():
            assert _is_testing() is True
        flask_app.config["TESTING"] = False
        with flask_app.test_request_context():
            assert _is_testing() is False


def test_is_testing_treats_invalid_flag_as_off():
    with patch.dict(
        os.environ,
        {"MNS_SKIP_BOOTSTRAP": "maybe", "MNS_TESTING": "", "TESTING": ""},
        clear=False,
    ):
        assert _is_testing() is False


def test_register_signal_handlers_skips_without_touching_signals():
    from app import _register_signal_handlers

    with patch("signal.signal", side_effect=AssertionError("must not arm")):
        assert _register_signal_handlers(MagicMock()) is None


def test_startup_ready_honors_explicit_flag_not_runner():
    scraper = YahooJPRealtimeScraper()
    mock_market = MagicMock()
    mock_market.first_sync_attempted = False
    mock_market.first_sync_completed_at = 0.0
    mock_app_state = MagicMock()
    mock_app_state.market = mock_market

    with patch("app_state.app_state", mock_app_state):
        assert scraper._is_startup_ready(force_check=False) is True
        with patch.dict(
            os.environ,
            {"MNS_SKIP_BOOTSTRAP": "", "MNS_TESTING": "", "TESTING": ""},
            clear=False,
        ):
            assert scraper._is_startup_ready(force_check=False) is False
