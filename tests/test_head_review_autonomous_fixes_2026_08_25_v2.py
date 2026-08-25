# tests/test_head_review_autonomous_fixes_2026_08_25_v2.py
"""Regression tests for autonomous HEAD review fixes (R1 - R3)."""

from __future__ import annotations

import concurrent.futures
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import trend_sources
from schemas.ai_portfolio import AIPortfolioSaveRequest
from schemas.stocks import PortfolioUpdateRequest, StockHistoryQueryRequest
from services.fallback_provider import (
    CompositeFallbackProvider,
    MinkabuProvider,
    Nikkei225JPProvider,
    YahooJPScraperProvider,
    YahooWebScraperProvider,
)


def test_r1_trend_sources_monotonic_slot_reservation_no_rollback() -> None:
    """R1: Verify Google Trends rate limiter reserves monotonically and does not roll back future slots."""
    with trend_sources._GOOGLE_TRENDS_LOCK:
        trend_sources._GOOGLE_TRENDS_LAST_CALL = 0.0

    mock_client = MagicMock()
    mock_client.suggestions.return_value = [{"title": "Suggestion 1"}]

    recorded_delays: list[float] = []
    curr_time = 1000.2

    def fake_sleep(d: float) -> None:
        recorded_delays.append(d)

    def fake_time() -> float:
        return curr_time

    with (
        patch.object(trend_sources, "TrendReq", object()),
        patch.object(trend_sources, "_google_trends_client", return_value=mock_client),
        patch.object(trend_sources, "_GOOGLE_TRENDS_MIN_INTERVAL", 1.5),
        patch.object(trend_sources.time, "time", side_effect=fake_time),
        patch.object(trend_sources.time, "sleep", side_effect=fake_sleep),
    ):
        with trend_sources._GOOGLE_TRENDS_LOCK:
            trend_sources._GOOGLE_TRENDS_LAST_CALL = 0.0

        # 1. First call at 1000.2 runs immediately (no recent call)
        curr_time = 1000.2
        res1 = trend_sources._trend_queries_for_keyword("query1", "us")
        assert res1 == ["Suggestion 1"]
        assert trend_sources._GOOGLE_TRENDS_LAST_CALL == pytest.approx(1000.2)

        # 2. Second concurrent call arriving at 1000.4 is scheduled for 1000.2 + 1.5 = 1001.7
        curr_time = 1000.4
        res2 = trend_sources._trend_queries_for_keyword("query2", "us")
        assert res2 == ["Suggestion 1"]
        assert trend_sources._GOOGLE_TRENDS_LAST_CALL == pytest.approx(1001.7)

        # 3. Third concurrent call arriving at 1000.6 is scheduled for 1001.7 + 1.5 = 1003.2
        curr_time = 1000.6
        res3 = trend_sources._trend_queries_for_keyword("query3", "us")
        assert res3 == ["Suggestion 1"]
        assert trend_sources._GOOGLE_TRENDS_LAST_CALL == pytest.approx(1003.2)

        # 4. When a fast query completes earlier at 1001.0, _GOOGLE_TRENDS_LAST_CALL is NOT rolled back
        with trend_sources._GOOGLE_TRENDS_LOCK:
            trend_sources._GOOGLE_TRENDS_LAST_CALL = max(
                trend_sources._GOOGLE_TRENDS_LAST_CALL, 1001.0
            )
            assert trend_sources._GOOGLE_TRENDS_LAST_CALL == pytest.approx(1003.2)

    assert len(recorded_delays) == 2
    assert recorded_delays[0] == pytest.approx(1.3)
    assert recorded_delays[1] == pytest.approx(2.6)


def _verify_provider_multithreaded_sessions(p_cls: type) -> None:
    provider = p_cls()
    mock_sessions: list[MagicMock] = []
    sessions_lock = threading.Lock()

    def fake_session_factory(*args, **kwargs):
        s = MagicMock()
        with sessions_lock:
            mock_sessions.append(s)
        return s

    provider.requests = MagicMock()
    provider.requests.Session = fake_session_factory

    def worker_task():
        client, is_sess = provider._get_client()
        assert is_sess is True
        return client

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(worker_task) for _ in range(3)]
        for f in futures:
            f.result()

    assert len(mock_sessions) >= 1
    with provider._sessions_lock:
        assert len(provider._all_sessions) == len(mock_sessions)

    # Main thread closes provider
    provider.close()

    # All worker thread sessions must have been closed
    for s in mock_sessions:
        s.close.assert_called_once()

    with provider._sessions_lock:
        assert len(provider._all_sessions) == 0


def test_r2_fallback_provider_multithreaded_session_tracking_and_close() -> None:
    """R2: Verify fallback providers track sessions across multiple worker threads and close all on close()."""
    provider_classes = [
        YahooWebScraperProvider,
        YahooJPScraperProvider,
        Nikkei225JPProvider,
        MinkabuProvider,
    ]

    for p_cls in provider_classes:
        _verify_provider_multithreaded_sessions(p_cls)


def test_r2_composite_fallback_provider_close_delegates() -> None:
    """R2: Verify CompositeFallbackProvider.close() closes all underlying providers."""
    composite = CompositeFallbackProvider()
    composite.yahoo_web = MagicMock()
    composite.yahoo_jp = MagicMock()
    composite.nikkei225jp = MagicMock()
    composite.minkabu = MagicMock()

    composite.close()

    composite.yahoo_web.close.assert_called_once()
    composite.yahoo_jp.close.assert_called_once()
    composite.nikkei225jp.close.assert_called_once()
    composite.minkabu.close.assert_called_once()


def test_r3_schema_contract_alignments() -> None:
    """R3: Verify AIPortfolioSaveRequest, StockHistoryQueryRequest defaults, and PortfolioUpdateRequest docstring."""
    # 1. AIPortfolioSaveRequest handles nested portfolio payload without top-level theme
    payload = {
        "portfolio": {
            "title": "AI & Robotics",
            "theme": "ai",
            "items": [
                {"symbol": "NVDA", "market": "us", "target_price": 120.0, "weight_pct": 50.0},
                {"symbol": "MSFT", "market": "us", "target_price": 400.0, "weight_pct": 50.0},
            ],
        }
    }
    validated = AIPortfolioSaveRequest.model_validate(payload)
    assert validated.portfolio["title"] == "AI & Robotics"
    assert validated.theme is None

    # Top-level theme is also valid if provided
    payload_with_theme = {
        "theme": "custom_tech",
        "name": "Custom Tech",
        "portfolio": {"title": "Custom Tech", "items": []},
    }
    validated2 = AIPortfolioSaveRequest.model_validate(payload_with_theme)
    assert validated2.theme == "custom_tech"
    assert validated2.name == "Custom Tech"

    # 2. StockHistoryQueryRequest defaults period to '3mo'
    query_req = StockHistoryQueryRequest(symbol="AAPL")
    assert query_req.period == "3mo"
    assert query_req.market == "us"

    # 3. PortfolioUpdateRequest docstring references /api/stocks/portfolio
    assert "/api/stocks/portfolio" in (PortfolioUpdateRequest.__doc__ or "")
