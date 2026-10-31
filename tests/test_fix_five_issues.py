"""
tests/test_fix_five_issues.py - Comprehensive tests covering the 5 issue fixes:
1. Chart fullscreen layer & z-index
2. Stock volume non-zero fallback & realtime preservation
3. Stock data fetching stability (disk-cache fallback & MultiIndex)
4. OpenAI-compatible API JSON parsing & token limit hardening
5. Test state isolation
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from app_state import app_state
from constants import (
    OPENAI_MAX_TOKENS_CEIL,
    OPENAI_MAX_TOKENS_DEFAULT,
    OPENAI_REASONING_MIN_TOKENS,
)
from services.openai_service import (
    _extract_and_parse_openai_json,
    _is_openai_reasoning_model,
    call_openai_chat,
)
from services.realtime.engine import RealtimeMarketEngine
from services.stock.provider import YFinanceProvider
from tests.test_support.state_reset import reset_app_state_internals
from utils.stock_payload import build_stock_payload
from utils.validators import StockAnalysis


class TestIssue1ChartFullscreenLayer:
    """Issue 1: Chart fullscreen display z-index / layering."""

    def test_css_variables_and_rules(self):
        colors_css = Path("static/css/colors.css").read_text(encoding="utf-8")
        assert "--z-fullscreen: 20000;" in colors_css

        index_css = Path("static/css/index.css").read_text(encoding="utf-8")
        assert "var(--z-fullscreen, 20000) !important;" in index_css
        assert ".chart-fullscreen-modal.show" in index_css

    def test_ui_js_modal_z_index(self):
        ui_js = Path("static/js/ui.js").read_text(encoding="utf-8")
        assert 'modal.style.zIndex = "20000"' in ui_js
        assert 'modal.style.zIndex = ""' in ui_js


class TestIssue2VolumeFallbackAndPreservation:
    """Issue 2: Volume displayed as 0 fixed with positive-history and realtime preservation."""

    def test_build_stock_payload_positive_volume_fallback(self):
        # Create history DataFrame where the last row has Volume == 0 (e.g. off-hours / incomplete candle)
        # but earlier row has Volume == 1500000
        dates = pd.date_range("2026-01-01", periods=3, freq="D")
        hist = pd.DataFrame(
            {
                "Open": [100.0, 102.0, 105.0],
                "High": [103.0, 106.0, 107.0],
                "Low": [99.0, 101.0, 104.0],
                "Close": [102.0, 105.0, 106.0],
                "Volume": [1200000, 1500000, 0],
            },
            index=dates,
        )

        payload = build_stock_payload("7203.T", "トヨタ", "jp", hist, lightweight=True)
        assert payload is not None
        # Should fallback to positive volume from previous candle, not 0
        assert payload["volume"] == 1500000

    def test_build_stock_payload_info_volume_fallback_when_all_hist_zero(self):
        dates = pd.date_range("2026-01-01", periods=2, freq="D")
        hist = pd.DataFrame(
            {
                "Open": [100.0, 102.0],
                "High": [103.0, 106.0],
                "Low": [99.0, 101.0],
                "Close": [102.0, 105.0],
                "Volume": [0, 0],
            },
            index=dates,
        )
        fake_info = {"regularMarketVolume": 850000}
        with patch("utils.stock_payload.get_stock_info_cached", return_value=fake_info):
            payload = build_stock_payload("7203.T", "トヨタ", "jp", hist, lightweight=False)
            assert payload is not None
            assert payload["volume"] == 850000

    def test_realtime_engine_preserves_positive_volume(self):
        engine = RealtimeMarketEngine()
        # Seed positive volume
        initial_payload = {
            "symbol": "7203.T",
            "price": 2500.0,
            "volume": 500000,
            "source": "initial",
        }
        engine._handle_producer_update(initial_payload)
        assert engine.market_store["7203.T"]["volume"] == 500000

        # Update with volume = 0 (e.g. from a scraper that doesn't report volume)
        zero_vol_payload = {
            "symbol": "7203.T",
            "price": 2510.0,
            "volume": 0,
            "source": "scraper_zero",
        }
        engine._handle_producer_update(zero_vol_payload)
        # Volume should stay 500000 instead of being wiped to 0
        assert engine.market_store["7203.T"]["volume"] == 500000
        assert engine.market_store["7203.T"]["price"] == 2510.0


class TestIssue3DataFetchingStability:
    """Issue 3: Disk cache fallback, MultiIndex indexing."""

    def test_provider_disk_cache_fallback_when_circuit_open(self):
        provider = YFinanceProvider()
        dates = pd.date_range("2026-01-01", periods=2, freq="D")
        cached_df = pd.DataFrame(
            {
                "Open": [100.0, 102.0],
                "High": [103.0, 106.0],
                "Low": [99.0, 101.0],
                "Close": [102.0, 105.0],
                "Volume": [1000, 2000],
            },
            index=dates,
        )

        with patch.object(provider, "_load_history_from_disk_cache", return_value=cached_df):
            with patch.object(app_state.market, "is_circuit_open", return_value=True):
                df = provider.get_history("AAPL", period="1mo")
                assert not df.empty
                assert len(df) == 2
                assert list(df["Close"]) == [102.0, 105.0]

    def test_extract_batch_history_level_0_and_level_1(self):
        from bg.sync.fetch import extract_batch_history

        dates = pd.date_range("2026-01-01", periods=2, freq="D")
        raw_data = {"Open": [10.0, 11.0], "Close": [12.0, 13.0]}

        # Level 1 MultiIndex: columns=(Metric, Symbol)
        mi_level1 = pd.MultiIndex.from_tuples(
            [("Open", "AAPL"), ("Close", "AAPL")], names=["Price", "Ticker"]
        )
        df_l1 = pd.DataFrame(raw_data, index=dates)
        df_l1.columns = mi_level1
        extracted1 = extract_batch_history(df_l1, "AAPL")
        assert not extracted1.empty
        assert "Close" in extracted1.columns

        # Level 0 MultiIndex: columns=(Symbol, Metric)
        mi_level0 = pd.MultiIndex.from_tuples(
            [("AAPL", "Open"), ("AAPL", "Close")], names=["Ticker", "Price"]
        )
        df_l0 = pd.DataFrame(raw_data, index=dates)
        df_l0.columns = mi_level0
        extracted0 = extract_batch_history(df_l0, "AAPL")
        assert not extracted0.empty
        assert "Close" in extracted0.columns


class TestIssue4OpenAIJSONAndTokens:
    """Issue 4: OpenAI API token limits and JSON parse hardening."""

    def test_constants_and_token_defaults(self):
        assert OPENAI_MAX_TOKENS_DEFAULT == 2500
        assert OPENAI_MAX_TOKENS_CEIL >= 16000
        assert OPENAI_REASONING_MIN_TOKENS >= 4000

    def test_is_openai_reasoning_model(self):
        assert _is_openai_reasoning_model("o1-mini")
        assert _is_openai_reasoning_model("o3-mini")
        assert _is_openai_reasoning_model("deepseek-reasoner")
        assert _is_openai_reasoning_model("deepseek-r1")
        assert _is_openai_reasoning_model("qwen-reasoning-model")
        assert not _is_openai_reasoning_model("gpt-4o")
        assert not _is_openai_reasoning_model("mistral-small-latest")

    def test_extract_and_parse_openai_json_with_think_tags(self):
        raw_output = """<think>
