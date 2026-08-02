import json
import unittest

from utils.validators import extract_chat_content, extract_json_payload


class ExtractJsonPayloadTests(unittest.TestCase):
    def test_extracts_plain_json(self):
        payload = '{"us":"a","jp":"b","trends":"c"}'
        self.assertEqual(extract_json_payload(payload), payload)

    def test_extracts_fenced_json(self):
        payload = """```json
{"us":"a","jp":"b","trends":"c"}
```"""
        self.assertEqual(extract_json_payload(payload), '{"us":"a","jp":"b","trends":"c"}')

    def test_extracts_json_embedded_in_text(self):
        payload = 'header text {"us":"a","jp":"b","trends":"c"} footer text'
        self.assertEqual(extract_json_payload(payload), '{"us":"a","jp":"b","trends":"c"}')

    def test_raises_for_empty(self):
        with self.assertRaises(ValueError):
            extract_json_payload("")

    def test_auto_repairs_truncated_json(self):
        # 末尾括弧が不足しているJSONは自動修復される
        payload = '{"us":"abc"'
        result = extract_json_payload(payload)
        parsed = json.loads(result)
        self.assertEqual(parsed["us"], "abc")

    def test_auto_repairs_truncated_key_and_array(self):
        # ユーザーのエラーログと同等の、末尾キー・配列の途切れ（lines...）を自動修復する
        payload = '{ "summary": "AMPL株価分析", "trend_bias": "Bearish", "lines...'
        result = extract_json_payload(payload, required_fields=["summary", "trend_bias", "lines"])
        parsed = json.loads(result)
        self.assertEqual(parsed["summary"], "AMPL株価分析")
        self.assertEqual(parsed["trend_bias"], "Bearish")

    def test_auto_repairs_truncated_inside_array_item(self):
        # 配列要素の途中で切れた構造の自動修復
        payload = '{"summary": "分析", "lines": [{"id": "line_1", "start_price": 100}, {'
        result = extract_json_payload(payload)
        parsed = json.loads(result)
        self.assertEqual(parsed["summary"], "分析")
        self.assertEqual(parsed["lines"][0]["id"], "line_1")

    def test_auto_repairs_unescaped_newlines(self):
        # 文字列リテラル内の生の改行コードの自動修復
        payload = '{\n  "summary": "1行目\n2行目",\n  "trend_bias": "Bullish"\n}'
        result = extract_json_payload(payload)
        parsed = json.loads(result)
        self.assertIn("1行目", parsed["summary"])
        self.assertEqual(parsed["trend_bias"], "Bullish")

    def test_raises_for_completely_invalid_json(self):
        with self.assertRaises(ValueError):
            extract_json_payload("completely invalid text without any json")

    def test_extract_chat_content_from_json_object(self):
        response = {
            "choices": [
                {
                    "message": {
                        "content": {
                            "type": "json_object",
                            "value": {"us": "a", "jp": "b", "trends": "c"},
                        }
                    }
                }
            ]
        }
        result = extract_chat_content(response)
        self.assertEqual(json.loads(result), {"us": "a", "jp": "b", "trends": "c"})

    def test_extract_chat_content_from_json_schema_string(self):
        # Mistral API json_schema returns content as a JSON string
        json_string = '{"us":"US News","jp":"Japan News","trends":"Trending Items"}'
        response = {"choices": [{"message": {"content": json_string}}]}
        result = extract_chat_content(response)
        self.assertEqual(
            json.loads(result),
            {"us": "US News", "jp": "Japan News", "trends": "Trending Items"},
        )

    def test_extract_chat_content_from_json_schema_dict(self):
        # Sometimes the API might return a parsed dict directly
        response = {
            "choices": [
                {
                    "message": {
                        "content": {
                            "us": "US News",
                            "jp": "Japan News",
                            "trends": "Trending Items",
                        }
                    }
                }
            ]
        }
        result = extract_chat_content(response)
        self.assertEqual(
            json.loads(result),
            {"us": "US News", "jp": "Japan News", "trends": "Trending Items"},
        )

    def test_extract_chat_content_from_text_type(self):
        # Text type in chunks
        response = {
            "choices": [{"message": {"content": {"type": "text", "text": "Some text response"}}}]
        }
        result = extract_chat_content(response)
        self.assertEqual(result, "Some text response")

    def test_extract_chat_content_from_list_chunks(self):
        # Multiple chunks in a list
        response = {
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "text", "text": "Hello "},
                            {"type": "text", "text": "World"},
                        ]
                    }
                }
            ]
        }
        result = extract_chat_content(response)
        self.assertEqual(result, "Hello World")


if __name__ == "__main__":
    unittest.main()
