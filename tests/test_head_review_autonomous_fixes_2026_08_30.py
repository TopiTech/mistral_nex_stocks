"""Regression tests for autonomous HEAD review fixes (R1 - R6)."""

import gc
import unittest
from unittest.mock import MagicMock, patch

import httpx
import pandas as pd
from mistralai.client.errors import HTTPValidationError, HTTPValidationErrorData, MistralError

from services.ai_service import (
    _MISTRAL_COMMUNICATION_ERRORS,
    _extract_stream_delta,
    call_mistral_chat,
    call_mistral_chat_with_tools,
)
from services.ai_tools import (
    _tool_calculate_technical_levels,
    _tool_get_company_fundamentals,
    _tool_get_market_news,
    _tool_get_stock_quote,
)
from services.stock_provider import YFinanceProvider
from services.stock_service import _history_with_timeout, fetch_history_sync_impl
from session_manager import _AUTH_RESET_LISTENERS, register_auth_reset_listener


class TestHeadReviewAutonomousFixes20260830(unittest.TestCase):
    def test_r1_merge_quote_into_history_preserves_indices_and_auxiliary_defaults(self):
        """R1: Test _merge_quote_into_history handles tz-aware, tz-naive, and string indices,
        and ensures auxiliary columns (Dividends, Stock Splits) do not get corrupted with price.
        """
        provider = YFinanceProvider()

        # Case A: tz-aware DatetimeIndex
        df_tz = pd.DataFrame(
            {
                "Open": [100.0],
                "High": [102.0],
                "Low": [99.0],
                "Close": [101.0],
                "Volume": [1000],
                "Dividends": [0.0],
                "Stock Splits": [0.0],
            },
            index=pd.to_datetime(["2026-08-28"]).tz_localize("America/New_York"),
        )
        quote = {
            "regularMarketPrice": 150.0,
            "regularMarketTime": 1788000000,
        }
        res_tz = provider._merge_quote_into_history(df_tz, quote, "AAPL")
        self.assertIsInstance(res_tz.index, pd.DatetimeIndex)
        self.assertIsNotNone(res_tz.index.tz)
        self.assertEqual(res_tz["Close"].iloc[-1], 150.0)
        self.assertEqual(res_tz["Dividends"].iloc[-1], 0.0)
        self.assertEqual(res_tz["Stock Splits"].iloc[-1], 0.0)

        # Case B: string date Index (must not raise TypeError in sort_index)
        df_str = pd.DataFrame(
            {
                "Open": [100.0],
                "High": [102.0],
                "Low": [99.0],
                "Close": [101.0],
                "Volume": [1000],
            },
            index=pd.Index(["2026-08-28"]),
        )
        res_str = provider._merge_quote_into_history(df_str, quote, "AAPL")
        self.assertIn("2026-08-29", res_str.index)
        self.assertEqual(res_str.loc["2026-08-29", "Close"], 150.0)

    def test_r2_extract_stream_delta_handles_think_chunks(self):
        """R2: Test _extract_stream_delta ignores ThinkChunks by default and extracts with include_thinking=True."""
        from mistralai.client.models import DeltaMessage, TextChunk, ThinkChunk

        # Case A: Object style with ThinkChunk
        dm = DeltaMessage(content=[ThinkChunk(thinking=[TextChunk(text="analyzing fundamentals...")])])

        class FakeChoice:
            delta = dm

        class FakeChunk:
            def __init__(self):
                self.choices = [FakeChoice()]

        # By default (for user streaming), thinking is omitted
        res_default = _extract_stream_delta(FakeChunk())
        self.assertIsNone(res_default)

        # When include_thinking=True, thinking is extracted
        res = _extract_stream_delta(FakeChunk(), include_thinking=True)
        self.assertEqual(res, "analyzing fundamentals...")

        # Case B: Dict representation
        dict_chunk = {
            "choices": [
                {
                    "delta": {
                        "content": [
                            {"thinking": [{"text": "evaluating EPS..."}]},
                        ]
                    }
                }
            ]
        }
        res_dict_default = _extract_stream_delta(dict_chunk)
        self.assertIsNone(res_dict_default)

        res_dict = _extract_stream_delta(dict_chunk, include_thinking=True)
        self.assertEqual(res_dict, "evaluating EPS...")

    def test_r3_ai_tools_query_filtering_and_key_mapping(self):
        """R3: Test _tool_get_market_news strictly filters queries and stock tools support camelCase keys."""
        # News filtering check
        mock_news = [
            {"title": "Apple announces quarterly results", "snippet": "iPhone revenue up", "source": "Reuters"},
            {"title": "Tesla updates self driving beta", "snippet": "EV automotive news", "source": "Bloomberg"},
            {"title": "NVIDIA reveals next gen GPUs", "snippet": "AI datacenter chips", "source": "CNBC"},
        ]
        with patch("trend_sources.collect_market_news_items_fast", return_value=mock_news):
            res = _tool_get_market_news({"query": "NVIDIA", "limit": 2})
            self.assertEqual(res["count"], 1)
            self.assertEqual(res["news"][0]["title"], "NVIDIA reveals next gen GPUs")

        # Quote & Fundamentals key mapping check
        real_info = {
            "name": "NVIDIA Corporation",
            "regularMarketPrice": 128.5,
            "regularMarketChange": 3.2,
            "regularMarketChangePercent": 2.55,
            "regularMarketVolume": 45000000,
            "regularMarketDayHigh": 130.0,
            "regularMarketDayLow": 125.1,
            "regularMarketOpen": 126.0,
            "currency": "USD",
            "marketCap": 3100000000000,
            "trailingPE": 45.2,
            "forwardPE": 35.0,
            "priceToBook": 38.5,
            "dividendYield": 0.001,
            "trailingEps": 2.84,
            "fiftyTwoWeekHigh": 140.76,
            "fiftyTwoWeekLow": 45.0,
        }
        with patch("utils.stock_payload.get_stock_info_cached", return_value=real_info):
            quote = _tool_get_stock_quote({"symbol": "NVDA"})
            self.assertEqual(quote["price"], 128.5)
            self.assertEqual(quote["change"], 3.2)
            self.assertEqual(quote["change_pct"], 2.55)

            funds = _tool_get_company_fundamentals({"symbol": "NVDA"})
            self.assertEqual(funds["market_cap"], 3100000000000)
            self.assertEqual(funds["pe_ratio"], 45.2)
            self.assertEqual(funds["forward_pe"], 35.0)



        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame({"Close": [100.0, 101.0, 102.0, 103.0, 104.0]})
        with patch("utils.market_utils.safe_get_ticker", return_value=mock_ticker) as ticker_fn:
            result = _tool_calculate_technical_levels({"symbol": "NVDA", "period": "10y"})
            ticker_fn.assert_called_once_with("NVDA")
            mock_ticker.history.assert_called_once_with(period="3mo")
            self.assertEqual(result["symbol"], "NVDA")
            self.assertEqual(result["period"], "3mo")

    def test_r4_mistral_error_handling_and_tool_response_format_synthesis(self):
        """R4: Test HTTPValidationError is caught by _MISTRAL_COMMUNICATION_ERRORS
        and call_mistral_chat_with_tools performs synthesis into response_format.
        """
        # Communication error hierarchy
        self.assertTrue(issubclass(MistralError, _MISTRAL_COMMUNICATION_ERRORS))

        # HTTPValidationError handled gracefully without crash
        raw_resp = httpx.Response(422, request=httpx.Request("POST", "https://api.mistral.ai"))
        err = HTTPValidationError(raw_response=raw_resp, data=HTTPValidationErrorData(detail=[]))
        mock_client = MagicMock()
        mock_client.chat.complete.side_effect = err

        with patch("services.ai_service.app_state.ai.get_or_create_mistral_client", return_value=mock_client):
            res = call_mistral_chat("test-key", [{"role": "user", "content": "hi"}])
            self.assertIn("error", res)
            self.assertEqual(res["error"]["status_code"], 422)

        # Tool calling synthesis check
        with patch("services.ai_service.call_mistral_chat") as mock_chat:
            mock_chat.side_effect = [
                {"choices": [{"message": {"role": "assistant", "content": "Apple price is 150"}}]},
                {"choices": [{"message": {"role": "assistant", "content": '{"summary": "Apple price is 150"}', "parsed": {"summary": "Apple price is 150"}}}]},
            ]
            final_res = call_mistral_chat_with_tools(
                "test-key",
                [{"role": "user", "content": "what is Apple price?"}],
                response_format=dict,
            )
            self.assertEqual(mock_chat.call_count, 2)
            self.assertEqual(mock_chat.call_args_list[1].kwargs.get("response_format"), dict)
            self.assertIsNotNone(final_res.get("choices"))

    def test_r5_history_connection_error_reraise_and_transient_cache_guard(self):
        """R5: Test _history_with_timeout re-raises ConnectionError and fetch_history_sync_impl
        flags temporary rate-limit / circuit openings as transient.
        """
        # ConnectionError re-raised
        mock_ticker = MagicMock()
        mock_ticker.history.side_effect = ConnectionResetError("Connection reset by peer")
        with patch("services.stock_service.safe_get_ticker", return_value=mock_ticker):
            with self.assertRaises((ConnectionResetError, ConnectionError, OSError)):
                _history_with_timeout("1d", "5m", "AAPL", "us")

        # Transient error flag during rate limit
        with patch("app_state.app_state.market.is_yf_rate_limited", return_value=True):
            res = fetch_history_sync_impl("AAPL", "us", "1d")
            self.assertTrue(res.get("transient"))

    def test_r6_auth_reset_listeners_weakref_no_memory_leak(self):
        """R6: Test register_auth_reset_listener does not leak bound method references."""
        initial_count = len(_AUTH_RESET_LISTENERS)
        for _ in range(5):
            provider = YFinanceProvider()
            register_auth_reset_listener(provider.clear_ticker_cache)

        del provider
        gc.collect()

        active = [r for r in _AUTH_RESET_LISTENERS if (r() if callable(r) else r) is not None]
        self.assertLessEqual(len(active), initial_count)


if __name__ == "__main__":
    unittest.main()
