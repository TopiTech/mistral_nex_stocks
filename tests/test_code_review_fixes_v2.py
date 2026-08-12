import json
import unittest

from config_store import _MERGE_SEED_KEYS
from routes.api_stocks import _stock_display_name
from utils.validators import extract_json_payload


class TestCodeReviewFixesV2(unittest.TestCase):
    def test_r1_merge_seed_keys(self):
        """[R1] Ensure custom_ai_prompt is excluded from _MERGE_SEED_KEYS."""
        self.assertNotIn("custom_ai_prompt", _MERGE_SEED_KEYS)
        self.assertIn("mistral_model", _MERGE_SEED_KEYS)

    def test_r2_stock_display_name_fallback(self):
        """[R2] Verify _stock_display_name fallback behavior."""
        name = _stock_display_name("AAPL", "us")
        self.assertTrue(len(name) > 0)

    def test_r5_extract_json_payload_length_cap(self):
        """[R5] Test input capping and Stage 4 exception handling in extract_json_payload."""
        large_json = '{"recommendation": "買い", "sentiment": "強気", "target_price_3m": 200} ' + (
            " " * 60000
        )
        res = extract_json_payload(large_json)
        data = json.loads(res)
        self.assertEqual(data.get("recommendation"), "買い")


if __name__ == "__main__":
    unittest.main()
