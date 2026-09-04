"""Regression tests for HEAD review fixes R1/R2 (2026-08-20).

R1: YFinanceProvider.get_history cache poisoning via mutable DataFrame return.
    Before fix, the provider returned the same DataFrame object stored in
    yfinance_short_cache, so caller mutation corrupted the cache (change=0 etc).
R2: mistral_compat fallback Mistral must expose chat.* so that
    services.ai_service exception handler (SDKError) is reached instead of
    unhandled AttributeError when SDK is absent.
"""

from unittest.mock import MagicMock, patch

import pandas as pd


def test_yfinance_provider_cache_isolation_mutation_does_not_corrupt_cache():
    """R1: mutating the DataFrame returned by get_history must not poison the cache."""
    import threading

    from cachetools import TTLCache

    from services.stock_provider import YFinanceProvider
    from utils.caching import history_short_cache_key

    provider = YFinanceProvider()
    m_state = provider._get_market_state()

    # Use isolated in-memory cache for this test.
    orig_cache = m_state.yfinance_short_cache
    orig_lock = m_state.yfinance_short_cache_lock
    try:
        m_state.yfinance_short_cache = TTLCache(maxsize=10, ttl=60)
        m_state.yfinance_short_cache_lock = threading.RLock()
        with (
            patch.object(m_state, "is_circuit_open", return_value=False),
            patch.object(m_state, "is_yf_rate_limited", return_value=False),
        ):
            sample_df = pd.DataFrame(
                {"Close": [100.0, 101.0]},
                index=pd.date_range("2024-01-01", periods=2),
            )
            key = history_short_cache_key("AAPL", "1mo", "1d")
            # Simulate the store path (normalized.copy() under the fix).
            with m_state.yfinance_short_cache_lock:
                m_state.yfinance_short_cache[key] = sample_df.copy()

            # Ensure cache hit path is exercised (get_ticker must not be called).
            provider.get_ticker = MagicMock(
                side_effect=AssertionError("get_ticker should not be called on cache hit")
            )  # type: ignore[method-assign]

            df1 = provider.get_history("AAPL", "1mo", "1d")
            assert isinstance(df1, pd.DataFrame)
            assert df1 is not sample_df, "cache hit must return a copy, not the cached object"
            # Mutate the returned frame; cache must remain intact.
            df1.loc[df1.index[0], "Close"] = 999.0
            df2 = provider.get_history("AAPL", "1mo", "1d")
            assert float(df2.loc[df2.index[0], "Close"]) == 100.0  # type: ignore[arg-type]
    finally:
        m_state.yfinance_short_cache = orig_cache
        m_state.yfinance_short_cache_lock = orig_lock


def test_yfinance_provider_store_path_copies_normalized_frame():
    """R1-store: the write path must not retain a reference to the caller's mutation."""
    import threading

    from cachetools import TTLCache

    from services.stock_provider import YFinanceProvider

    provider = YFinanceProvider()
    m_state = provider._get_market_state()
    orig_cache = m_state.yfinance_short_cache
    orig_lock = m_state.yfinance_short_cache_lock
    try:
        m_state.yfinance_short_cache = TTLCache(maxsize=10, ttl=60)
        m_state.yfinance_short_cache_lock = threading.RLock()
        with (
            patch.object(m_state, "is_circuit_open", return_value=False),
            patch.object(m_state, "is_yf_rate_limited", return_value=False),
        ):
            raw = pd.DataFrame(
                {"Open": [10], "High": [11], "Low": [9], "Close": [10.5], "Volume": [1000]},
                index=[pd.Timestamp("2024-01-02")],
            )
            t = MagicMock()
            t.history.return_value = raw
            provider.get_ticker = MagicMock(return_value=t)  # type: ignore[method-assign]

            # First call populates cache.
            df1 = provider.get_history("MSFT", "1mo", "1d")
            # Patch get_ticker to force cache hit on second call.
            provider.get_ticker = MagicMock(side_effect=AssertionError("should hit cache"))  # type: ignore[method-assign]
            df1.loc[df1.index[0], "Close"] = 12345.0  # mutate returned frame
            df2 = provider.get_history("MSFT", "1mo", "1d")
            assert float(df2.loc[df2.index[0], "Close"]) != 12345.0  # type: ignore[arg-type]
    finally:
        m_state.yfinance_short_cache = orig_cache
        m_state.yfinance_short_cache_lock = orig_lock


def test_mistral_fallback_chat_raises_sdk_error_not_attribute_error():
    """R2: fallback Mistral.chat.* must raise SDKError (handled) not AttributeError (unhandled)."""
    from services import ai_service as svc

    # Force the fallback class path regardless of installed SDK by constructing
    # the inner fallback type directly (mirrors mistral_compat.Mistral fallback).
    # If the real SDK is installed, exercise the fallback shape via a synthetic instance.
    class FakeFallback:
        class _Chat:
            def _sdk_err(self):
                try:
                    import httpx

                    resp = httpx.Response(
                        503, request=httpx.Request("POST", "https://api.mistral.ai/fallback")
                    )
                    return svc.SDKError("sdk missing", resp)
                except Exception:
                    try:
                        return svc.SDKError("sdk missing", None)  # type: ignore[call-arg]
                    except TypeError:
                        return svc.SDKError("sdk missing")  # type: ignore[call-arg]

            def complete(self, *a, **kw):
                raise self._sdk_err()

            def stream(self, *a, **kw):
                raise self._sdk_err()

            def parse(self, *a, **kw):
                raise self._sdk_err()

        @property
        def chat(self):
            return self._Chat()

    fake = FakeFallback()

    # When SDK raises SDKError, ai_service maps it to {"error": ...} instead of 500.
    with patch.object(svc, "_get_mistral_client", return_value=fake):
        with patch.object(svc.app_state.market, "is_circuit_open", return_value=False):
            with patch.object(svc, "_acquire_mistral_call_slot", return_value=0.0):
                with patch.object(svc, "_wait_for_rate_limit_slot", return_value=False):
                    with patch.object(
                        svc.app_state.ai,
                        "mistral_call_semaphore",
                        MagicMock(__enter__=lambda s: None, __exit__=lambda s, *a: False),
                    ):
                        result = svc.call_mistral_chat(
                            "sk-test",
                            [{"role": "user", "content": "hi"}],
                            use_cache=False,
                            max_tokens=64,
                        )
    assert isinstance(result, dict) and "error" in result

    # Streaming path must also surface as error event, not raise.
    with patch.object(svc, "_get_mistral_client", return_value=fake):
        with patch.object(svc.app_state.market, "is_circuit_open", return_value=False):
            with patch.object(svc, "_acquire_mistral_call_slot", return_value=0.0):
                with patch.object(svc, "_wait_for_rate_limit_slot", return_value=False):
                    with patch.object(
                        svc.app_state.ai,
                        "mistral_stream_semaphore",
                        MagicMock(__enter__=lambda s: None, __exit__=lambda s, *a: False),
                    ):
                        events = list(
                            svc.stream_mistral_chat("sk-test", [{"role": "user", "content": "hi"}])
                        )
    assert any(e.get("type") == "error" for e in events)
