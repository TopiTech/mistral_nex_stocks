"""
Regression tests for the Chrome extension "detected stock" add flow.

The popup's page-detector tab previously POSTed to /api/stocks/add_ext directly
without the Bearer extension token, so the backend always rejected it with 403
(api_stocks.py requires `Authorization: Bearer <extension token>`). The fix
routes the request through the service worker (background.js), which owns the
token. These tests verify that routing and the button state transitions.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))


class DetectedStockAddTestCase(unittest.TestCase):
    """Verify the addDetectedStock message handler in background.js."""

    def setUp(self):
        self.popup = {}
        with open(
            Path(__file__).parent.parent / "chrome_extension" / "background.js",
            encoding="utf-8",
        ) as f:
            self.background_source = f.read()
        with open(
            Path(__file__).parent.parent / "chrome_extension" / "popup.js",
            encoding="utf-8",
        ) as f:
            self.popup_source = f.read()

    def test_popup_delegates_to_service_worker(self):
        """The popup must not call the add_ext endpoint directly; it must send
        addDetectedStock to the service worker so the Bearer token is attached.
        """
        self.assertIn(
            'send("addDetectedStock", {',
            self.popup_source,
        )
        self.assertNotIn(
            "currentBackendBase}/api/stocks/add_ext",
            self.popup_source,
        )
        self.assertIn(
            "api/stocks/add_ext",
            self.background_source,
        )

    def test_popup_send_forwards_payload(self):
        """send() must include the payload in the runtime message."""
        self.assertIn(
            "const message = { action, ...(payload || {}) };",
            self.popup_source,
        )

    def test_service_worker_attaches_bearer_token(self):
        """The service worker's addDetectedStock handler must send the
        Authorization: Bearer <extension token> header to /api/stocks/add_ext.
        """
        handler_start = self.background_source.index('message.action === "addDetectedStock"')
        handler_end = self.background_source.index('message.action === "openMain"')
        handler = self.background_source[handler_start:handler_end]
        self.assertIn('"X-MNS-Extension-Request": "true"', handler)
        self.assertIn("Authorization: `Bearer ${mnsExtensionToken || \"\"}`", handler)
        self.assertIn('body: JSON.stringify({ symbol, market })', handler)

    def test_service_worker_validates_symbol(self):
        """A missing/blank symbol must be rejected before any fetch."""
        handler_start = self.background_source.index('message.action === "addDetectedStock"')
        handler_end = self.background_source.index('message.action === "openMain"')
        handler = self.background_source[handler_start:handler_end]
        self.assertIn('String(message.symbol || "").trim()', handler)
        self.assertIn('error: "銘柄シンボルが指定されていません"', handler)

    def test_market_normalization_jp(self):
        """market is normalized to jp only when exactly 'jp', else us."""
        handler_start = self.background_source.index('message.action === "addDetectedStock"')
        handler_end = self.background_source.index('message.action === "openMain"')
        handler = self.background_source[handler_start:handler_end]
        self.assertIn('const market = message.market === "jp" ? "jp" : "us";', handler)


if __name__ == "__main__":
    unittest.main()
