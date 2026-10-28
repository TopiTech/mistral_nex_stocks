"""
Tests for 2026 API boundary fixes, dependency vulnerability remediation, and client guards.

Verifies:
1. TASK-01: pylint is updated to 4.0.9 in pyproject.toml and requirements-lint.txt,
   and pip-audit --strict passes cleanly with no vulnerabilities.
2. TASK-02: README.md documents Python 3.11–3.14.
3. TASK-03: _chat_error_response includes error, message, reply (all identical),
   and code in its JSON payload; api_client.ts handles data.reply fallback.
4. TASK-08: bulkAnalyzeFavorites in static/js/api.js handles missing stock data
   by recording failure and incrementing completedCount, ensuring progress reaches 100%.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import requests
from flask import Flask, g

from routes.api_analysis import _chat_error_response

ROOT_DIR = Path(__file__).resolve().parent.parent


class TestChatErrorResponseEnvelope:
    """Verify TASK-03: _chat_error_response payload consistency."""

    @pytest.fixture(autouse=True)
    def setup_app_context(self) -> Any:
        app = Flask(__name__)
        with app.test_request_context():
            g.request_id = "test-req-boundary-2026"
            yield

    @pytest.mark.parametrize(
        ("exc", "expected_status", "keyword_in_msg"),
        [
            (ValueError("不正なパラメータ"), 400, "入力データが不正です"),
            (TypeError("型の不整合"), 400, "入力データが不正です"),
            (Exception("401 Unauthorized"), 401, "AI APIの認証に失敗しました"),
            (Exception("API認証エラー"), 401, "API認証エラー"),
            (requests.ConnectionError("接続失敗"), 503, "AIサービスに接続できませんでした"),
            (httpx.ConnectError("Connection refused"), 503, "AIサービスに接続できませんでした"),
            (httpx.TimeoutException("Read timed out"), 504, "AIサービス呼び出しがタイムアウトしました"),
            (requests.Timeout("Request timeout"), 504, "AIサービス呼び出しがタイムアウトしました"),
            (RuntimeError("想定外の内部障害"), 500, "チャット処理に失敗しました"),
        ],
    )
    def test_chat_error_response_keys_and_values(
        self, exc: Exception, expected_status: int, keyword_in_msg: str
    ) -> None:
        token = "token-test-1234"
        resp, status = _chat_error_response(exc, g, operation_token=token)
        assert status == expected_status

        data = resp.get_json()
        assert data is not None
        assert "reply" in data
        assert "error" in data
        assert "message" in data
        assert "code" in data
        assert "disclaimer" in data
        assert data.get("request_token") == token

        # Verify equivalence
        assert data["error"] == data["reply"]
        assert data["message"] == data["reply"]
        assert data["code"] == expected_status
        assert keyword_in_msg in data["reply"]

    def test_chat_error_response_without_token(self) -> None:
        resp, status = _chat_error_response(ValueError("invalid"), g, operation_token=None)
        assert status == 400
        data = resp.get_json()
        assert data is not None
        assert "request_token" not in data
        assert data["error"] == data["message"] == data["reply"] == "入力データが不正です"
        assert data["code"] == 400


class TestApiClientAndFrontendGuards:
    """Verify TASK-03 & TASK-08 frontend implementations."""

    def test_api_client_error_msg_fallback_support(self) -> None:
        ts_content = (ROOT_DIR / "static" / "js" / "api_client.ts").read_text(encoding="utf-8")
        assert 'typeof data.reply === "string" ? data.reply : undefined' in ts_content

    def test_bulk_analyze_favorites_guard_increments_completed(self) -> None:
        api_js = (ROOT_DIR / "static" / "js" / "api.js").read_text(encoding="utf-8")
        guard_match = re.search(
            r"const stock = getStockByKey\(stockKey\);\s*if \(!stock\) \{(.*?continue;\s*\})",
            api_js,
            re.DOTALL,
        )
        assert guard_match is not None, "Guard if (!stock) block not found in static/js/api.js"
        guard_body = guard_match.group(1)

        assert "failed.push(" in guard_body
        assert "銘柄データが見つかりません" in guard_body
        assert "completedCount++;" in guard_body
        assert "continue;" in guard_body


class TestDependencyAuditAndDocConsistency:
    """Verify TASK-01 and TASK-02."""

    def test_python_version_in_readme(self) -> None:
        readme = (ROOT_DIR / "README.md").read_text(encoding="utf-8")
        assert "Python 3.11–3.14" in readme
        assert "Python 3.12–3.14" not in readme

    def test_pylint_pinned_versions(self) -> None:
        pyproject = (ROOT_DIR / "pyproject.toml").read_text(encoding="utf-8")
        assert '"pylint==4.0.9"' in pyproject
        req_lint = (ROOT_DIR / "requirements-lint.txt").read_text(encoding="utf-8")
        assert "pylint==4.0.9" in req_lint

    def test_pip_audit_cleanliness(self) -> None:
        venv_pip_audit = (
            ROOT_DIR
            / ".venv"
            / ("Scripts" if sys.platform == "win32" else "bin")
            / ("pip-audit.exe" if sys.platform == "win32" else "pip-audit")
        )
        if venv_pip_audit.exists():
            pip_audit = str(venv_pip_audit)
        else:
            pip_audit = shutil.which("pip-audit")

        if pip_audit is None:
            pytest.skip("pip-audit executable not found in PATH or virtualenv")

        res = subprocess.run(
            [pip_audit, "--strict"],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            check=False,
        )
        combined_output = f"{res.stdout}\n{res.stderr}"
        assert res.returncode == 0, f"pip-audit --strict failed:\n{combined_output}"
        assert "No known vulnerabilities found" in combined_output
