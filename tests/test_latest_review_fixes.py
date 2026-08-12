"""Unit tests for the latest code review fixes (R1-R5)."""

import unittest
from unittest.mock import patch

from services.ai_service import _supports_reasoning_effort


class TestLatestReviewFixes(unittest.TestCase):
    def test_r5_supports_reasoning_effort_enhancements(self):
        """Test R5: _supports_reasoning_effort recognises reasoning/thinking models and custom extra models."""
        # Built-in reasoning models
        self.assertTrue(_supports_reasoning_effort("mistral-small-2603"))
        self.assertTrue(_supports_reasoning_effort("mistral-medium-2604"))
        self.assertTrue(_supports_reasoning_effort("mistral-small-latest"))
        self.assertTrue(_supports_reasoning_effort("mistral-medium-latest"))

        # Prefix detection
        self.assertTrue(_supports_reasoning_effort("mistral-small-custom-v1"))
        self.assertTrue(_supports_reasoning_effort("mistral-medium-custom-v1"))
        self.assertTrue(_supports_reasoning_effort("magistral-preview"))

        # Enhanced keyword detection (reasoning / thinking)
        self.assertTrue(_supports_reasoning_effort("mistral-large-reasoning-preview"))
        self.assertTrue(_supports_reasoning_effort("codestral-thinking-2601"))

        # Standard non-reasoning models
        self.assertFalse(_supports_reasoning_effort("mistral-tiny"))
        self.assertFalse(_supports_reasoning_effort("open-mistral-7b"))
        self.assertFalse(_supports_reasoning_effort(""))

    def test_r3_native_host_security_audit_logging_on_fallback(self):
        """Test R3: Native Host emits detailed security audit log when ancestor process lookup is empty."""
        from native_host.native_host import _is_caller_authorized_browser

        with patch("native_host.native_host._get_ancestor_process_names", return_value=[]), patch(
            "native_host.native_host.logger.info"
        ) as mock_info:
            allowed = _is_caller_authorized_browser()
            self.assertTrue(allowed)
            mock_info.assert_called_once()
            call_args = mock_info.call_args[0]
            self.assertIn("Security Audit", call_args[0])

    def test_r2_auto_remove_persist_error_schedules_sync(self):
        """Test R2: When auto-remove persistence fails, a follow-up sync is scheduled for self-healing."""
        from app_bg import _auto_remove_invalid_symbols
        from app_state import app_state

        items = [("INVALID_SYM", "Invalid Test Stock", "us")]
        fetched = [("__INVALID_SYMBOL__", "INVALID_SYM")]

        with app_state.market.invalid_symbol_lock:
            app_state.market.invalid_symbol_streak["INVALID_SYM"] = 5

        with patch("app_bg._get_stock_container", return_value={"INVALID_SYM": {"symbol": "INVALID_SYM"}}), patch(
            "app_bg.save_user_stocks", side_effect=OSError("Disk write error")
        ), patch("app_bg.schedule_sync_all_stocks_now") as mock_schedule:
            _auto_remove_invalid_symbols(items, fetched)
            mock_schedule.assert_called_once()


if __name__ == "__main__":
    unittest.main()
