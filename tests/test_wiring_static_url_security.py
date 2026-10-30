"""Security and robustness tests for static_url cache-busting in app_factory.wiring."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from flask import Flask

from app_factory.wiring import _configure_static_cache_buster


def test_static_url_normal_file() -> None:
    """Verify that an existing static file receives a cache-busting query parameter."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_file = Path(tmp_dir) / "app.css"
        test_file.write_text("body { color: red; }", encoding="utf-8")

        app = Flask(__name__, static_folder=tmp_dir, static_url_path="/static")
        _configure_static_cache_buster(app)

        with app.test_request_context():
            static_url_func = app.jinja_env.globals.get("static_url")
            assert callable(static_url_func)

            url = static_url_func("app.css")
            expected_mtime = int(os.path.getmtime(test_file))
            assert url == f"/static/app.css?v={expected_mtime}"

            # Second call should return cached value
            cached_url = static_url_func("app.css")
            assert cached_url == url


def test_static_url_directory_traversal_fallbacks_safely() -> None:
    """Verify that traversal paths immediately fallback to url_for without cache busting."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Create a file outside static folder
        outside_file = Path(tmp_dir).parent / "outside_secret.txt"
        outside_file.write_text("secret_data", encoding="utf-8")

        static_dir = Path(tmp_dir) / "static"
        static_dir.mkdir()

        app = Flask(__name__, static_folder=str(static_dir), static_url_path="/static")
        _configure_static_cache_buster(app)

        with app.test_request_context():
            static_url_func = app.jinja_env.globals.get("static_url")
            assert callable(static_url_func)

            traversal_cases = [
                "../outside_secret.txt",
                "../../etc/passwd",
                "../../../boot.ini",
                "..\\outside_secret.txt",
                "..\\..\\windows\\win.ini",
                "/etc/passwd",
                "/windows/system32/cmd.exe",
                "sub/../../outside_secret.txt",
            ]

            for bad_path in traversal_cases:
                result = static_url_func(bad_path)
                # Must not contain cache-busting query parameter
                assert "?v=" not in result
                assert (
                    "outside_secret.txt" in result
                    or "passwd" in result
                    or "ini" in result
                    or "cmd.exe" in result
                )

        try:
            outside_file.unlink(missing_ok=True)
        except OSError:
            pass


def test_static_url_nonexistent_file() -> None:
    """Verify that nonexistent static files fallback to url_for without raising exceptions."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        app = Flask(__name__, static_folder=tmp_dir, static_url_path="/static")
        _configure_static_cache_buster(app)

        with app.test_request_context():
            static_url_func = app.jinja_env.globals.get("static_url")
            assert callable(static_url_func)

            url = static_url_func("nonexistent_script.js")
            assert url == "/static/nonexistent_script.js"
            assert "?v=" not in url


def test_static_url_empty_static_folder() -> None:
    """Verify safe fallback when static_folder is empty or None."""
    app = Flask(__name__, static_folder=None)
    app.add_url_rule("/static/<path:filename>", endpoint="static")
    _configure_static_cache_buster(app)

    with app.test_request_context():
        static_url_func = app.jinja_env.globals.get("static_url")
        assert callable(static_url_func)

        url = static_url_func("style.css")
        assert url == "/static/style.css"
        assert "?v=" not in url
