"""Additional unit tests for services/search_service.py.

Covers remaining uncovered functions: strategy determination, trending titles,
compact context, hybrid search, and market trending functions.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from services import search_service


class DetermineSearchStrategyTestCase(unittest.TestCase):
    """_determine_search_strategy tests"""

    def test_langsearch_when_key_present(self):
        result = search_service._determine_search_strategy(
            tavily_api_key="", langsearch_api_key="ls-key"
        )
        self.assertEqual(result, "langsearch")

    def test_ddgs_tavily_when_tavily_present(self):
        result = search_service._determine_search_strategy(
            tavily_api_key="tv-key", langsearch_api_key=""
        )
        self.assertEqual(result, "ddgs_tavily")

    def test_ddgs_only_when_no_keys(self):
        result = search_service._determine_search_strategy(tavily_api_key="", langsearch_api_key="")
        self.assertEqual(result, "ddgs_only")

    def test_langsearch_preferred_over_tavily(self):
        result = search_service._determine_search_strategy(
            tavily_api_key="tv-key", langsearch_api_key="ls-key"
        )
        self.assertEqual(result, "langsearch")


class ExtractTrendingTitlesFromItemsTestCase(unittest.TestCase):
    """_extract_trending_titles_from_items tests"""

    def test_empty_items_returns_empty(self):
        self.assertEqual(search_service._extract_trending_titles_from_items([]), [])

    def test_extracts_titles(self):
        items = [
            {"title": "First Article"},
            {"title": "Second Article"},
            {"title": ""},
        ]
        result = search_service._extract_trending_titles_from_items(items)
        self.assertEqual(result, ["First Article", "Second Article"])

    def test_respects_count(self):
        items = [{"title": f"Article {i}"} for i in range(20)]
        result = search_service._extract_trending_titles_from_items(items, count=3)
        self.assertEqual(len(result), 3)

    def test_deduplicates(self):
        items = [
            {"title": "Same Title"},
            {"title": "Same Title"},
            {"title": "Other Title"},
        ]
        result = search_service._extract_trending_titles_from_items(items)
        self.assertEqual(len(result), 2)

    def test_handles_none_title(self):
        items = [{"title": None}, {"title": "Real Title"}]
        result = search_service._extract_trending_titles_from_items(items)
        self.assertEqual(result, ["Real Title"])

    def test_whitespace_title_skipped(self):
        items = [{"title": "  "}, {"title": "Valid Title"}]
        result = search_service._extract_trending_titles_from_items(items)
        self.assertEqual(result, ["Valid Title"])


class CompactSmallModelContextTestCase(unittest.TestCase):
    """_compact_small_model_context tests"""

    @patch("services.search_service.ts.compact_context", return_value="compact result")
    def test_delegates_to_compact_context(self, mock_compact):
        items = [{"title": "Test", "url": "https://example.com"}]
        result = search_service._compact_small_model_context(items, limit=7, max_chars=1800)
        self.assertEqual(result, "compact result")
        mock_compact.assert_called_once_with(items, limit=7)

    @patch("services.search_service.ts.compact_context", return_value="x" * 2000)
    def test_truncates_when_exceeds_max_chars(self, mock_compact):
        items = [{"title": "Test", "url": "https://example.com"}]
        result = search_service._compact_small_model_context(items, limit=7, max_chars=1000)
        self.assertEqual(len(result), 1000)

    @patch("services.search_service.ts.compact_context", return_value="short result")
    def test_short_text_not_truncated(self, mock_compact):
        items = [{"title": "Test", "url": "https://example.com"}]
        result = search_service._compact_small_model_context(items, limit=7, max_chars=1800)
        self.assertEqual(result, "short result")


class CollectHybridItemsTestCase(unittest.TestCase):
    """_collect_hybrid_items tests"""

    @patch("services.search_service._collect_ddgs_items")
    @patch("services.search_service.ts.dedupe_items", side_effect=lambda items: list(items))
    def test_uses_ddgs_only_when_sufficient(self, mock_dedup, mock_ddgs):
        mock_ddgs.return_value = [
            {"title": f"Article {i}", "url": f"https://example.com/{i}"} for i in range(10)
        ]
        result = search_service._collect_hybrid_items(
            ["query"], "us", "d", 2, 1, tavily_api_key="tv-key", limit=5
        )
        self.assertEqual(len(result), 5)

    @patch("services.search_service._collect_ddgs_items")
    @patch("services.search_service._collect_tavily_items")
    @patch("services.search_service.ts.dedupe_items", side_effect=lambda items: list(items))
    def test_supplements_with_tavily_when_sparse(self, mock_dedup, mock_tavily, mock_ddgs):
        mock_ddgs.return_value = [{"title": "Only One", "url": "https://example.com/1"}]
        mock_tavily.return_value = [{"title": "Tavily Result", "url": "https://example.com/t1"}]
        result = search_service._collect_hybrid_items(
            ["query"], "us", "d", 2, 1, tavily_api_key="tv-key", limit=5
        )
        self.assertGreaterEqual(len(result), 2)

    @patch("services.search_service._collect_ddgs_items")
    @patch("services.search_service._collect_tavily_items")
    @patch("services.search_service.ts.dedupe_items", side_effect=lambda items: list(items))
    def test_tavily_failure_falls_back_to_ddgs(self, mock_dedup, mock_tavily, mock_ddgs):
        mock_ddgs.return_value = [{"title": "Only One", "url": "https://example.com/1"}]
        mock_tavily.side_effect = RuntimeError("Tavily error")
        result = search_service._collect_hybrid_items(
            ["query"], "us", "d", 2, 1, tavily_api_key="tv-key", limit=5
        )
        self.assertEqual(len(result), 1)

    @patch("services.search_service._collect_ddgs_items")
    def test_no_tavily_key_returns_ddgs_only(self, mock_ddgs):
        mock_ddgs.return_value = [{"title": "Only", "url": "https://example.com/1"}]
        result = search_service._collect_hybrid_items(
            ["query"], "us", "d", 2, 1, tavily_api_key="", limit=5
        )
        self.assertEqual(len(result), 1)


class CollectSymbolResearchContextTestCase(unittest.TestCase):
    """collect_symbol_research_context tests"""

    @patch("services.search_service._execute_search_strategy")
    @patch("services.search_service._compact_small_model_context", return_value="compact")
    @patch("services.search_service.ts.collect_symbol_research_items", return_value=[])
    @patch("services.search_service.ts.dedupe_items", side_effect=lambda items: list(items))
    def test_basic_call(self, mock_dedup, mock_collect, mock_compact, mock_execute):
        mock_execute.return_value = []
        result = search_service.collect_symbol_research_context("AAPL", "Apple", market="us")
        self.assertEqual(result, "compact")
        mock_execute.assert_called_once()

    @patch("services.search_service._execute_search_strategy")
    @patch("services.search_service._compact_small_model_context", return_value="compact")
    @patch(
        "services.search_service.ts.collect_symbol_research_items",
        return_value=[{"title": "Existing", "url": "u"}],
    )
    @patch("services.search_service.ts.dedupe_items", side_effect=lambda items: list(items))
    def test_merges_trend_sources_with_search(
        self, mock_dedup, mock_collect, mock_compact, mock_execute
    ):
        mock_execute.return_value = [{"title": "Search", "url": "s"}]
        result = search_service.collect_symbol_research_context(
            "AAPL", "Apple", market="us", langsearch_api_key="key"
        )
        self.assertEqual(result, "compact")


class CollectMarketTrendingTitlesTestCase(unittest.TestCase):
    """collect_market_trending_titles tests"""

    @patch(
        "services.search_service._get_market_trending_titles", return_value=["Trend 1", "Trend 2"]
    )
    def test_returns_titles(self, mock_get):
        result = search_service.collect_market_trending_titles(
            market="us", count=10, langsearch_api_key=""
        )
        self.assertEqual(result, ["Trend 1", "Trend 2"])
        mock_get.assert_called_once_with("us", "ddgs_only", "", "")

    @patch(
        "services.search_service._get_market_trending_titles",
        return_value=["T1", "T2", "T3", "T4", "T5"],
    )
    def test_respects_count_limit(self, mock_get):
        result = search_service.collect_market_trending_titles(
            market="us", count=3, langsearch_api_key=""
        )
        self.assertEqual(len(result), 3)

    @patch("services.search_service._get_market_trending_titles", return_value=[])
    def test_empty_result(self, mock_get):
        result = search_service.collect_market_trending_titles(
            market="us", count=10, langsearch_api_key=""
        )
        self.assertEqual(result, [])


class BuildMarketTrendingTitlesTestCase(unittest.TestCase):
    """_build_market_trending_titles tests"""

    @patch("services.search_service.ts.collect_market_trending_titles", return_value=["Trend A"])
    @patch("services.search_service._execute_search_strategy", return_value=[])
    @patch("services.search_service._extract_trending_titles_from_items", return_value=[])
    @patch("services.search_service._market_ddgs_queries", return_value=("us", ["query"]))
    @patch("services.search_service._determine_search_strategy", return_value="ddgs_only")
    @patch("services.search_service.ts.dedupe_items", side_effect=lambda items: list(items))
    def test_merges_titles(
        self, mock_dedup, mock_strategy, mock_queries, mock_extract, mock_execute, mock_collect
    ):
        result = search_service._build_market_trending_titles(
            "us", langsearch_api_key="", tavily_api_key=""
        )
        self.assertIn("Trend A", result)

    @patch("services.search_service.ts.collect_market_trending_titles", return_value=[])
    @patch(
        "services.search_service._execute_search_strategy",
        return_value=[{"title": "Search Title", "url": "u"}],
    )
    @patch(
        "services.search_service._extract_trending_titles_from_items", return_value=["Search Title"]
    )
    @patch("services.search_service._market_ddgs_queries", return_value=("us", ["query"]))
    @patch("services.search_service._determine_search_strategy", return_value="ddgs_only")
    @patch("services.search_service.ts.dedupe_items", side_effect=lambda items: list(items))
    def test_includes_search_titles(
        self, mock_dedup, mock_strategy, mock_queries, mock_extract, mock_execute, mock_collect
    ):
        result = search_service._build_market_trending_titles(
            "us", langsearch_api_key="", tavily_api_key=""
        )
        self.assertIn("Search Title", result)


class GetMarketTrendingTitlesTestCase(unittest.TestCase):
    """_get_market_trending_titles tests"""

    @patch("services.search_service._get_cached_value")
    def test_returns_cached_list(self, mock_cache):
        mock_cache.return_value = ["Cached 1", "Cached 2"]
        result = search_service._get_market_trending_titles("us", "ddgs_only", "", "")
        self.assertEqual(result, ["Cached 1", "Cached 2"])

    @patch("services.search_service._get_cached_value")
    def test_handles_cached_string(self, mock_cache):
        mock_cache.return_value = "str1、str2"
        result = search_service._get_market_trending_titles("us", "ddgs_only", "", "")
        self.assertEqual(result, ["str1", "str2"])

    @patch("services.search_service._get_cached_value", return_value=None)
    @patch("services.search_service._build_market_trending_titles", return_value=["New 1", "New 2"])
    @patch("services.search_service._set_cached_value")
    def test_builds_when_cache_miss(self, mock_set, mock_build, mock_cache):
        result = search_service._get_market_trending_titles("us", "ddgs_only", "", "")
        self.assertEqual(result, ["New 1", "New 2"])

    @patch("services.search_service._get_cached_value", return_value=None)
    @patch("services.search_service._build_market_trending_titles", return_value=[])
    @patch("services.search_service._schedule_market_trends_refresh_async", return_value=True)
    @patch("services.search_service._set_cached_value")
    def test_schedules_refresh_when_build_empty(
        self, mock_set, mock_schedule, mock_build, mock_cache
    ):
        result = search_service._get_market_trending_titles("us", "ddgs_only", "", "")
        self.assertEqual(result, [])


class ScheduleMarketTrendsRefreshAsyncTestCase(unittest.TestCase):
    """_schedule_market_trends_refresh_async tests"""

    def setUp(self):
        from app_state import app_state

        app_state.ai.trends_refresh_inflight.clear()

    @patch("services.search_service.app_state.execution.executor.submit")
    @patch("services.search_service._build_market_trending_titles", return_value=["Title"])
    @patch("services.search_service._set_cached_value")
    def test_refresh_submitted(self, mock_set, mock_build, mock_submit):
        result = search_service._schedule_market_trends_refresh_async(
            "us", "ddgs_only", langsearch_api_key=""
        )
        self.assertTrue(result)
        mock_submit.assert_called_once()

    @patch("services.search_service.app_state.execution.executor.submit")
    def test_prevents_duplicate_inflight(self, mock_submit):
        search_service._schedule_market_trends_refresh_async("us", "ddgs_only", "")
        result = search_service._schedule_market_trends_refresh_async("us", "ddgs_only", "")
        self.assertFalse(result)


class ExecuteSearchStrategyTestCase(unittest.TestCase):
    """_execute_search_strategy tests"""

    @patch(
        "services.search_service._collect_ddgs_items",
        return_value=[{"title": "DDGS Item", "url": "u"}],
    )
    def test_ddgs_only_strategy(self, mock_ddgs):
        result = search_service._execute_search_strategy(
            "ddgs_only",
            ["query"],
            "us",
            "d",
            news_n=2,
            text_n=1,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "DDGS Item")

    @patch(
        "services.search_service._collect_langsearch_items",
        return_value=[{"title": "LS Item", "url": "u"}],
    )
    def test_langsearch_strategy(self, mock_ls):
        result = search_service._execute_search_strategy(
            "langsearch",
            ["query"],
            "us",
            "d",
            news_n=2,
            text_n=1,
            langsearch_api_key="key",
        )
        self.assertEqual(len(result), 1)

    @patch("services.search_service._collect_langsearch_items", return_value=[])
    @patch(
        "services.search_service._collect_ddgs_items",
        return_value=[{"title": "Fallback", "url": "u"}],
    )
    def test_langsearch_empty_fallsback_to_ddgs(self, mock_ddgs, mock_ls):
        result = search_service._execute_search_strategy(
            "langsearch",
            ["query"],
            "us",
            "d",
            news_n=2,
            text_n=1,
            langsearch_api_key="key",
        )
        self.assertEqual(result[0]["title"], "Fallback")

    @patch(
        "services.search_service._collect_hybrid_items",
        return_value=[{"title": "Hybrid", "url": "u"}],
    )
    def test_ddgs_tavily_strategy(self, mock_hybrid):
        result = search_service._execute_search_strategy(
            "ddgs_tavily",
            ["query"],
            "us",
            "d",
            news_n=2,
            text_n=1,
            tavily_api_key="tv-key",
        )
        self.assertEqual(result[0]["title"], "Hybrid")


if __name__ == "__main__":
    unittest.main()
