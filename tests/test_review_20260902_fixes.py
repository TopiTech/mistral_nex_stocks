"""Regression tests for 2026-09-02 review fixes."""

from unittest.mock import patch

import pytest

from app import create_app
from utils.chat_history import SQLiteChatHistoryStore


class TestChatHistoryBusyTimeout:
    def test_connection_has_busy_timeout(self, tmp_path, monkeypatch):
        import utils.chat_history as ch

        monkeypatch.setenv("MNS_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(ch, "DB_PATH", tmp_path / "chat_history.db")
        ch._reset_db_state()
        store = SQLiteChatHistoryStore()
        conn = store._get_connection()
        val = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert val == 5000
        store.close_all()
        ch._reset_db_state()


class TestChartImageValidation:
    @pytest.fixture
    def client(self):
        app = create_app()
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        app.config["WTF_CSRF_CHECK_DEFAULT"] = False
        return app.test_client()

    def test_rejects_http_url(self, client):
        headers = {"Origin": "http://localhost:5000", "X-Requested-With": "XMLHttpRequest"}
        with patch("routes.api_analysis.extract_api_key", return_value="dummy_key"):
            res = client.post(
                "/api/analyze-chart-image",
                json={"image_data": "https://example.com/chart.png", "symbol": "AAPL"},
                headers=headers,
            )
            assert res.status_code == 400
            assert (
                "URL形式" in res.get_json()["details"]["reason"]
                or "許可" in res.get_json()["details"]["reason"]
            )

    def test_rejects_svg_data_uri(self, client):
        headers = {"Origin": "http://localhost:5000", "X-Requested-With": "XMLHttpRequest"}
        with patch("routes.api_analysis.extract_api_key", return_value="dummy_key"):
            res = client.post(
                "/api/analyze-chart-image",
                json={"image_data": "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=", "symbol": "AAPL"},
                headers=headers,
            )
            assert res.status_code == 400
            assert "SVG" in res.get_json()["details"]["reason"]

    def test_rejects_case_variant_mismatch(self, client):
        headers = {"Origin": "http://localhost:5000", "X-Requested-With": "XMLHttpRequest"}
        with patch("routes.api_analysis.extract_api_key", return_value="dummy_key"):
            res = client.post(
                "/api/analyze-chart-image",
                json={"image_data": "data:ImAgE/svg+xml;base64,abcd", "symbol": "AAPL"},
                headers=headers,
            )
            assert res.status_code == 400
            assert "SVG" in res.get_json()["details"]["reason"]

    def test_accepts_valid_png_data_uri(self, client):
        headers = {"Origin": "http://localhost:5000", "X-Requested-With": "XMLHttpRequest"}
        b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        with (
            patch("routes.api_analysis.extract_api_key", return_value="dummy_key"),
            patch(
                "routes.api_analysis.analyze_chart_image_with_mistral",
                return_value={"ok": True, "analysis": "ok"},
            ),
        ):
            res = client.post(
                "/api/analyze-chart-image",
                json={"image_data": f"data:image/png;base64,{b64}", "symbol": "AAPL"},
                headers=headers,
            )
            assert res.status_code == 200

    def test_rejects_data_uri_without_strict_header(self, client):
        headers = {"Origin": "http://localhost:5000", "X-Requested-With": "XMLHttpRequest"}
        with patch("routes.api_analysis.extract_api_key", return_value="dummy_key"):
            res = client.post(
                "/api/analyze-chart-image",
                json={"image_data": "data:image/png,notbase64", "symbol": "AAPL"},
                headers=headers,
            )
            assert res.status_code == 400


class TestR7Legacy:
    def test_http_url_handling_removed(self):
        from services.ai_service import analyze_chart_image_with_mistral

        with patch("services.ai_service.call_mistral_chat") as mock_call:
            mock_call.return_value = {"choices": [{"message": {"content": "ok"}}]}
            res = analyze_chart_image_with_mistral("k", "https://example.com/x.png", symbol="AAPL")
            assert "choices" not in res or "error" not in res
            sent = mock_call.call_args[1]["messages"]
            url = sent[1]["content"][1]["image_url"]
            assert url == "https://example.com/x.png"
