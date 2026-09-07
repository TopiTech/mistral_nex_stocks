# tests/test_code_review_goal_audit_2026_09_v4.py
"""Regression tests for code review audit fixes v4 (September 2026).

Covers:
- R22: api_indices forwards force=True to schedule_sync_all_stocks_now, and routes.stocks.common return annotation is bool.
- R23: routes.api_stocks re-exports is_allowed_trusted_origin in __all__ and matches utils.networking.
- R24: Cross-origin untrusted Origin check on operational endpoints (/api/cache-stats, /api/metrics, /api/system/ai-usage).
- R25: ARIA group roles and labels on toggle button groups in templates/heatmap.html and templates/index.html, plus keyboard navigation.
"""

from __future__ import annotations

import inspect
import json
import os
import pathlib
import unittest
from unittest.mock import MagicMock, patch

from bs4 import BeautifulSoup

import routes.api_stocks
import routes.stocks.common
import utils.networking
from app import app


class TestApiIndicesForceSyncAndAnnotation(unittest.TestCase):
    """Test R22: api_indices force sync forwarding and return type annotation."""

    def setUp(self) -> None:
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()

    def test_schedule_sync_all_stocks_now_return_annotation_is_bool(self) -> None:
        """routes.stocks.common.schedule_sync_all_stocks_now must return bool."""
        sig = inspect.signature(routes.stocks.common.schedule_sync_all_stocks_now)
        self.assertIn(sig.return_annotation, (bool, "bool"))

    def test_api_indices_forwards_force_true_parameter(self) -> None:
        """GET /api/indices?force=true must invoke schedule_sync_all_stocks_now with force=True."""
        with (
            patch("routes.stocks.quotes.require_trusted_or_admin", return_value=(True, "")),
            patch("routes.stocks.quotes.schedule_sync_all_stocks_now") as mock_sync,
            patch(
                "routes.stocks.quotes.resolve_indices_for_response",
                return_value={"^N225": {"price": 38000}},
            ),
        ):
            resp = self.client.get("/api/indices?force=true")
            self.assertEqual(resp.status_code, 200)
            mock_sync.assert_called_once_with(force=True)

    def test_api_indices_does_not_sync_without_force(self) -> None:
        """GET /api/indices without force or with force=false must not invoke schedule_sync_all_stocks_now."""
        with (
            patch("routes.stocks.quotes.require_trusted_or_admin", return_value=(True, "")),
            patch("routes.stocks.quotes.schedule_sync_all_stocks_now") as mock_sync,
            patch(
                "routes.stocks.quotes.resolve_indices_for_response",
                return_value={"^N225": {"price": 38000}},
            ),
        ):
            resp = self.client.get("/api/indices")
            self.assertEqual(resp.status_code, 200)
            mock_sync.assert_not_called()

            resp_false = self.client.get("/api/indices?force=false")
            self.assertEqual(resp_false.status_code, 200)
            mock_sync.assert_not_called()


class TestApiStocksOriginHelperExport(unittest.TestCase):
    """Test R23: routes.api_stocks re-exports is_allowed_trusted_origin."""

    def test_is_allowed_trusted_origin_exported_in_all(self) -> None:
        """is_allowed_trusted_origin must be exported in routes.api_stocks.__all__."""
        self.assertIn("is_allowed_trusted_origin", routes.api_stocks.__all__)
        self.assertTrue(hasattr(routes.api_stocks, "is_allowed_trusted_origin"))
        self.assertIs(
            routes.api_stocks.is_allowed_trusted_origin,
            utils.networking.is_allowed_trusted_origin,
        )

    def test_exported_origin_helper_functional(self) -> None:
        """Calling routes.api_stocks.is_allowed_trusted_origin checks origins properly."""
        trusted_req = MagicMock()
        trusted_req.headers = {"Origin": "http://127.0.0.1:5000"}
        self.assertTrue(routes.api_stocks.is_allowed_trusted_origin(trusted_req))

        untrusted_req = MagicMock()
        untrusted_req.headers = {"Origin": "https://malicious.evil.com"}
        self.assertFalse(routes.api_stocks.is_allowed_trusted_origin(untrusted_req))


