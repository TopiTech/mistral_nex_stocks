import unittest
from app import app


class CSPReportTest(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_csp_report_endpoint(self):
        rv = self.client.post(
            "/api/csp-report",
            json={"document-uri": "http://localhost/", "violated-directive": "script-src"},
        )
        self.assertIn(rv.status_code, (200, 204))


if __name__ == "__main__":
    unittest.main()
