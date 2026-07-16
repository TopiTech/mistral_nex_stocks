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
            "https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns/dist/chartjs-adapter-date-fns.bundle.min.js",
            "https://cdn.jsdelivr.net/npm/chartjs-chart-financial/dist/chartjs-chart-financial.min.js",
        )

        self.assertIsNotNone(csp, "CSP header missing")
        assert csp is not None
        for url in script_urls:
            match = re.search(
                rf'<script\s+nonce="([^"]+)"\s+src="{re.escape(url)}"',
                html,
                re.S,
            )
            self.assertIsNotNone(match, f"Chart.js script missing nonce: {url}")
            assert match is not None
            self.assertIn(f"'nonce-{match.group(1)}'", csp)


if __name__ == "__main__":
    unittest.main()
