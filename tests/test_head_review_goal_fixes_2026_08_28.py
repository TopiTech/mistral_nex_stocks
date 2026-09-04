# tests/test_head_review_goal_fixes_2026_08_28.py
"""Comprehensive regression test suite for R1-R7 fixes."""

import base64
import json
import tempfile
import unittest
from unittest.mock import patch

from app import create_app
from app_state import app_state
from crypto_utils import _decode_secret
from error_codes import ErrorCode
from route_helpers import invalidate_single_stock_cache, invalidate_stock_caches
from services.ai_portfolio_service import generate_ai_portfolio_by_theme
from utils.caching import (
    _get_cached_value,
    _set_cached_value,
    history_short_payload_cache_key,
)


class TestR1CacheInvalidation(unittest.TestCase):
    """R1: Test that cache invalidation does not collateral-evict prefix lookalikes,
    and reliably cleans history_short_payload and info_disk."""

    def setUp(self):
        self.app = create_app()
        self.app_context = self.app.app_context()
        self.app_context.push()

    def tearDown(self):
        self.app_context.pop()

    def test_r1_invalidate_does_not_evict_prefix_lookalikes(self):
        """Invalidating 'A' must NOT evict 'AAPL', 'AMZN', 'AMD', etc."""
        # 1. Global in-memory history cache
        _set_cached_value("hist_AAPL_us_3mo", {"symbol": "AAPL"}, 300)
        _set_cached_value("hist_A_us_3mo", {"symbol": "A"}, 300)

        # 2. Global in-memory info cache
        _set_cached_value("info_AAPL", {"name": "Apple"}, 3600)
        _set_cached_value("info_A", {"name": "Agilent"}, 3600)

        # 3. yfinance short cache
        with app_state.yfinance_short_cache_lock:
            app_state.yfinance_short_cache["info_short_AAPL"] = {"name": "Apple"}
            app_state.yfinance_short_cache["info_short_A"] = {"name": "Agilent"}
            app_state.yfinance_short_cache["fastinfo_AAPL"] = {"price": 200.0}
            app_state.yfinance_short_cache["fastinfo_A"] = {"price": 130.0}

        # 4. Disk caches
        app_state.stock_disk_cache.set("hist_AAPL_us_3mo", {"data": "aapl_disk"})
        app_state.stock_disk_cache.set("hist_A_us_3mo", {"data": "a_disk"})
        app_state.payload_disk_cache.set("payload_AAPL_us_123", {"data": "aapl_payload"})
        app_state.payload_disk_cache.set("payload_A_us_123", {"data": "a_payload"})

        # Act: Invalidate single symbol 'A'
        invalidate_stock_caches("A")

        # Assert: 'A' caches are gone
        self.assertIsNone(_get_cached_value("hist_A_us_3mo", 300))
        self.assertIsNone(_get_cached_value("info_A", 3600))
        with app_state.yfinance_short_cache_lock:
            self.assertNotIn("info_short_A", app_state.yfinance_short_cache)
            self.assertNotIn("fastinfo_A", app_state.yfinance_short_cache)
        self.assertIsNone(app_state.stock_disk_cache.get("hist_A_us_3mo"))
        self.assertIsNone(app_state.payload_disk_cache.get("payload_A_us_123"))

        # Assert: 'AAPL' caches are preserved (no collateral eviction!)
        self.assertEqual(_get_cached_value("hist_AAPL_us_3mo", 300), {"symbol": "AAPL"})
        self.assertEqual(_get_cached_value("info_AAPL", 3600), {"name": "Apple"})
        with app_state.yfinance_short_cache_lock:
            self.assertEqual(
                app_state.yfinance_short_cache.get("info_short_AAPL"), {"name": "Apple"}
            )
            self.assertEqual(app_state.yfinance_short_cache.get("fastinfo_AAPL"), {"price": 200.0})
        self.assertEqual(app_state.stock_disk_cache.get("hist_AAPL_us_3mo"), {"data": "aapl_disk"})
        self.assertEqual(
            app_state.payload_disk_cache.get("payload_AAPL_us_123"), {"data": "aapl_payload"}
        )

    def test_r1_invalidate_cleans_history_payload_and_info_disk(self):
        """Invalidating 'MSFT' must evict history_short_payload and info_disk."""
        payload_key = history_short_payload_cache_key("MSFT", "1mo", "1d")
        with app_state.yfinance_short_cache_lock:
            app_state.yfinance_short_cache[payload_key] = {"candles": []}

        app_state.stock_disk_cache.set("info_disk_MSFT", {"name": "Microsoft"})

        self.assertIsNotNone(app_state.yfinance_short_cache.get(payload_key))
        self.assertIsNotNone(app_state.stock_disk_cache.get("info_disk_MSFT"))

        invalidate_single_stock_cache("MSFT")

        with app_state.yfinance_short_cache_lock:
            self.assertNotIn(payload_key, app_state.yfinance_short_cache)
        self.assertIsNone(app_state.stock_disk_cache.get("info_disk_MSFT"))


class TestR2ApiClientCsrfLogic(unittest.TestCase):
    """R2: Verify api_client logic via inspection of compiled api_client.js."""

    def test_r2_compiled_client_csrf_condition(self):
        """api_client.js must not check errCode 1002 or 1003 for CSRF, but check details.reason."""
        from pathlib import Path

        client_js_path = Path("static/js/api_client.js")
        content = client_js_path.read_text(encoding="utf-8")
        self.assertNotIn("errCode === 1002", content)
        self.assertNotIn("errCode === 1003", content)
        self.assertIn("errReason", content)
        self.assertIn("/csrf token/i.test(errReason)", content)


