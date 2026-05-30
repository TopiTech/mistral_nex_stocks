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
            'Content-Security-Policy-Report-Only' in headers or 'Content-Security-Policy' in headers,
            f"CSP header missing, headers: {headers}"
        )

if __name__ == '__main__':
    unittest.main()
