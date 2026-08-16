"""
R8 (NH-1) / R9 (NH-2) 根本原因修正の回帰テスト

- R8: ``_sanitize_log_message`` が ``Authorization: Bearer <token>`` のトークン全体と
      ``token=<value>`` の値全体（引用符・区切り文字を含む）を完全にマスクすること。
- R9: バックエンド停止中に ``get_extension_api_token`` がトークンを発行・生成しないこと。
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from native_host import native_host


class SanitizeLogMessageRegressionTestCase(unittest.TestCase):
    """R8 / NH-1: _sanitize_log_message のマスキング完全化"""

    def test_bearer_token_fully_masked(self):
        """Authorization: Bearer <token> のトークン本体全体がマスクされる"""
        sanitized = native_host._sanitize_log_message("Authorization: Bearer abc.def.ghi")
        self.assertNotIn("abc.def.ghi", sanitized)
        self.assertNotIn("Bearer", sanitized)
        self.assertIn("[REDACTED]", sanitized)

    def test_bearer_token_lowercase_header_masked(self):
        """小文字ヘッダ・スキームでもトークン本体がマスクされる"""
        sanitized = native_host._sanitize_log_message("authorization: bearer abc.def.ghi")
        self.assertNotIn("abc.def.ghi", sanitized)
        self.assertIn("[REDACTED]", sanitized)

    def test_bearer_token_via_equals_masked(self):
        """Authorization=Bearer <token> 形式でもトークン本体がマスクされる"""
        sanitized = native_host._sanitize_log_message("Authorization=Bearer abc.def.ghi")
        self.assertNotIn("abc.def.ghi", sanitized)
        self.assertIn("[REDACTED]", sanitized)

    def test_bearer_token_without_scheme_masked(self):
        """スキームなしの Authorization 値も全体がマスクされる"""
        sanitized = native_host._sanitize_log_message("authorization: abc.def.ghi")
        self.assertNotIn("abc.def.ghi", sanitized)
        self.assertIn("[REDACTED]", sanitized)

    def test_token_value_with_quote_fully_masked(self):
        """token=abc\"def の値部分（引用符込み）が全体マスクされる"""
        sanitized = native_host._sanitize_log_message('token=abc"def')
        self.assertNotIn("abc", sanitized)
        self.assertNotIn("def", sanitized)
        self.assertIn("[REDACTED]", sanitized)

    def test_token_value_with_double_quotes_masked(self):
        """token=\"value\" 形式でも値が全体マスクされる"""
        sanitized = native_host._sanitize_log_message('token="supersecretvalue"')
        self.assertNotIn("supersecretvalue", sanitized)
        self.assertIn("[REDACTED]", sanitized)

    def test_token_value_with_colon_masked(self):
        """token:value 形式でも値が全体マスクされる"""
        sanitized = native_host._sanitize_log_message("token:supersecret")
        self.assertNotIn("supersecret", sanitized)
        self.assertIn("[REDACTED]", sanitized)

    def test_existing_mask_patterns_still_work(self):
        """既存のマスキングパターンを壊さない"""
        cases = [
            ("api_key=sk-1234567890", "sk-1234567890"),
            ("api_key='sk-1234567890'", "sk-1234567890"),
            ("password=hunter2", "hunter2"),
            ("token=secret", "secret"),
        ]
        for msg, secret in cases:
            with self.subTest(msg=msg):
                sanitized = native_host._sanitize_log_message(msg)
                self.assertNotIn(secret, sanitized)
                self.assertIn("[REDACTED]", sanitized)

    def test_combined_message_masks_all_secrets(self):
        """複数の秘密が混在するメッセージで全てマスクされる"""
        msg = "Authorization: Bearer abc.def.ghi token=abc\"def api_key=sk-1234567890"
        sanitized = native_host._sanitize_log_message(msg)
        self.assertNotIn("abc.def.ghi", sanitized)
        self.assertNotIn("sk-1234567890", sanitized)
        self.assertEqual(sanitized.count("[REDACTED]"), 3)

    def test_plain_message_unchanged(self):
        """機密を含まないメッセージは変更されない"""
        sanitized = native_host._sanitize_log_message("Processing action: ping")
        self.assertIn("Processing action: ping", sanitized)
        self.assertNotIn("[REDACTED]", sanitized)

    def test_empty_message(self):
        """空・None 入力は空文字を返す"""
        self.assertEqual(native_host._sanitize_log_message(""), "")
        self.assertEqual(native_host._sanitize_log_message(None), "")


