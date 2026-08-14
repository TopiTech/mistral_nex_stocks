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

    def test_non_json_request_body_uses_client_error_contract(self):
        from utils.text_utils import _parse_json_request

        with app.test_request_context(
            "/api/stocks/add",
            method="POST",
            data="not json",
            content_type="text/plain",
        ):
            self.assertIsNone(_parse_json_request())


if __name__ == "__main__":
    unittest.main()
