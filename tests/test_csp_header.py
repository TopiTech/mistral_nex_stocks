import re
import unittest

from app import app


class CSPHeaderTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_csp_header_present_on_health(self):
        rv = self.client.get("/api/health")
        headers = rv.headers
        # Accept either enforcement or report-only header depending on env config
        self.assertTrue(
            "Content-Security-Policy-Report-Only" in headers
            or "Content-Security-Policy" in headers,
            f"CSP header missing, headers: {headers}",
        )

    def test_csp_nonce_is_injected_into_chart_js_scripts(self):
        rv = self.client.get("/main")
        csp = rv.headers.get("Content-Security-Policy") or rv.headers.get(
            "Content-Security-Policy-Report-Only"
        )
        html = rv.get_data(as_text=True)

        script_urls = (
            "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js",
            "https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js",
            "https://cdn.jsdelivr.net/npm/chartjs-chart-financial@0.2.1/dist/chartjs-chart-financial.min.js",
        )

        self.assertIsNotNone(csp, "CSP header missing")
        assert csp is not None
        for url in script_urls:
            match = re.search(
                rf'<script\s+nonce="([^"]+)"\s+src="{re.escape(url)}"',
                html,
                re.DOTALL,
            )
            self.assertIsNotNone(match, f"Chart.js script missing nonce: {url}")
            assert match is not None
            self.assertIn(f"'nonce-{match.group(1)}'", csp)

        self.assertNotIn(
            "https://cdn.jsdelivr.net/npm/chartjs-chart-financial/dist/",
            html,
            "Financial chart CDN URL must be pinned to an explicit package version",
        )

    def test_csp_frame_src_allows_tradingview_widget_domains(self):
        """The TradingView widgets must be framable for the chart/ticker to render.

        The ticker tape script loads from s3.tradingview.com and the Advanced
        Chart iframe is hosted on www.tradingview-widget.com (a dedicated
        widget-hosting domain, not a *.tradingview.com subdomain). If either is
        missing from frame-src the widget silently never appears.
        """
        rv = self.client.get("/api/health")
        csp = rv.headers.get("Content-Security-Policy") or rv.headers.get(
            "Content-Security-Policy-Report-Only"
        )
        self.assertIsNotNone(csp)
        assert csp is not None
        frame_src_match = re.search(r"frame-src\s+([^;]+)", csp)
        self.assertIsNotNone(frame_src_match)
        assert frame_src_match is not None
        frame_src = frame_src_match.group(1)
        self.assertIn("https://s3.tradingview.com", frame_src)
        self.assertIn("https://www.tradingview-widget.com", frame_src)

    def test_csp_report_only_mode(self):
        import os
        from unittest.mock import patch

        from app import create_app

        with patch.dict(os.environ, {"CSP_ENFORCE": "false"}):
            test_app = create_app(skip_bootstrap=True)
            test_client = test_app.test_client()
            rv = test_client.get("/api/health")
            headers = rv.headers
            self.assertIn("Content-Security-Policy-Report-Only", headers)
            self.assertNotIn("Content-Security-Policy", headers)


if __name__ == "__main__":
    unittest.main()
