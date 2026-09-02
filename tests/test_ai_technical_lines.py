"""
Tests for AI Technical Lines generation and model eligibility restrictions.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import app
from credential_manager import is_medium_or_large_model


class AITechnicalLinesEligibilityTestCase(unittest.TestCase):
    """is_medium_or_large_model 判定テスト"""

    def test_medium_model_is_eligible(self):
        self.assertTrue(is_medium_or_large_model("mistral-medium-2604"))
        self.assertTrue(is_medium_or_large_model("mistral-medium-latest"))
        self.assertTrue(is_medium_or_large_model("mistral-medium-3.5"))
        self.assertTrue(is_medium_or_large_model("2"))  # Index 2 -> mistral-medium-2604

    def test_large_model_is_eligible(self):
        self.assertTrue(is_medium_or_large_model("mistral-large-2512"))
        self.assertTrue(is_medium_or_large_model("mistral-large-latest"))
        self.assertTrue(is_medium_or_large_model("3"))  # Index 3 -> mistral-large-2512

    def test_small_and_other_models_are_ineligible(self):
        self.assertFalse(is_medium_or_large_model("mistral-small-2603"))
        self.assertFalse(is_medium_or_large_model("mistral-small-latest"))
        self.assertFalse(is_medium_or_large_model("ministral-3-8b-2512"))
        self.assertFalse(is_medium_or_large_model("ministral-3-14b-2512"))
        self.assertFalse(is_medium_or_large_model("1"))  # Index 1 -> mistral-small-2603


class AITechnicalLinesEndpointTestCase(unittest.TestCase):
    """/api/ai-technical-lines エンドポイントのアクセス制御と動的生成のテスト"""

    def setUp(self):
        self._orig_csrf = app.config.get("WTF_CSRF_ENABLED")
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()

    def tearDown(self):
        if self._orig_csrf is not None:
            app.config["WTF_CSRF_ENABLED"] = self._orig_csrf

    @patch("routes.api_analysis.get_model_name", return_value="mistral-small-2603")
    def test_endpoint_blocks_small_model(self, mock_get_model):
        res = self.client.post(
            "/api/ai-technical-lines",
            json={"symbol": "AAPL", "market": "us", "period": "3mo"},
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(res.status_code, 403)
        data = res.get_json()
        self.assertFalse(data.get("ok"))
        self.assertTrue(data.get("model_restricted"))
        self.assertIn("Mistral Medium または Large", data.get("error", ""))

    @patch("routes.api_analysis.get_model_name", return_value="mistral-medium-2604")
    @patch("routes.api_analysis.extract_api_key", return_value="test-api-key-12345678901234567890")
    @patch("routes.api_analysis.generate_ai_technical_lines")
    def test_endpoint_allows_medium_model(self, mock_gen, mock_key, mock_model):
        mock_gen.return_value = {
            "summary": "上昇トレンド構築中",
            "trend_bias": "Bullish",
            "lines": [
                {
                    "id": "line_1",
                    "type": "support",
                    "label": "サポート $150",
                    "color": "#00ff88",
                    "style": "dashed",
                    "start_date": "2026-01-01",
                    "start_price": 150.0,
                    "end_date": "2026-08-01",
                    "end_price": 150.0,
                    "description": "下値支持線",
                }
            ],
        }

        res = self.client.post(
            "/api/ai-technical-lines",
            json={
                "symbol": "AAPL",
                "market": "us",
                "period": "3mo",
                "history_data": [{"date": "2026-01-01", "close": 150.0}],
            },
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("summary"), "上昇トレンド構築中")
        self.assertEqual(len(data.get("lines", [])), 1)

    @patch("routes.api_analysis.get_model_name", return_value="mistral-medium-2604")
    @patch("routes.api_analysis.extract_api_key", return_value="test-api-key-12345678901234567890")
    def test_endpoint_rejects_missing_symbol(self, mock_key, mock_model):
        """Reject request when symbol is missing and return standard fields list."""
        res = self.client.post(
            "/api/ai-technical-lines",
            json={"market": "us", "period": "3mo"},
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertFalse(data.get("ok"))
        self.assertEqual(data.get("details", {}).get("fields"), ["symbol"])

    @patch("routes.api_analysis.get_model_name", return_value="mistral-medium-2604")
    @patch("routes.api_analysis.extract_api_key", return_value="test-api-key-12345678901234567890")
    def test_endpoint_rejects_invalid_period(self, mock_key, mock_model):
        """MNS-002: an out-of-range period must be rejected before hitting the LLM."""
        res = self.client.post(
            "/api/ai-technical-lines",
            json={"symbol": "AAPL", "market": "us", "period": "not-a-period"},
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(res.status_code, 400)

    @patch("routes.api_analysis.get_model_name", return_value="mistral-medium-2604")
    @patch("routes.api_analysis.extract_api_key", return_value="test-api-key-12345678901234567890")
    def test_endpoint_rejects_non_list_history_data(self, mock_key, mock_model):
        """MNS-002: non-list history_data must be rejected with 400."""
        res = self.client.post(
            "/api/ai-technical-lines",
            json={
                "symbol": "AAPL",
                "market": "us",
                "period": "3mo",
                "history_data": {"o": 1},
            },
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(res.status_code, 400)

    @patch("routes.api_analysis.get_model_name", return_value="mistral-medium-2604")
    @patch("routes.api_analysis.extract_api_key", return_value="test-api-key-12345678901234567890")
    def test_endpoint_rejects_oversized_history_data(self, mock_key, mock_model):
        """MNS-002: history_data over 5000 points must be rejected with 400."""
        big = [{"date": "2026-01-01", "price": 1.0}] * 5001
        res = self.client.post(
            "/api/ai-technical-lines",
            json={
                "symbol": "AAPL",
                "market": "us",
                "period": "3mo",
                "history_data": big,
            },
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(res.status_code, 400)

    @patch("routes.api_analysis.get_model_name", return_value="mistral-medium-2604")
    @patch("routes.api_analysis.extract_api_key", return_value="test-api-key-12345678901234567890")
    def test_endpoint_rejects_invalid_symbol(self, mock_key, mock_model):
        """R3: malformed symbols must be rejected before reaching the LLM."""
        res = self.client.post(
            "/api/ai-technical-lines",
            json={
                "symbol": "../not-a-ticker/with spaces",
                "market": "us",
                "period": "3mo",
            },
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(res.status_code, 400)

    @patch("routes.api_analysis.get_model_name", return_value="mistral-medium-2604")
    @patch("routes.api_analysis.extract_api_key", return_value="test-api-key-12345678901234567890")
    def test_endpoint_rejects_invalid_market(self, mock_key, mock_model):
        """R3: unknown markets must be rejected before reaching the LLM."""
        res = self.client.post(
            "/api/ai-technical-lines",
            json={
                "symbol": "AAPL",
                "market": "not-a-market",
                "period": "3mo",
            },
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(res.status_code, 400)


class AITechnicalLinesGenerationTestCase(unittest.TestCase):
    """generate_ai_technical_lines の自動修復・フォールバック機能のテスト"""

    @patch("services.ai_service.call_mistral_chat")
    def test_salvages_truncated_llm_response(self, mock_chat):
        # ユーザーのエラーログと同等の、途切れたLLM出力レスポンス
        truncated_response = {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{ "summary": "AMPLの1年間の株価データを分析した結果、明確な下降トレンドが確認され...", '
                            '"trend_bias": "Bearish (下降トレンド優勢)", "lines...'
                        )
                    }
                }
            ]
        }
        mock_chat.return_value = truncated_response

        from services.ai_service import generate_ai_technical_lines

        history = [{"date": "2026-08-01", "o": 10, "h": 12, "l": 9, "c": 11}]
        res = generate_ai_technical_lines("test_api_key", "AMPL", "us", "1y", history)

        self.assertNotIn("error", res)
        self.assertIn("AMPLの1年間の株価データを分析した結果", res.get("summary", ""))
        self.assertEqual(res.get("trend_bias"), "Bearish (下降トレンド優勢)")
        self.assertIsInstance(res.get("lines"), list)

    @patch("services.ai_service.call_mistral_chat")
    def test_generates_technical_lines_for_jp_market(self, mock_chat):
        mock_response = {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{\n'
                            '  "summary": "7203.Tのテクニカル分析サマリー",\n'
                            '  "trend_bias": "Bullish",\n'
                            '  "lines": [\n'
                            '    {\n'
                            '      "id": "line_jp_1",\n'
                            '      "type": "support",\n'
                            '      "label": "支持線 ¥2,500",\n'
                            '      "color": "#00ff88",\n'
                            '      "style": "dashed",\n'
                            '      "start_date": "2026-01-01",\n'
                            '      "start_price": 2500.0,\n'
                            '      "end_date": "2026-08-01",\n'
                            '      "end_price": 2500.0,\n'
                            '      "description": "2500円支持帯"\n'
                            '    }\n'
                            '  ]\n'
                            '}'
                        )
                    }
                }
            ]
        }
        mock_chat.return_value = mock_response

        from services.ai_service import generate_ai_technical_lines

        history = [{"date": "2026-08-01", "o": 2500, "h": 2600, "l": 2480, "c": 2550}]
        res = generate_ai_technical_lines("test_api_key", "7203.T", "jp", "3mo", history)

        self.assertNotIn("error", res)
        self.assertEqual(res.get("summary"), "7203.Tのテクニカル分析サマリー")
        self.assertEqual(res.get("trend_bias"), "Bullish")
        self.assertEqual(len(res.get("lines", [])), 1)
        self.assertEqual(res["lines"][0]["start_price"], 2500.0)


class AITechnicalLinesSanitizationTestCase(unittest.TestCase):
    """_sanitize_prompt_text のサニタイズ動作テスト (MNS-002)"""

    def test_sanitize_prompt_text(self):
        from services.ai_service import _sanitize_prompt_text

        self.assertEqual(_sanitize_prompt_text("  AAPL  "), "AAPL")
        self.assertEqual(_sanitize_prompt_text("a<b>c"), "a b c")
        self.assertEqual(_sanitize_prompt_text(None), "")
        self.assertEqual(_sanitize_prompt_text("line1\nline2"), "line1\nline2")
        self.assertEqual(_sanitize_prompt_text("a\x00b\x1fc"), "abc")
        long_value = "x" * 300
        self.assertEqual(len(_sanitize_prompt_text(long_value)), 120)


if __name__ == "__main__":
    unittest.main()