class TestSystemOperationalEndpointsOriginDefense(unittest.TestCase):
    """Test R24: Cross-origin untrusted Origin check on operational endpoints."""

    def setUp(self) -> None:
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()

    def test_endpoints_allow_local_request_without_origin(self) -> None:
        """Same-origin GET requests without Origin header succeed for local callers."""
        endpoints = ["/api/cache-stats", "/api/metrics", "/api/system/ai-usage"]
        for ep in endpoints:
            resp = self.client.get(ep, environ_base={"REMOTE_ADDR": "127.0.0.1"})
            self.assertEqual(
                resp.status_code, 200, f"Endpoint {ep} should succeed for local request"
            )
            data = json.loads(resp.data)
            self.assertTrue(data.get("ok"))

    def test_endpoints_allow_trusted_loopback_origin(self) -> None:
        """GET requests with trusted loopback Origin succeed."""
        endpoints = ["/api/cache-stats", "/api/metrics", "/api/system/ai-usage"]
        for ep in endpoints:
            resp = self.client.get(
                ep,
                headers={"Origin": "http://127.0.0.1:5000"},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
            self.assertEqual(
                resp.status_code, 200, f"Endpoint {ep} should succeed with trusted Origin"
            )
            data = json.loads(resp.data)
            self.assertTrue(data.get("ok"))

    def test_endpoints_reject_untrusted_origin(self) -> None:
        """GET requests from loopback with untrusted Origin header must be rejected with 403."""
        endpoints = ["/api/cache-stats", "/api/metrics", "/api/system/ai-usage"]
        for ep in endpoints:
            resp = self.client.get(
                ep,
                headers={"Origin": "https://attacker.evil.com"},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
            self.assertEqual(
                resp.status_code,
                403,
                f"Endpoint {ep} must reject untrusted Origin with 403 Forbidden",
            )
            data = json.loads(resp.data)
            self.assertFalse(data.get("ok"))
            self.assertEqual(data.get("details", {}).get("reason"), "untrusted origin")

    def test_endpoints_remote_mode_with_admin_token(self) -> None:
        """In remote mode with valid admin token, origin check is deferred to admin auth."""
        admin_token = "a" * 32
        with patch.dict(os.environ, {"MNS_ALLOW_REMOTE_API": "1", "MNS_ADMIN_TOKEN": admin_token}):
            with patch(
                "routes.api_system.get_api_credential_state",
                return_value={"admin_token": admin_token},
            ):
                endpoints = ["/api/cache-stats", "/api/metrics", "/api/system/ai-usage"]
                for ep in endpoints:
                    resp = self.client.get(
                        ep,
                        headers={
                            "Origin": "https://external.authorized-domain.com",
                            "X-MNS-Admin-Token": admin_token,
                        },
                        environ_base={"REMOTE_ADDR": "192.168.1.50"},
                    )
                    self.assertEqual(
                        resp.status_code,
                        200,
                        f"Endpoint {ep} with valid admin token in remote mode should succeed",
                    )


class TestHeatmapAndIndexAriaAccessibility(unittest.TestCase):
    """Test R25: ARIA group roles, labels, and keyboard navigation."""

    def test_heatmap_html_toggle_groups_have_aria_roles_and_labels(self) -> None:
        """Toggle containers in templates/heatmap.html must have role='group' and aria-label."""
        html_content = pathlib.Path("templates/heatmap.html").read_text(encoding="utf-8")
        soup = BeautifulSoup(html_content, "html.parser")

        group_selectors = [".market-toggle", ".view-toggle", ".size-toggle", ".cam-btn-group"]
        for selector in group_selectors:
            elem = soup.select_one(selector)
            if elem is None:
                self.fail(f"Element matching {selector} should exist")
            self.assertEqual(
                elem.get("role"),
                "group",
                f"{selector} must have role='group'",
            )
            aria_label = str(elem.get("aria-label") or "").strip()
            self.assertTrue(
                bool(aria_label),
                f"{selector} must have non-empty aria-label",
            )

    def test_heatmap_camera_buttons_have_aria_labels(self) -> None:
        """Camera control buttons in templates/heatmap.html must have aria-label."""
        html_content = pathlib.Path("templates/heatmap.html").read_text(encoding="utf-8")
        soup = BeautifulSoup(html_content, "html.parser")

        cam_btn_ids = ["cam-reset", "cam-top", "cam-iso"]
        for btn_id in cam_btn_ids:
            btn = soup.find(id=btn_id)
            if btn is None:
                self.fail(f"Button #{btn_id} should exist")
            aria_label = str(btn.get("aria-label") or "").strip()
            self.assertTrue(bool(aria_label), f"Button #{btn_id} must have aria-label")

    def test_index_html_settings_button_aria_label_japanese(self) -> None:
        """#settingsBtn in templates/index.html must have Japanese aria-label."""
        html_content = pathlib.Path("templates/index.html").read_text(encoding="utf-8")
        soup = BeautifulSoup(html_content, "html.parser")

        btn = soup.find(id="settingsBtn")
        if btn is None:
            self.fail("#settingsBtn should exist in index.html")
        self.assertEqual(btn.get("aria-label"), "設定画面を開く")

    def test_heatmap_js_has_toggle_group_keyboard_navigation(self) -> None:
        """static/js/heatmap.js must define and wire setupButtonGroupKeyboardNav."""
        js_content = pathlib.Path("static/js/heatmap.js").read_text(encoding="utf-8")

        self.assertIn("function setupButtonGroupKeyboardNav", js_content)
        self.assertIn('["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)', js_content)
        self.assertIn("setupButtonGroupKeyboardNav([els.toggleUs, els.toggleJp]", js_content)
        self.assertIn("setupButtonGroupKeyboardNav([els.view2d, els.view3d]", js_content)
        self.assertIn("setupButtonGroupKeyboardNav([els.sizeMarketCap, els.sizeVolume]", js_content)
        self.assertIn("setupButtonGroupKeyboardNav([els.camReset, els.camTop, els.camIso]", js_content)


if __name__ == "__main__":
    unittest.main()