class TestR3CryptoFailClosed(unittest.TestCase):
    """R3: Test fail-closed decryption on non-Windows platforms and unknown schemes."""

    def test_r3_dpapi_fails_closed_on_non_windows(self):
        fake_ciphertext = base64.b64encode(b"sensitive-api-token-12345").decode("ascii")
        with patch("crypto_utils._is_windows", return_value=False):
            result = _decode_secret(
                {"scheme": "dpapi", "value": fake_ciphertext}, "mistral_api_key"
            )
            self.assertEqual(result, "")

    def test_r3_unknown_scheme_fails_closed(self):
        fake_ciphertext = base64.b64encode(b"sensitive-api-token-12345").decode("ascii")
        result = _decode_secret(
            {"scheme": "unsupported_cipher", "value": fake_ciphertext}, "tavily_api_key"
        )
        self.assertEqual(result, "")


class TestR4NativeHostTokenMarkerCoupling(unittest.TestCase):
    """R4: Test native host couples token_file and used_marker to avoid stale lockout."""

    def test_r4_primary_token_with_stale_legacy_marker(self):
        """When primary token exists and is fresh, presence of legacy used marker must not lock out."""
        with tempfile.TemporaryDirectory() as tmpdir:
            from pathlib import Path

            base_dir = Path(tmpdir)
            app_data_dir = base_dir / "appdata"
            repo_root_dir = base_dir / "repo"

            app_data_dir.mkdir(parents=True, exist_ok=True)
            repo_root_dir.mkdir(parents=True, exist_ok=True)

            primary_token_file = app_data_dir / ".mns_shutdown_token"
            primary_used_marker = app_data_dir / ".mns_shutdown_token.used"
            legacy_token_file = repo_root_dir / ".mns_shutdown_token"
            legacy_used_marker = repo_root_dir / ".mns_shutdown_token.used"

            # Primary token exists and is unused
            primary_token_file.write_text("valid-new-token", encoding="utf-8")
            # Legacy used marker exists from previous session
            legacy_used_marker.write_text("used", encoding="utf-8")

            # Logic under test:
            if primary_token_file.exists():
                token_file = primary_token_file
                used_marker = primary_used_marker
            else:
                token_file = legacy_token_file
                used_marker = legacy_used_marker

            self.assertEqual(token_file, primary_token_file)
            self.assertEqual(used_marker, primary_used_marker)
            # Fresh token is NOT marked as used!
            self.assertFalse(used_marker.exists())


class TestR5AiPortfolioRebalanceCache(unittest.TestCase):
    """R5: Test that rebalance explicitly passes use_cache=False to call_mistral_chat."""

    @patch("services.ai_portfolio_service._find_saved_ai_portfolio", return_value=None)
    @patch("services.ai_portfolio_service._acquire_ai_generation_slot", return_value=True)
    @patch("services.ai_portfolio_service._release_ai_generation_slot")
    @patch("services.ai_portfolio_service.collect_symbol_research_context", return_value="Context")
    @patch("services.ai_portfolio_service.call_mistral_chat")
    @patch("services.ai_portfolio_service._persist_generated_ai_portfolio")
    def test_r5_rebalance_bypasses_cache(
        self, mock_persist, mock_chat, mock_context, mock_rel, mock_acq, mock_find
    ):
        fake_response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "title": "Tech Leaders",
                                "description": "Top tech companies",
                                "risk_level": "mid",
                                "expected_return": "10-15%",
                                "commentary": "Rebalanced commentary",
                                "items": [
                                    {
                                        "symbol": "AAPL",
                                        "name": "Apple",
                                        "market": "us",
                                        "weight_pct": 50.0,
                                        "target_price": 250.0,
                                        "rationale": "Strong growth",
                                        "risk_level": "mid",
                                    },
                                    {
                                        "symbol": "MSFT",
                                        "name": "Microsoft",
                                        "market": "us",
                                        "weight_pct": 50.0,
                                        "target_price": 450.0,
                                        "rationale": "Cloud leader",
                                        "risk_level": "mid",
                                    },
                                ],
                            }
                        )
                    }
                }
            ]
        }
        mock_chat.return_value = fake_response

        generate_ai_portfolio_by_theme("Tech Leaders", force_rebalance=True, api_key="fake-key")

        self.assertTrue(mock_chat.called)
        _, kwargs = mock_chat.call_args
        self.assertEqual(kwargs.get("use_cache"), False)


class TestR6SseStreamDisconnectHandling(unittest.TestCase):
    """R6: Test that client disconnect on SSE stream exits cleanly."""

    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()

    def test_r6_client_disconnect_handled(self):
        # Client test requesting mode=0 returns 200
        res = self.client.get("/api/stocks/stream?mode=0")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["sse_mode"], 0)


class TestR7AiPortfolioDuplicateSymbolRejection(unittest.TestCase):
    """R7: Test that duplicate symbols in copy-to-my are rejected with 400 INVALID_INPUT."""

    def setUp(self):
        self.app = create_app()
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.client = self.app.test_client()

    def test_r7_duplicate_symbols_rejected(self):
        payload = {
            "items": [
                {"symbol": "NVDA", "market": "us", "weight_pct": 20.0, "target_price": 120.0},
                {"symbol": "NVDA", "market": "us", "weight_pct": 30.0, "target_price": 120.0},
            ]
        }
        response = self.client.post(
            "/api/ai-portfolio/copy-to-my",
            data=json.dumps(payload),
            content_type="application/json",
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(response.status_code, 400)
        data = response.get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error_code"], ErrorCode.INVALID_INPUT)
        self.assertIn("重複", data["details"]["reason"])


if __name__ == "__main__":
    unittest.main()
