"""tests/test_review_autonomous_goal_fixes.py

Comprehensive regression test suite for autonomous code review fixes R1-R12:
- R1: Shutdown token rotation unlinks used marker; commit persists consumption marker
- R2: Watchlist mutations invalidate SSE payload cache to broadcast real updates
- R3: Custom AI portfolio rebalancing updates in-place, preserving ID & eliminating duplicates
- R4: AI technical lines prompt formats epoch ms timestamps (x) into YYYY-MM-DD
- R5: Concurrency safety in _is_polling_token_inflight with lock protection
- R6: Nikkei225JP scraper direct ADR fallback parses var A0 from response text
- R7: RealtimeMarketEngine.register_symbol gracefully handles stopped background executor
- R8: Fallback scraper providers honor global is_scraper_blocked() cooldown
- R9: Stream Mistral chat extracts usage telemetry from Pydantic UsageInfo models
- R10: Wikipedia trending topics filter out Japanese placeholder "メインページ"
- R11: LangSearch semantic rerank handles enveloped response format {"data": ...}
- R12: News section formatter correctly truncates English sentences with periods/exclamations
"""

from __future__ import annotations

import json
import logging
import threading
import time
from unittest.mock import MagicMock, patch

from app_state import app_state
from route_helpers import _is_polling_token_inflight
from routes.api_analysis import (
    analyze_fetch_lock,
    analyze_result_cache,
    chat_fetch_lock,
    chat_result_cache,
)
from routes.api_stocks import _announce_watchlist_state
from services.ai_portfolio_service import (
    generate_ai_portfolio_by_theme,
    load_saved_ai_portfolios,
)
from services.ai_service import generate_ai_technical_lines, stream_mistral_chat
from services.fallback_provider import (
    MinkabuProvider,
    Nikkei225JPProvider,
    YahooJPScraperProvider,
    YahooWebScraperProvider,
)
from services.news_formatter import _coerce_news_section_text_v2
from services.realtime_engine import Nikkei225JPScraper, RealtimeMarketEngine
from services.search.langsearch import langsearch_rerank
from shutdown_manager import ShutdownTokenManager
from trend_sources import collect_wikipedia_top_items


# ---------------------------------------------------------------------------
# R1: Shutdown Token Rotation & Commit Persistence
# ---------------------------------------------------------------------------
def test_shutdown_token_rotation_and_commit(tmp_path):
    logger = logging.getLogger("test_shutdown_token")
    mgr = ShutdownTokenManager(logger)
    mgr.token_file = tmp_path / ".mns_shutdown_token"
    mgr.used_marker = tmp_path / ".mns_shutdown_token.used"
    mgr._legacy_token_file = tmp_path / ".legacy_token"
    mgr._legacy_used_marker = tmp_path / ".legacy_used"
    mgr.runtime_state_dir = tmp_path

    # Initial creation
    tok1 = mgr.get_or_create_shutdown_token()
    assert tok1 and isinstance(tok1, str)
    assert not mgr.used_marker.exists()

    # Pre-validation succeeds
    assert mgr.validate_shutdown_token(tok1) is True
    assert mgr.shutdown_token_used is False

    # Commit marks used in memory AND persists .used marker to disk
    mgr.commit_shutdown_token()
    assert mgr.shutdown_token_used is True
    assert mgr.used_marker.exists()
    assert mgr.validate_shutdown_token(tok1) is False

    # Rotate token creates a NEW token and unlinks .used marker
    mgr.rotate_shutdown_token()
    tok2 = mgr.get_or_create_shutdown_token()
    assert tok2 and tok2 != tok1
    assert not mgr.used_marker.exists()
    assert mgr.shutdown_token_used is False
    assert mgr.validate_shutdown_token(tok2) is True


# ---------------------------------------------------------------------------
# R2: Watchlist SSE Cache Invalidation
# ---------------------------------------------------------------------------
def test_watchlist_mutation_triggers_sse_cache_invalidation():
    import app_bg

    initial_gen = app_bg._sse_payload_generation

    with patch("app_bg.announce_current_market_state"), patch("app_bg.announce_real_market_state"):
        _announce_watchlist_state()

    # Generation counter must be incremented
    assert app_bg._sse_payload_generation > initial_gen


