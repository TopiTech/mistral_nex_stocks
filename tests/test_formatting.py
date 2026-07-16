"""
Tests for utils/formatting.py — datetime parsing and fallback analysis result.
"""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.formatting import _parse_datetime_to_utc, build_fallback_analysis_result


class ParseDatetimeToUtcTestCase(unittest.TestCase):
    """_parse_datetime_to_utc のテスト"""

    def test_none_returns_none(self):
        self.assertIsNone(_parse_datetime_to_utc(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(_parse_datetime_to_utc(""))

    def test_whitespace_string_returns_none(self):
        self.assertIsNone(_parse_datetime_to_utc("   "))

    def test_unix_timestamp_seconds(self):
        # Use current timestamp to verify correct parsing
        now = datetime.now(timezone.utc)
        ts = str(int(now.timestamp()))
        dt = _parse_datetime_to_utc(ts)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, now.year)
        self.assertEqual(dt.month, now.month)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_rfc2822_date(self):
        # "Wed, 15 Jan 2026 12:00:00 GMT"
        dt = _parse_datetime_to_utc("Wed, 15 Jan 2026 12:00:00 GMT")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 1)
        self.assertEqual(dt.day, 15)
        self.assertEqual(dt.hour, 12)

    def test_basic_utc_timestamp_format(self):
        dt = _parse_datetime_to_utc("20260115T120000Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 1)
        self.assertEqual(dt.day, 15)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_iso8601_with_z(self):
        dt = _parse_datetime_to_utc("2026-01-15T12:00:00Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_iso8601_with_offset(self):
        dt = _parse_datetime_to_utc("2026-01-15T21:00:00+09:00")
        self.assertIsNotNone(dt)
        # +09:00 → UTC = 12:00
        self.assertEqual(dt.hour, 12)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_iso8601_naive_treated_as_utc(self):
        dt = _parse_datetime_to_utc("2026-01-15T12:00:00")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_invalid_format_returns_none(self):
        self.assertIsNone(_parse_datetime_to_utc("not-a-date-xyz"))

    def test_overflow_timestamp_returns_none(self):
        self.assertIsNone(_parse_datetime_to_utc("9999999999999999999"))

    def test_rfc2822_alternate_format(self):
        # RFC 2822: "15 Jan 2026 12:00:00 +0000"
        dt = _parse_datetime_to_utc("15 Jan 2026 12:00:00 +0000")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.tzinfo, timezone.utc)


class BuildFallbackAnalysisResultTestCase(unittest.TestCase):
    """build_fallback_analysis_result のテスト"""

    def test_default_reason(self):
        result = build_fallback_analysis_result()
        self.assertEqual(result.get("recommendation"), "中立")
        self.assertEqual(result.get("sentiment"), "中立")
        self.assertEqual(result.get("target_price_3m"), 0)
        self.assertTrue(result.get("fallback_used"))
        self.assertIn("保守的に中立判定", result.get("analysis_summary", ""))

    def test_with_reason(self):
        result = build_fallback_analysis_result("API timeout after 30s")
        self.assertTrue(result.get("fallback_used"))
        self.assertIn("API timeout", result.get("analysis_summary", ""))

    def test_reason_truncated(self):
        long_reason = "X" * 200
        result = build_fallback_analysis_result(long_reason)
        self.assertLessEqual(len(result.get("analysis_summary", "")), 120)

    def test_all_default_keys_present(self):
        result = build_fallback_analysis_result("error")
        expected_keys = {
            "recommendation",
            "sentiment",
            "target_price_3m",
            "upside_3m",
            "confidence",
            "analysis_summary",
            "key_catalysts",
            "risk_factors",
            "technical_analysis",
            "fundamental_analysis",
            "latest_news_impact",
            "fallback_used",
        }
        self.assertTrue(expected_keys.issubset(result.keys()))

    def test_catalysts_and_risks_are_lists(self):
        result = build_fallback_analysis_result()
        self.assertIsInstance(result.get("key_catalysts"), list)
        self.assertIsInstance(result.get("risk_factors"), list)


if __name__ == "__main__":
    unittest.main()
