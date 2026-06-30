"""Comprehensive unit tests for services/news_formatter.py (NewsFormatter class)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.news_formatter import NewsFormatter


class CoerceNewsSectionTextTestCase(unittest.TestCase):
    """NewsFormatter._coerce_news_section_text tests"""

    def test_none_returns_empty(self):
        self.assertEqual(NewsFormatter._coerce_news_section_text(None), "")

    def test_empty_string_returns_empty(self):
        self.assertEqual(NewsFormatter._coerce_news_section_text(""), "")

    def test_plain_string_passed_through(self):
        result = NewsFormatter._coerce_news_section_text("Markets rally on positive economic data, analysts report.")
        self.assertEqual(result, "Markets rally on positive economic data, analysts report.")

    def test_list_of_strings(self):
        result = NewsFormatter._coerce_news_section_text(["Market rallies on strong earnings.", "Tech sector leads gains."])
        self.assertIn("Market rallies", result)
        self.assertIn("Tech sector leads", result)

    def test_dict_input(self):
        result = NewsFormatter._coerce_news_section_text(
            {"topic": "Market News", "summary": "Markets are up"}
        )
        self.assertIn("Market News", result)
        self.assertIn("Markets are up", result)


class CoerceNewsSectionTextV2TestCase(unittest.TestCase):
    """NewsFormatter._coerce_news_section_text_v2 tests"""

    def test_none_returns_empty(self):
        self.assertEqual(NewsFormatter._coerce_news_section_text_v2(None), "")

    def test_list_input_delegates_to_v1(self):
        result = NewsFormatter._coerce_news_section_text_v2(
            [{"topic": "A", "summary": "B"}]
        )
        self.assertIn("A", result)
        self.assertIn("B", result)

    def test_dict_input_delegates_to_v1(self):
        result = NewsFormatter._coerce_news_section_text_v2(
            {"topic": "X", "summary": "Y"}
        )
        self.assertIn("X - Y", result)

    def test_truncated_sentence_cleaned_on_jp_punc(self):
        # Japanese text ending without punctuation - should truncate at last 。
        result = NewsFormatter._coerce_news_section_text_v2(
            "最初の文です。次の文。最後の不完全な"
        )
        self.assertEqual(result, "最初の文です。次の文。")

    def test_truncated_sentence_cleaned_on_question_mark(self):
        result = NewsFormatter._coerce_news_section_text_v2(
            "First sentence. Second? Incomplete"
        )
        self.assertEqual(result, "First sentence. Second? Incomplete")

    def test_truncated_sentence_cleaned_on_newline(self):
        result = NewsFormatter._coerce_news_section_text_v2(
            "Line one.\nLine two.\nIncomplete"
        )
        self.assertEqual(result, "Line one.\nLine two.")

    def test_complete_sentence_unchanged(self):
        text = "本日の株式市場は大幅な上昇となり、多くの投資家が注目しています。"
        result = NewsFormatter._coerce_news_section_text_v2(text)
        self.assertEqual(result, text)

    def test_sentence_ending_with_quote_unchanged(self):
        text = 'He said "hello".'
        result = NewsFormatter._coerce_news_section_text_v2(text)
        self.assertEqual(result, text)

    def test_single_char_no_punc_returns_empty(self):
        # Single character is noise, returns empty
        result = NewsFormatter._coerce_news_section_text_v2("a")
        self.assertEqual(result, "")


class FlattenTestCase(unittest.TestCase):
    """NewsFormatter._flatten tests - the core recursive flattening logic"""

    def test_none_returns_empty(self):
        self.assertEqual(NewsFormatter._flatten(None), "")

    def test_bool_converts_to_string(self):
        self.assertEqual(NewsFormatter._flatten(True), "True")

    def test_int_converts_to_string(self):
        self.assertEqual(NewsFormatter._flatten(42), "42")

    def test_float_converts_to_string(self):
        self.assertEqual(NewsFormatter._flatten(3.14), "3.14")

    def test_empty_string_returns_empty(self):
        self.assertEqual(NewsFormatter._flatten(""), "")

    def test_whitespace_string_returns_empty(self):
        self.assertEqual(NewsFormatter._flatten("   "), "")

    def test_strips_markdown_fence_from_string(self):
        result = NewsFormatter._flatten("```json\n{\"key\": \"value\"}\n```")
        self.assertIn("key", result)
        self.assertIn("value", result)

    def test_json_string_is_recursively_parsed(self):
        result = NewsFormatter._flatten(
            '{"topic": "AI News", "summary": "Breakthrough!"}'
        )
        self.assertIn("AI News", result)
        self.assertIn("Breakthrough!", result)

    def test_trailing_comma_json_handled(self):
        result = NewsFormatter._flatten('{"topic": "Test",}')
        self.assertIn("Test", result)

    def test_dict_with_topic_summary(self):
        result = NewsFormatter._flatten(
            {"topic": "Earnings", "summary": "Strong results"}
        )
        self.assertIn("Earnings - Strong results", result)

    def test_dict_with_impact_dict(self):
        result = NewsFormatter._flatten(
            {
                "topic": "Fed Decision",
                "summary": "Rates unchanged",
                "market_impact": {"rates": "stable", "dollar": "weak"},
            }
        )
        self.assertIn("Fed Decision", result)
        self.assertIn("rates: stable", result)
        self.assertIn("dollar: weak", result)

    def test_dict_with_impact_string(self):
        result = NewsFormatter._flatten(
            {
                "topic": "IPO News",
                "summary": "New listing",
                "market_impact": "Positive impact expected",
            }
        )
        self.assertIn("IPO News", result)
        self.assertIn("Positive impact expected", result)

    def test_dict_without_topic(self):
        result = NewsFormatter._flatten(
            {"title": "Title Only", "details": "Details only"}
        )
        self.assertIn("Title Only", result)
        self.assertIn("Details only", result)

    def test_dict_misc_fallback(self):
        result = NewsFormatter._flatten({"custom_key": "custom_value"})
        self.assertIn("custom_key: custom_value", result)

    def test_dict_empty_impact_skipped(self):
        result = NewsFormatter._flatten(
            {
                "topic": "Test",
                "market_impact": {"": "val"},
            }
        )
        self.assertIn("Test", result)

    def test_list_of_dicts(self):
        result = NewsFormatter._flatten(
            [
                {"topic": "First", "summary": "First summary"},
                {"topic": "Second", "summary": "Second summary"},
            ]
        )
        self.assertIn("First", result)
        self.assertIn("Second", result)

    def test_list_deduplication(self):
        result = NewsFormatter._flatten(["Good content here.", "Good content here.", "Unique item!"])
        lines = [line.strip() for line in result.split("\n") if line.strip()]
        self.assertEqual(len(lines), 2)  # "Good content here." should appear only once
        self.assertIn("Unique item!", lines)

    def test_max_depth_exceeded(self):
        # Nesting beyond max_depth=5 should just stringify
        deep = [[[[["deep"]]]]]
        result = NewsFormatter._flatten(deep, current_depth=6, max_depth=5)
        self.assertEqual(result, "[[[[['deep']]]]]")

    def test_string_with_json_like_lines_extracted(self):
        text = '"topic": "Market rallies."\n"summary": "Strong gains."'
        result = NewsFormatter._flatten(text)
        self.assertIn("Market rallies.", result)
        self.assertIn("Strong gains.", result)

    def test_quoted_strings_extracted(self):
        text = '"Just a quoted line."\n"Another quoted line."'
        result = NewsFormatter._flatten(text)
        self.assertIn("Just a quoted line.", result)
        self.assertIn("Another quoted line.", result)

    def test_noise_lines_filtered_from_string(self):
        text = '"topic": "Good news."\n"source: foo"\n"date: bar"\n"url: baz"\n"Real content here."'
        result = NewsFormatter._flatten(text)
        self.assertIn("Good news.", result)
        self.assertIn("Real content here.", result)


class IsNoiseNewsLineTestCase(unittest.TestCase):
    """NewsFormatter._is_noise_news_line tests"""

    def test_empty_is_noise(self):
        self.assertTrue(NewsFormatter._is_noise_news_line(""))
        self.assertTrue(NewsFormatter._is_noise_news_line(None))
        self.assertTrue(NewsFormatter._is_noise_news_line("   "))

    def test_source_prefix_is_noise(self):
        self.assertTrue(NewsFormatter._is_noise_news_line("source: Reuters"))

    def test_date_prefix_is_noise(self):
        self.assertTrue(NewsFormatter._is_noise_news_line("Date: 2026-01-01"))

    def test_url_prefix_is_noise(self):
        self.assertTrue(NewsFormatter._is_noise_news_line("URL: https://example.com"))

    def test_html_tags_are_noise(self):
        self.assertTrue(NewsFormatter._is_noise_news_line("<a href='...'>link</a>"))

    def test_http_url_is_noise(self):
        self.assertTrue(NewsFormatter._is_noise_news_line("https://example.com/news"))
        self.assertTrue(NewsFormatter._is_noise_news_line("http://example.com"))

    def test_google_news_url_is_noise(self):
        self.assertTrue(
            NewsFormatter._is_noise_news_line(
                "news.google.com/rss/articles/CB"
            )
        )

    def test_short_cjk_line_10_chars_or_less_is_noise(self):
        self.assertTrue(NewsFormatter._is_noise_news_line("こんにちは"))

    def test_longer_cjk_line_is_not_noise(self):
        self.assertFalse(NewsFormatter._is_noise_news_line("こんにちは世界。今日のニュースです。"))

    def test_short_non_cjk_no_punc_is_noise(self):
        self.assertTrue(NewsFormatter._is_noise_news_line("short line"))

    def test_short_non_cjk_with_punc_not_noise(self):
        self.assertFalse(NewsFormatter._is_noise_news_line("Short!"))

    def test_normal_sentence_not_noise(self):
        self.assertFalse(NewsFormatter._is_noise_news_line("Markets rally on positive economic data."))

    def test_html_list_markers_are_noise(self):
        self.assertTrue(NewsFormatter._is_noise_news_line("<li>item</li>"))
        self.assertTrue(NewsFormatter._is_noise_news_line("<ol>"))


class ParseLinesTestCase(unittest.TestCase):
    """NewsFormatter._parse_lines tests"""

    def test_empty_text_returns_empty(self):
        self.assertEqual(NewsFormatter._parse_lines(None), [])
        self.assertEqual(NewsFormatter._parse_lines(""), [])
        self.assertEqual(NewsFormatter._parse_lines("   "), [])

    def test_strips_bullet_markers(self):
        result = NewsFormatter._parse_lines("- Market rallies on strong data.\n* Tech sector leads gains.\n• Oil prices decline sharply.")
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0], "Market rallies on strong data.")
        self.assertEqual(result[1], "Tech sector leads gains.")
        self.assertEqual(result[2], "Oil prices decline sharply.")

    def test_strips_numbered_markers(self):
        result = NewsFormatter._parse_lines("1. Fed holds rates steady.\n2. Tech stocks surge.\n10. Oil prices drop.")
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0], "Fed holds rates steady.")
        self.assertEqual(result[1], "Tech stocks surge.")

    def test_strips_quotes(self):
        result = NewsFormatter._parse_lines('"Markets are rallying."')
        self.assertEqual(result[0], "Markets are rallying.")

    def test_removes_noise_lines(self):
        result = NewsFormatter._parse_lines(
            "Market rallies on strong data.\nsource: Some Source\nTech sector leads gains."
        )
        self.assertEqual(len(result), 2)
        self.assertIn("Market rallies on strong data.", result)
        self.assertIn("Tech sector leads gains.", result)

    def test_removes_empty_after_strip(self):
        result = NewsFormatter._parse_lines("  \nMarket rallies.\n  ")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], "Market rallies.")


class NormalizeMistralNewsLinesTestCase(unittest.TestCase):
    """NewsFormatter._normalize_mistral_news_lines tests"""

    def test_empty_input_returns_empty(self):
        self.assertEqual(NewsFormatter._normalize_mistral_news_lines(None), "")
        self.assertEqual(NewsFormatter._normalize_mistral_news_lines(""), "")

    def test_basic_lines(self):
        result = NewsFormatter._normalize_mistral_news_lines("Fed holds rates.\nTech stocks surge.\nOil prices drop.")
        self.assertEqual(result, "Fed holds rates.\nTech stocks surge.\nOil prices drop.")

    def test_deduplication(self):
        result = NewsFormatter._normalize_mistral_news_lines("Market rallies.\nUnique content.\nMarket rallies.")
        lines = result.split("\n")
        self.assertEqual(len(lines), 2)
        self.assertIn("Unique content.", lines)

    def test_noise_lines_filtered(self):
        result = NewsFormatter._normalize_mistral_news_lines(
            "Market rallies on strong data.\nsource: Reuters\nTech sector leads gains.\nURL: http://x.com\nOil prices decline."
        )
        self.assertIn("Market rallies on strong data.", result)
        self.assertIn("Tech sector leads gains.", result)
        self.assertNotIn("source: Reuters", result)

    def test_max_lines_enforced(self):
        many_lines = "\n".join([f"Line {i} content." for i in range(20)])
        result = NewsFormatter._normalize_mistral_news_lines(many_lines, max_lines=5)
        lines = result.split("\n")
        self.assertEqual(len(lines), 5)

    def test_case_insensitive_dedup(self):
        result = NewsFormatter._normalize_mistral_news_lines("Hello\nhello\nHELLO")
        lines = result.split("\n")
        self.assertEqual(len(lines), 1)


if __name__ == "__main__":
    unittest.main()