# ---------------------------------------------------------------------------
# R3: Custom AI Portfolio Rebalancing Updates In-Place
# ---------------------------------------------------------------------------
def test_custom_ai_portfolio_rebalance_persists_in_place(tmp_path):
    theme = "Clean Energy 2026"

    with patch("services.ai_portfolio_service.AI_PORTFOLIO_STORAGE_FILE", tmp_path / "ai_portfolios.json"):
        # 1. Generate initial custom portfolio
        with patch("services.ai_portfolio_service.call_mistral_chat") as mock_chat:
            mock_chat.return_value = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "title": "Clean Energy v1",
                                    "description": "Initial portfolio",
                                    "risk_level": "mid",
                                    "expected_return": "10%",
                                    "commentary": "v1 commentary",
                                    "items": [
                                        {
                                            "symbol": "NVDA",
                                            "market": "us",
                                            "weight_pct": 50.0,
                                            "target_price": 150.0,
                                            "rationale": "GPU AI",
                                            "risk_level": "mid",
                                        },
                                        {
                                            "symbol": "9501.T",
                                            "market": "jp",
                                            "weight_pct": 50.0,
                                            "target_price": 700.0,
                                            "rationale": "Power",
                                            "risk_level": "mid",
                                        },
                                    ],
                                }
                            )
                        }
                    }
                ]
            }
            p1 = generate_ai_portfolio_by_theme(theme, api_key="dummy_key")
            assert p1["title"] == "Clean Energy v1"
            custom_id = p1["id"]
            assert custom_id.startswith("custom-")

        # 2. Rebalance the same portfolio
        with patch("services.ai_portfolio_service.call_mistral_chat") as mock_chat:
            mock_chat.return_value = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "title": "Clean Energy v2 Rebalanced",
                                    "description": "Rebalanced portfolio",
                                    "risk_level": "mid",
                                    "expected_return": "12%",
                                    "commentary": "v2 commentary",
                                    "items": [
                                        {
                                            "symbol": "MSFT",
                                            "market": "us",
                                            "weight_pct": 60.0,
                                            "target_price": 450.0,
                                            "rationale": "Cloud",
                                            "risk_level": "mid",
                                        },
                                        {
                                            "symbol": "8306.T",
                                            "market": "jp",
                                            "weight_pct": 40.0,
                                            "target_price": 1800.0,
                                            "rationale": "Finance",
                                            "risk_level": "mid",
                                        },
                                    ],
                                }
                            )
                        }
                    }
                ]
            }
            p2 = generate_ai_portfolio_by_theme(custom_id, force_rebalance=True, api_key="dummy_key")
            assert p2["id"] == custom_id
            assert p2["title"] == "Clean Energy v2 Rebalanced"

        # 3. Verify saved storage has exactly 1 portfolio and returns the rebalanced version
        saved_list = load_saved_ai_portfolios()
        matching = [p for p in saved_list if p.get("theme") == theme or p.get("id") == custom_id]
        assert len(matching) == 1
        assert matching[0]["title"] == "Clean Energy v2 Rebalanced"

        # 4. Subsequent retrieval without force_rebalance returns the updated v2
        p_retrieved = generate_ai_portfolio_by_theme(theme, force_rebalance=False)
        assert p_retrieved["title"] == "Clean Energy v2 Rebalanced"


