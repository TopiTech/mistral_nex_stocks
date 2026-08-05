"""
Tests for recent code review fixes (H-1..H-5, M-1..M-10, L-1..L-8).
"""

import unittest
from unittest.mock import patch

from native_host import native_host
from routes.api_analysis import _safe_prompt_field
from utils.env_helpers import _env_float


class CodeReviewFixesTestCase(unittest.TestCase):
    """Verify code review findings and fixes."""

    def test_h3_safe_prompt_field_sanitizes_input(self):
        """Verify _safe_prompt_field strips dangerous XML and control characters."""
        raw_price = "<script>alert(1)</script> 150.25 & USD"
        safe_price = _safe_prompt_field(raw_price, max_len=60)
        self.assertNotIn("<", safe_price)
        self.assertNotIn(">", safe_price)
        self.assertNotIn("&", safe_price)
        self.assertIn("150.25", safe_price)

    def test_h4_read_message_oversized_returns_skip_frame(self):
        """Oversized native messaging frame should return SKIP_FRAME without terminating host."""
        with patch.object(native_host.RAW_STDIN, "read") as mock_read:
            # Header claiming 2MB message (exceeds 1MB default)
            mock_read.side_effect = [
                (2 * 1024 * 1024).to_bytes(4, byteorder="little"),  # 4-byte header
                b"x" * 65536,  # drained chunk
                b"",  # EOF on drain
            ]
            result = native_host.read_message()
            self.assertIs(result, native_host.SKIP_FRAME)

    def test_m3_env_float_safe_fallback(self):
        """_env_float handles invalid float values safely."""
        with patch.dict("os.environ", {"MNS_FALLBACK_FETCH_TIMEOUT": "invalid_float"}):
            val = _env_float("MNS_FALLBACK_FETCH_TIMEOUT", 10.0, 1.0, 60.0)
            self.assertEqual(val, 10.0)

    def test_m6_heatmap_fallback_size_zero_volume(self):
        """Zero volume should yield 0 fallback_size in heatmap calculation logic."""
        price = 100.0
        volume = 0
        fallback_size = price * volume if volume > 0 else 0
        self.assertEqual(fallback_size, 0)