class ExtensionApiTokenGateTestCase(unittest.TestCase):
    """R9 / NH-2: get_extension_api_token はバックエンド稼働状態を確認する"""

    VALID_ID = "abcdefghijklmnopqrstuvwxyz123456"

    def _run_request(self, healthy, token_value="tok-extension-123"):
        """main() を通して get_extension_api_token を処理し、送信メッセージを返す"""
        sent = []
        req = {"action": "get_extension_api_token", "extensionId": self.VALID_ID}
        with (
            patch.object(native_host, "read_message", side_effect=[req, None]),
            patch.object(native_host, "send_message", side_effect=lambda m: sent.append(m)),
            patch.object(native_host, "_check_rate_limit", return_value=True),
            patch.object(native_host, "_token_action_allowed", return_value=True),
            patch.object(native_host, "is_backend_healthy_once", return_value=healthy),
            patch.object(
                native_host,
                "_load_allowed_manifest_origins",
                return_value={self.VALID_ID},
            ),
            patch(
                "sys.argv",
                ["native_host.py", f"chrome-extension://{self.VALID_ID}/"],
            ),
            patch.object(
                native_host,
                "_get_ancestor_process_names",
                return_value=["cmd.exe", "chrome.exe"],
            ),
            patch(
                "credential_manager.get_or_create_extension_api_token",
                return_value=token_value,
            ),
        ):
            native_host.main()
        return sent

    def test_token_refused_when_backend_down(self):
        """バックエンド停止中はトークンを返さない"""
        sent = self._run_request(healthy=False)
        self.assertEqual(len(sent), 1)
        self.assertFalse(sent[0]["ok"])
        self.assertIn("not running", sent[0]["error"])
        self.assertNotIn("token", sent[0])

    def test_token_not_created_when_backend_down(self):
        """バックエンド停止中は get_or_create_extension_api_token を呼ばない"""
        sent = []
        req = {"action": "get_extension_api_token", "extensionId": self.VALID_ID}
        with (
            patch.object(native_host, "read_message", side_effect=[req, None]),
            patch.object(native_host, "send_message", side_effect=lambda m: sent.append(m)),
            patch.object(native_host, "_check_rate_limit", return_value=True),
            patch.object(native_host, "_token_action_allowed", return_value=True),
            patch.object(native_host, "is_backend_healthy_once", return_value=False),
            patch.object(
                native_host,
                "_load_allowed_manifest_origins",
                return_value={self.VALID_ID},
            ),
            patch(
                "sys.argv",
                ["native_host.py", f"chrome-extension://{self.VALID_ID}/"],
            ),
            patch.object(
                native_host,
                "_get_ancestor_process_names",
                return_value=["cmd.exe", "chrome.exe"],
            ),
            patch(
                "credential_manager.get_or_create_extension_api_token",
            ) as get_token,
        ):
            native_host.main()
        self.assertEqual(len(sent), 1)
        self.assertFalse(sent[0]["ok"])
        get_token.assert_not_called()

    def test_token_refused_when_health_check_unavailable(self):
        """バックエンドヘルスチェックが利用不可の場合は拒否する"""
        sent = []
        req = {"action": "get_extension_api_token", "extensionId": self.VALID_ID}
        with (
            patch.object(native_host, "read_message", side_effect=[req, None]),
            patch.object(native_host, "send_message", side_effect=lambda m: sent.append(m)),
            patch.object(native_host, "_check_rate_limit", return_value=True),
            patch.object(native_host, "_token_action_allowed", return_value=True),
            patch.object(native_host, "is_backend_healthy_once", None),
            patch.object(
                native_host,
                "_load_allowed_manifest_origins",
                return_value={self.VALID_ID},
            ),
            patch(
                "sys.argv",
                ["native_host.py", f"chrome-extension://{self.VALID_ID}/"],
            ),
            patch.object(
                native_host,
                "_get_ancestor_process_names",
                return_value=["cmd.exe", "chrome.exe"],
            ),
            patch(
                "credential_manager.get_or_create_extension_api_token",
            ) as get_token,
        ):
            native_host.main()
        self.assertEqual(len(sent), 1)
        self.assertFalse(sent[0]["ok"])
        self.assertIn("unavailable", sent[0]["error"])
        get_token.assert_not_called()

    def test_token_returned_when_backend_healthy(self):
        """バックエンド稼働中は従来どおりトークンを返す"""
        sent = self._run_request(healthy=True, token_value="tok-extension-123")
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0]["ok"])
        self.assertEqual(sent[0]["token"], "tok-extension-123")


if __name__ == "__main__":
    unittest.main()