# ---------------------------------------------------------------------------
# R4: AI Technical Lines Candlestick Timestamp Parsing
# ---------------------------------------------------------------------------
def test_ai_technical_lines_candlestick_timestamp_conversion():
    history_data = [
        {"x": 1716163200000, "o": 150.0, "h": 155.0, "l": 149.0, "c": 153.0, "v": 100000},
        {"x": 1716249600000, "o": 153.0, "h": 158.0, "l": 152.0, "c": 157.0, "v": 120000},
    ]

    with patch("services.ai_service.call_mistral_chat") as mock_chat:
        mock_chat.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "summary": "Bullish trend",
                                "trend_bias": "Bullish",
                                "lines": [],
                            }
                        )
                    }
                }
            ]
        }

        res = generate_ai_technical_lines("dummy_key", "AAPL", "us", "1mo", history_data)
        assert "error" not in res

        # Verify the prompt contained the formatted dates (2024-05-20 and 2024-05-21)
        call_args = mock_chat.call_args[0]
        messages = call_args[1]
        user_message = next(m for m in messages if m.get("role") == "user")
        prompt_text = user_message["content"]
        assert "2024-05-20: O=150.0, H=155.0, L=149.0, C=153.0" in prompt_text
        assert "2024-05-21: O=153.0, H=158.0, L=152.0, C=157.0" in prompt_text


# ---------------------------------------------------------------------------
# R5: Thread Safety in _is_polling_token_inflight
# ---------------------------------------------------------------------------
def test_polling_token_inflight_thread_safety():
    token = "test_token_1234567890123456"

    # Simulate concurrently modifying cache while checking inflight
    def _cache_mutator():
        for i in range(100):
            with chat_fetch_lock:
                chat_result_cache[f"chat:scope:{token}_{i}"] = (time.time(), {"ok": True}, None)
            with analyze_fetch_lock:
                analyze_result_cache[f"analyze:scope:{token}_{i}"] = (time.time(), {"ok": True}, None)
            time.sleep(0.001)

    t = threading.Thread(target=_cache_mutator)
    t.start()

    try:
        for _ in range(50):
            # Should not raise KeyError or RuntimeError
            _is_polling_token_inflight(token)
            time.sleep(0.002)
    finally:
        t.join()
        with chat_fetch_lock:
            for k in list(chat_result_cache.keys()):
                if token in k:
                    chat_result_cache.pop(k, None)
        with analyze_fetch_lock:
            for k in list(analyze_result_cache.keys()):
                if token in k:
                    analyze_result_cache.pop(k, None)


# ---------------------------------------------------------------------------
# R6: Nikkei225JP Scraper Direct ADR var A0 Parsing
# ---------------------------------------------------------------------------
def test_nikkei225jp_scraper_adr_var_a0_parsing():
    scraper = Nikkei225JPScraper()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    # Response contains var Sno and var A0
    mock_resp.text = (
        'var Sno="7203";\n'
        'var A0="7203_TOYOTA_T_3000.0_15.0_0.50_3005.0_2990.0_3000.0_15.0_0.50_100000_12345_2026-08-15";\n'
    )

    with patch.object(scraper, "_get_session") as mock_get_session, \
         patch.object(scraper, "_refresh_adr_cache", return_value={}):
        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_get_session.return_value = mock_session

        quote = scraper.fetch_quote("7203.T")
        assert quote is not None
        assert quote["price"] == 3000.0
        assert quote["change"] == 15.0
        assert quote["change_percent"] == 0.50


# ---------------------------------------------------------------------------
# R7: RealtimeMarketEngine.register_symbol on Stopped Executor
# ---------------------------------------------------------------------------
def test_register_symbol_graceful_on_shutdown_executor():
    with patch("services.realtime_engine.TradingViewWSClient"), \
         patch("services.realtime_engine.YahooJPRealtimeScraper"):
        engine = RealtimeMarketEngine()
        engine.tv_client = MagicMock()
        engine.yahoojp_scraper = MagicMock()
        try:
            engine._bg_executor.shutdown(wait=False)

            # Should not raise RuntimeError: cannot schedule new futures after shutdown
            engine.register_symbol("7203.T", "jp")
            engine.register_symbol("AAPL", "us")
        finally:
            engine.stop()


