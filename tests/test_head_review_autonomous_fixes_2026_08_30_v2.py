"""
tests/test_head_review_autonomous_fixes_2026_08_30_v2.py
Regression tests for autonomous HEAD review findings (R1 - R9).
"""

import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

import services.ai_tools as ait
from app import create_app
from error_codes import ErrorCode
from services.ai_service import (
    _resolve_reasoning_effort,
    analyze_chart_image_with_mistral,
    call_mistral_chat,
    call_mistral_chat_with_tools,
)
from services.realtime.engine import RealtimeMarketEngine
from utils.disk_cache import StockDiskCache


class TestHeadReviewAutonomousFixes20260830V2(unittest.TestCase):
    """Test cases for R1 through R9."""

    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config["TESTING"] = True
        cls.app.config["WTF_CSRF_ENABLED"] = False
        cls.app.config["WTF_CSRF_CHECK_DEFAULT"] = False

    def test_r1_analyze_chart_image_auth_gate_and_error_handling(self):
        """R1: Test /api/analyze-chart-image enforces require_trusted_or_admin gate."""
        client = self.app.test_client()

        with patch.dict(os.environ, {"MNS_ADMIN_TOKEN": "x" * 32}):
            # 1. Unauthenticated request must return 403 Forbidden
            resp = client.post(
                "/api/analyze-chart-image",
                json={"image_data": "dGVzdA==", "symbol": "AAPL"},
            )
            self.assertEqual(resp.status_code, 403)
            data = resp.get_json()
            self.assertEqual(data.get("error_code"), ErrorCode.FORBIDDEN.value)

            # 2. Authenticated request passes the gate
            mock_res = {
                "symbol": "AAPL",
                "market": "us",
                "model": "pixtral-large-latest",
                "analysis": "Uptrend",
                "analyzed_at": "2026-08-30T00:00:00Z",
            }
            with patch("routes.api_analysis.extract_api_key", return_value="test_key"), \
                 patch("routes.api_analysis.analyze_chart_image_with_mistral", return_value=mock_res):
                resp_auth = client.post(
                    "/api/analyze-chart-image",
                    json={"image_data": "dGVzdA==", "symbol": "AAPL"},
                    headers={"X-MNS-Admin-Token": "x" * 32},
                )
                self.assertEqual(resp_auth.status_code, 200)
                self.assertTrue(resp_auth.get_json().get("ok"))

            # 3. Provider diagnostics must not be reflected to the caller.
            internal_error = "provider trace at https://internal.example/api request=private-123"
            with patch("routes.api_analysis.extract_api_key", return_value="test_key"), \
                 patch(
                     "routes.api_analysis.analyze_chart_image_with_mistral",
                     return_value={"error": internal_error},
                 ):
                resp_error = client.post(
                    "/api/analyze-chart-image",
                    json={"image_data": "dGVzdA==", "symbol": "AAPL"},
                    headers={"X-MNS-Admin-Token": "x" * 32},
                )
            self.assertEqual(resp_error.status_code, 500)
            error_data = resp_error.get_json()
            self.assertEqual(error_data.get("error_code"), ErrorCode.INTERNAL_SERVER_ERROR.value)
            self.assertEqual(error_data.get("details", {}).get("reason"), "画像分析に失敗しました")
            self.assertNotIn("private-123", json.dumps(error_data))

    def test_r2_ai_usage_endpoint_security_and_rate_limiting(self):
        """R2: Test /api/system/ai-usage enforces admin token in remote mode and supports OPTIONS."""
        client = self.app.test_client()

        # OPTIONS preflight
        resp_options = client.options("/api/system/ai-usage")
        self.assertEqual(resp_options.status_code, 200)

        with patch.dict(os.environ, {"MNS_ADMIN_TOKEN": "y" * 32}):
            # Missing admin token returns 403
            resp = client.get("/api/system/ai-usage")
            self.assertEqual(resp.status_code, 403)

            # Providing admin token returns 200
            resp_authed = client.get(
                "/api/system/ai-usage",
                headers={"X-MNS-Admin-Token": "y" * 32},
            )
            self.assertEqual(resp_authed.status_code, 200)
            self.assertTrue(resp_authed.get_json().get("ok"))

    def test_r3_rate_limit_bypasses_options_preflight(self):
        """R3: Test that CORS preflight OPTIONS requests do not consume rate-limit quota."""
        client = self.app.test_client()
        with patch.dict(os.environ, {"MNS_LOCAL_RATE_LIMIT_MULTIPLE": "1"}):
            # Send 10 consecutive OPTIONS requests (exceeding max_requests=5 on /api/shutdown)
            for i in range(10):
                resp = client.options("/api/shutdown")
                self.assertEqual(resp.status_code, 200, f"OPTIONS #{i+1} failed with {resp.status_code}")

    def test_r4_reasoning_effort_omitted_on_400_retry(self):
        """R4: Test reasoning_effort=False resolves to None and omits parameter on 400 retry."""
        # 1. Direct resolution test
        self.assertIsNone(_resolve_reasoning_effort("mistral-small-2603", reasoning_effort=False))

        # 2. call_mistral_chat retry test
        mock_client = MagicMock()
        call_count = [0]
        recorded_kwargs = []

        def mock_complete(**kwargs):
            call_count[0] += 1
            recorded_kwargs.append(kwargs)
            if call_count[0] == 1:
                import httpx
                mock_resp = MagicMock()
                mock_resp.status_code = 400
                mock_resp.json.return_value = {
                    "object": "error",
                    "message": "Invalid parameter: reasoning_effort is not supported on this endpoint",
                }
                mock_resp.text = json.dumps(mock_resp.json.return_value)
                raise httpx.HTTPStatusError("400 Bad Request", request=MagicMock(), response=mock_resp)
            mock_choice = MagicMock()
            mock_choice.message.content = "Success without reasoning"
            res = MagicMock()
            res.choices = [mock_choice]
            res.model_dump.return_value = {"choices": [{"message": {"content": "Success without reasoning"}}]}
            return res

        mock_client.chat.complete.side_effect = mock_complete

        with patch("services.ai_service._get_mistral_client", return_value=mock_client), \
             patch("services.ai_service._get_mistral_model_name", return_value="mistral-small-2603"):
            res = call_mistral_chat(
                api_key="test-key",
                messages=[{"role": "user", "content": "hello"}],
                reasoning_effort="high",
                use_cache=False,
            )
            self.assertEqual(call_count[0], 2)
            self.assertIn("reasoning_effort", recorded_kwargs[0])
            self.assertNotIn("reasoning_effort", recorded_kwargs[1])
            self.assertEqual(res["choices"][0]["message"]["content"], "Success without reasoning")

    def test_r5_call_mistral_chat_with_tools_synthesis_user_turn(self):
        """R5: Test that final synthesis turn in call_mistral_chat_with_tools includes user prompt turn."""
        recorded_messages = []

        def mock_call(api_key, messages, **kwargs):
            recorded_messages.append(list(messages))
            if len(recorded_messages) == 1:
                # First turn: model responds with final answer without tool calls
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "Apple current price is 150.0 USD.",
                            }
                        }
                    ]
                }
            # Second turn (synthesis): model formats according to schema
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"price": 150.0}),
                        }
                    }
                ]
            }

        with patch("services.ai_service.call_mistral_chat", side_effect=mock_call):
            _ = call_mistral_chat_with_tools(
                api_key="test-key",
                messages=[{"role": "user", "content": "What is AAPL price?"}],
                response_format=dict,
            )
            self.assertEqual(len(recorded_messages), 2)
            # Verify the final synthesis message sequence ends with a user message
            synthesis_msgs = recorded_messages[1]
            self.assertEqual(synthesis_msgs[-2]["role"], "assistant")
            self.assertEqual(synthesis_msgs[-1]["role"], "user")
            self.assertIn("整形", synthesis_msgs[-1]["content"])

    def test_r6_ai_tools_news_summary_and_rsi_momentum(self):
        """R6: Test ai_tools matches news summary and calculates RSI 100 on 100% gains."""
        # 1. Market news summary check
        mock_news = [
            {
                "title": "Toyota Q3 Overview",
                "summary": "Record breaking EV shipment numbers reported.",
                "source": "Nikkei",
            }
        ]
        with patch("trend_sources.collect_market_news_items_fast", return_value=mock_news):
            res_body = ait._tool_get_market_news({"query": "shipment"})
            self.assertEqual(res_body["count"], 1)
            self.assertIn("EV shipment", res_body["news"][0]["snippet"])

        # 2. Technical levels RSI calculation with 0 losses
        prices = [100.0 + i for i in range(25)]
        df = pd.DataFrame({"Close": prices})
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = df
        with patch("utils.market_utils.safe_get_ticker", return_value=mock_ticker):
            res_tech = ait._tool_calculate_technical_levels({"symbol": "NVDA"})
            self.assertEqual(res_tech["rsi_14"], 100.0)

    def test_r7_analyze_chart_image_http_url_handling(self):
        """R7: Test analyze_chart_image_with_mistral preserves http/https URLs."""
        with patch("services.ai_service.call_mistral_chat") as mock_call:
            mock_call.return_value = {"choices": [{"message": {"content": "chart analyzed"}}]}
            res = analyze_chart_image_with_mistral(
                api_key="test-key",
                image_data="https://chart.example.com/aapl.png",
                symbol="AAPL",
            )
            self.assertNotIn("error", res)
            sent_msgs = mock_call.call_args[1]["messages"]
            img_url = sent_msgs[1]["content"][1]["image_url"]
            self.assertEqual(img_url, "https://chart.example.com/aapl.png")

    def test_r8_disk_cache_corruption_unlinking_and_prefix_safety(self):
        """R8: Test corrupt JSON is unlinked on get() and delete_prefix('') does not wipe cache."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = StockDiskCache(cache_dir=pathlib.Path(tmpdir))
            cache.set("AAPL", {"price": 150})
            cache.set("MSFT", {"price": 300})

            # Empty prefix must be ignored
            removed = cache.delete_prefix("")
            self.assertEqual(removed, 0)
            self.assertEqual(len(list(pathlib.Path(tmpdir).glob("*.json"))), 2)

            # Corrupted file must be unlinked on get()
            corrupt_file = cache._entry_path("corrupt")
            corrupt_file.write_text("{not valid json", encoding="utf-8")
            self.assertTrue(corrupt_file.exists())

            val = cache.get("corrupt")
            self.assertIsNone(val)
            self.assertFalse(corrupt_file.exists())

    def test_r9_realtime_engine_lifecycle_and_scraper_sessions(self):
        """R9: Test engine.stop clears pts_thread, watchdog ignores stopped engine, scraper resets closed sessions."""
        # 1. stop() clears pts_thread so worker_threads returns empty
        engine = RealtimeMarketEngine()
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = False
        engine.pts_thread = mock_thread
        engine.stop()
        self.assertIsNone(engine.pts_thread)
        self.assertEqual(engine.worker_threads(), [])

        # 2. Scraper session invalidation on close()
        from services.realtime.scrapers import SBISecuritiesScraper
        scraper = SBISecuritiesScraper()
        sess1 = scraper.session
        self.assertIsNotNone(sess1)
        scraper.close()
        # After close, calling scraper.session allocates a fresh valid session instead of reusing the closed one
        sess2 = scraper.session
        self.assertIsNotNone(sess2)
        self.assertIsNot(sess1, sess2)
        scraper.close()


if __name__ == "__main__":
    unittest.main()