Let's analyze 7203.T.
Recommendation: 買い.
Confidence: 高.
Target price: 3200.
Upside: +15%.
</think>
```json
{
  "recommendation": "買い",
  "sentiment": "強気",
  "target_price_3m": 3200.0,
  "upside_3m": "+15%",
  "confidence": "高",
  "analysis_summary": "好調な業績と円安進行による収益改善が見込まれます。",
  "key_catalysts": ["円安効果", "ハイブリッド車の需要増"],
  "risk_factors": ["原材料費高騰"],
  "technical_analysis": "25日移動平均線を上抜けて上昇トレンド入り。",
  "fundamental_analysis": "PER・PBRともに割安圏にあり下値余地は限定的。",
  "latest_news_impact": "最新の決算発表で上方修正を発表し、市場の好感触を得ている。"
}
```"""
        parsed, _ = _extract_and_parse_openai_json(raw_output, pydantic_cls=StockAnalysis)
        assert parsed is not None
        assert parsed["recommendation"] == "買い"
        assert parsed["sentiment"] == "強気"
        assert parsed["target_price_3m"] == 3200.0
        assert parsed["upside_3m"] == "+15%"
        assert parsed["confidence"] == "高"

    def test_extract_and_parse_openai_json_with_aliases_and_trailing_comma(self):
        raw_output = """{
  "recommendation": "買い",
  "sentiment": "強気",
  "target_price": 2800.0,
  "upside": "+10%",
  "confidence": "高",
  "analysis_summary": "概要テスト",
  "key_catalysts": ["好決算",],
  "risk_factors": ["為替リスク",],
  "technical_analysis": "テクニカル分析",
  "fundamental_analysis": "ファンダメンタル分析",
  "latest_news_impact": "ニュース影響",
}"""
        parsed, _ = _extract_and_parse_openai_json(raw_output, pydantic_cls=StockAnalysis)
        assert parsed is not None
        # target_price alias should be normalized to target_price_3m
        assert parsed["target_price_3m"] == 2800.0
        # upside alias should be normalized to upside_3m
        assert parsed["upside_3m"] == "+10%"
        assert parsed["recommendation"] == "買い"

    def test_call_openai_chat_token_limit_boost_for_reasoning_and_pydantic(self):
        # When calling with a reasoning model like deepseek-reasoner without explicit tokens,
        # token_limit defaults to OPENAI_REASONING_MIN_TOKENS (4000)
        fake_client = MagicMock()
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "choices": [{"message": {"role": "assistant", "content": '{"test": 123}'}}],
            "usage": {"total_tokens": 50},
        }
        fake_client.post.return_value = fake_resp

        with patch("services.openai_service._get_openai_client", return_value=fake_client):
            call_openai_chat(
                api_key="sk-test-key",
                messages=[{"role": "user", "content": "hi"}],
                _model_override="deepseek-reasoner",
                use_cache=False,
            )
            # Verify payload sent to client had max_completion_tokens >= 4000
            called_payload = fake_client.post.call_args[1]["json"]
            token_key = "max_completion_tokens"
            assert token_key in called_payload
            assert called_payload[token_key] >= OPENAI_REASONING_MIN_TOKENS

            # When response_format is Pydantic, token limit is boosted to at least 3000
            call_openai_chat(
                api_key="sk-test-key",
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=500,
                response_format=StockAnalysis,
                use_cache=False,
            )
            pydantic_payload = fake_client.post.call_args[1]["json"]
            assert pydantic_payload["max_tokens"] >= 3000


class TestIssue5TestIsolationStateReset:
    """Issue 5: State reset cleanly resets AI and Realtime clients."""

    def test_state_reset_clears_openai_and_realtime_clients(self):
        from services.realtime_engine import realtime_market_engine

        # Add dummy client to realtime engine
        cid = realtime_market_engine.register_client()
        assert cid in realtime_market_engine._client_states
        assert cid in realtime_market_engine._client_events

        # Call reset
        reset_app_state_internals()

        # Engine client states and events must be cleared
        assert cid not in realtime_market_engine._client_states
        assert len(realtime_market_engine._client_events) == 0