# ---------------------------------------------------------------------------
# R8: Fallback Scraper Providers Honor is_scraper_blocked()
# ---------------------------------------------------------------------------
def test_fallback_providers_honor_scraper_blocked():
    with patch.object(app_state.market, "is_scraper_blocked", return_value=True):
        yahoo_web = YahooWebScraperProvider()
        yahoo_jp = YahooJPScraperProvider()
        nikkei = Nikkei225JPProvider()
        minkabu = MinkabuProvider()

        assert yahoo_web.get_latest_quote("AAPL") is None
        assert yahoo_jp.get_latest_quote("7203.T") is None
        assert nikkei.get_latest_quote("7203.T") is None
        assert minkabu.get_latest_quote("7203.T") is None


# ---------------------------------------------------------------------------
# R9: Stream Mistral Chat Pydantic Usage Extraction
# ---------------------------------------------------------------------------
def test_stream_mistral_chat_pydantic_usage_capture():
    class DummyUsageInfo:
        def model_dump(self):
            return {"prompt_tokens": 120, "completion_tokens": 45, "total_tokens": 165}

    class DummyChunk:
        def __init__(self, text, usage=None):
            self.choices = [MagicMock(delta=MagicMock(content=text))]
            self.usage = usage

    chunks = [
        DummyChunk("Hello"),
        DummyChunk(" world", usage=DummyUsageInfo()),
    ]

    mock_client = MagicMock()
    mock_client.chat.stream.return_value = iter(chunks)

    with patch("services.ai_service._get_mistral_client", return_value=mock_client), \
         patch("services.ai_service.app_state.ai.record_mistral_usage") as mock_record:
        events = list(stream_mistral_chat("dummy_key", [{"role": "user", "content": "hi"}]))
        assert any(e.get("type") == "done" for e in events)
        mock_record.assert_called_once_with({"prompt_tokens": 120, "completion_tokens": 45, "total_tokens": 165})


# ---------------------------------------------------------------------------
# R10: Wikipedia Top Items Filtering
# ---------------------------------------------------------------------------
def test_wikipedia_top_items_filters_main_page():
    mock_payload = {
        "items": [
            {
                "articles": [
                    {"article": "メインページ", "views": 500000},
                    {"article": "特別:検索", "views": 200000},
                    {"article": "トヨタ自動車", "views": 150000},
                ]
            }
        ]
    }

    with patch("trend_sources._request_json", return_value=mock_payload):
        items = collect_wikipedia_top_items(market="jp", limit=5)
        titles = [item["title"] for item in items]
        assert "メインページ" not in titles
        assert "特別:検索" not in titles
        assert "トヨタ自動車" in titles


# ---------------------------------------------------------------------------
# R11: LangSearch Rerank Enveloped Payload Handling
# ---------------------------------------------------------------------------
def test_langsearch_rerank_enveloped_payload():
    documents = [
        {"title": "Doc A", "summary": "Summary A"},
        {"title": "Doc B", "summary": "Summary B"},
    ]
    enveloped_response = {
        "code": 200,
        "data": {
            "results": [
                {"index": 1, "relevance_score": 0.95},
                {"index": 0, "relevance_score": 0.40},
            ]
        },
    }

    with patch("services.search.langsearch._langsearch_post_json", return_value=enveloped_response):
        reranked = langsearch_rerank("test query", documents, api_key="dummy_key")
        assert len(reranked) == 2
        assert reranked[0]["title"] == "Doc B"
        assert reranked[1]["title"] == "Doc A"


# ---------------------------------------------------------------------------
# R12: News Formatter English Punctuation Truncation
# ---------------------------------------------------------------------------
def test_news_formatter_english_punctuation_truncation():
    # Incomplete sentence after period
    raw_text = "Apple reports strong Q3 earnings. Revenue grew 15% year over year. The CEO stated that new products"
    result = _coerce_news_section_text_v2(raw_text)
    assert result == "Apple reports strong Q3 earnings. Revenue grew 15% year over year."

    # Incomplete sentence after exclamation
    raw_text_exclamation = "Major breakthrough in semiconductor tech! Stock surges 20%! Analysts expect further"
    result_exclamation = _coerce_news_section_text_v2(raw_text_exclamation)
    assert result_exclamation == "Major breakthrough in semiconductor tech! Stock surges 20%!"
