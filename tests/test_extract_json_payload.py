import json
import unittest

from app import extract_chat_content, extract_json_payload


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
        # repairされるため、JSONとしてパース可能
        import json
        parsed = json.loads(result)
        self.assertEqual(parsed["us"], "abc")

    def test_raises_for_completely_invalid_json(self):
        with self.assertRaises(ValueError):
            extract_json_payload('completely invalid text without any json')

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


if __name__ == "__main__":
    unittest.main()
