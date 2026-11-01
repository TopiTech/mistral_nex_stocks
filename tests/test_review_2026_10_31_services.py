"""Regression tests for the 2026-10-31 services-layer review (SVC-01..SVC-23).

Every FIXED item has its original trigger, an edge/abnormal case, and a
retained-valid-behavior check. NO ACTION items get a documented spot-check of
the pinned behavior (see scratch/review/services_resolution.md for triage).

All tests are offline: provider/client/network layers are mocked.
"""

import json
import logging
import threading
import time
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from pydantic import ValidationError

from app_state import app_state
from constants import CHART_IMAGE_MAX_IMAGE_DATA_CHARS
from services.ai_portfolio_service import generate_ai_portfolio_by_theme
from services.ai_service import (
    _release_mistral_call_slot,
    call_mistral_chat,
    call_mistral_chat_with_tools,
    generate_ai_technical_lines,
    stream_mistral_chat,
)
from services.ai_tools import _tool_get_stock_quote
from services.fallback_provider import CompositeFallbackProvider, MinkabuProvider
from services.openai_service import call_openai_chat, call_openai_chat_with_tools
from services.realtime.engine import RealtimeMarketEngine
from services.realtime.scrapers import MinkabuScraper, SBISecuritiesScraper
from services.realtime.utils import _get_yfinance_previous_close, is_jp_market_open
from services.search_service import _get_market_trending_titles
from services.stock.provider import YFinanceProvider

JST = ZoneInfo("Asia/Tokyo")

REPAIRED_TECHNICAL_PAYLOAD = {
    "summary": "修復済みサマリ",
    "trend_bias": "Bullish",
    "lines": [
        {
            "id": "line_1",
            "type": "support",
            "label": "サポート",
            "start_date": "2026-01-01",
            "start_price": 100.0,
            "end_date": "2026-02-01",
            "end_price": 110.0,
        }
    ],
}

# generate_ai_technical_lines rejects an empty history before reaching the
# extraction/repair block (services/ai_service.py:1613-1614).
SAMPLE_TECHNICAL_HISTORY = [{"x": 1719792000000, "o": 100, "h": 101, "l": 99, "c": 100.5}]


@pytest.fixture
def mistral_pacing_state():
    """Snapshot/restore the shared Mistral pacing timestamps."""
    saved = (app_state.ai.mistral_last_call_ts, app_state.ai.mistral_next_allowed_ts)
    yield app_state.ai
    app_state.ai.mistral_last_call_ts, app_state.ai.mistral_next_allowed_ts = saved


# ---------------------------------------------------------------------------
# SVC-01 — LLM JSON repair must fire on empty extraction, not only on
# json.loads exceptions.
# ---------------------------------------------------------------------------


def test_svc01_empty_extraction_triggers_llm_repair():
    response = {"choices": [{"message": {"content": "not json at all"}}]}
    with (
        patch("services.ai_service.call_mistral_chat", return_value=response),
        patch("services.ai_service.extract_json_payload", return_value=None) as mock_extract,
        patch(
            "services.ai_service.repair_technical_lines_json_with_llm",
            return_value=(REPAIRED_TECHNICAL_PAYLOAD, True),
        ) as mock_repair,
    ):
        result = generate_ai_technical_lines("k", "TEST", "us", "3mo", list(SAMPLE_TECHNICAL_HISTORY))

    mock_extract.assert_called_once()
    mock_repair.assert_called_once()
    assert result["summary"] == "修復済みサマリ"
    assert result["trend_bias"] == "Bullish"
    assert len(result["lines"]) == 1  # degraded fallback would be []
    assert "JSONパースに一部不備" not in result["summary"]


def test_svc01_parse_error_still_repairs_exactly_once():
    response = {"choices": [{"message": {"content": "{broken json"}}]}
    with (
        patch("services.ai_service.call_mistral_chat", return_value=response),
        patch("services.ai_service.extract_json_payload", return_value="{broken json"),
        patch(
            "services.ai_service.repair_technical_lines_json_with_llm",
            return_value=(REPAIRED_TECHNICAL_PAYLOAD, True),
        ) as mock_repair,
    ):
        result = generate_ai_technical_lines("k", "TEST2", "us", "3mo", list(SAMPLE_TECHNICAL_HISTORY))

    assert mock_repair.call_count == 1
    assert result["summary"] == "修復済みサマリ"
    assert len(result["lines"]) == 1


# ---------------------------------------------------------------------------
# SVC-02 — abort paths must roll the unused pacing reservation back.
# ---------------------------------------------------------------------------


def test_svc02_rate_limited_abort_rolls_back_reservation(mistral_pacing_state):
    now = time.time()
    app_state.ai.mistral_next_allowed_ts = now + 31.0  # > 30 s cap
    app_state.ai.mistral_last_call_ts = now + 70.0  # dominates the wait
    client = MagicMock()
    with (
        patch("services.ai_service.is_openai_provider", return_value=False),
        patch.object(app_state.market, "is_circuit_open", return_value=False),
        patch.object(app_state.market, "get_circuit_state", return_value={"status": "CLOSED"}),
        patch("services.ai_service._get_mistral_client", return_value=client),
        patch("services.ai_service.MISTRAL_MIN_INTERVAL_SEC", 0.0),
        patch("services.ai_service.MISTRAL_JITTER_FACTOR", 0.0),
        patch("services.ai_service._MISTRAL_RATE_LIMIT_MAX_WAIT_SEC", 0.01),
    ):
        result = call_mistral_chat("test-key-123", [{"role": "user", "content": "x"}])

    assert result["error"]["status_code"] == 429
    client.chat.complete.assert_not_called()
    # The phantom reservation (now+71) must be rolled back to at most the
    # authoritative cooldown — without the fix it stays ~now+71.
    assert app_state.ai.mistral_last_call_ts <= app_state.ai.mistral_next_allowed_ts + 1.0


def test_svc02_shutdown_abort_rolls_back_reservation(mistral_pacing_state):
    now = time.time()
    app_state.ai.mistral_next_allowed_ts = now + 5.0
    app_state.ai.mistral_last_call_ts = now + 40.0
    client = MagicMock()
    shutdown_event = MagicMock()
    shutdown_event.wait.return_value = True
    with (
        patch("services.ai_service.is_openai_provider", return_value=False),
        patch.object(app_state.market, "is_circuit_open", return_value=False),
        patch.object(app_state.market, "get_circuit_state", return_value={"status": "CLOSED"}),
        patch("services.ai_service._get_mistral_client", return_value=client),
        patch("services.ai_service.MISTRAL_MIN_INTERVAL_SEC", 0.0),
        patch("services.ai_service.MISTRAL_JITTER_FACTOR", 0.0),
        patch("services.ai_service._MISTRAL_RATE_LIMIT_MAX_WAIT_SEC", 0.01),
        patch.object(app_state.execution, "shutdown_event", shutdown_event),
    ):
        result = call_mistral_chat("test-key-123", [{"role": "user", "content": "x"}])

    assert result["error"]["status_code"] == 503
    client.chat.complete.assert_not_called()
    assert app_state.ai.mistral_last_call_ts <= app_state.ai.mistral_next_allowed_ts + 1.0


def test_svc02_stream_rate_limited_abort_rolls_back(mistral_pacing_state):
    now = time.time()
    app_state.ai.mistral_next_allowed_ts = now + 5.0
    app_state.ai.mistral_last_call_ts = now + 40.0
    client = MagicMock()
    with (
        patch("services.ai_service.is_openai_provider", return_value=False),
        patch.object(app_state.market, "is_circuit_open", return_value=False),
        patch.object(app_state.market, "get_circuit_state", return_value={"status": "CLOSED"}),
        patch("services.ai_service._get_mistral_client", return_value=client),
        patch("services.ai_service.MISTRAL_MIN_INTERVAL_SEC", 0.0),
        patch("services.ai_service.MISTRAL_JITTER_FACTOR", 0.0),
        patch("services.ai_service._MISTRAL_RATE_LIMIT_MAX_WAIT_SEC", 0.01),
    ):
        events = list(stream_mistral_chat("test-key-123", [{"role": "user", "content": "x"}]))

    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert events[0]["status_code"] == 429
    client.chat.complete.assert_not_called()
    assert app_state.ai.mistral_last_call_ts <= app_state.ai.mistral_next_allowed_ts + 1.0


def test_svc02_release_slot_never_advances_timestamp(mistral_pacing_state):
    now = time.time()
    app_state.ai.mistral_next_allowed_ts = now + 20.0
    app_state.ai.mistral_last_call_ts = now + 99.0
    _release_mistral_call_slot()
    # Rollback keeps the authoritative cooldown and never advances the clock.
    assert app_state.ai.mistral_last_call_ts == pytest.approx(now + 20.0, abs=1.0)
    before = app_state.ai.mistral_last_call_ts
    _release_mistral_call_slot()
    assert app_state.ai.mistral_last_call_ts <= before + 1e-6


# ---------------------------------------------------------------------------
# SVC-03 — SDK responses without model_dump surface a typed 502, never a
# fabricated {"choices": []} success.
# ---------------------------------------------------------------------------


def test_svc03_response_without_model_dump_returns_typed_502(caplog):
    client = MagicMock()
    client.chat.complete.return_value = object()  # no model_dump attribute
    with (
        caplog.at_level(logging.WARNING, logger="services.ai_service"),
        patch("services.ai_service.is_openai_provider", return_value=False),
        patch.object(app_state.market, "is_circuit_open", return_value=False),
        patch.object(app_state.market, "get_circuit_state", return_value={"status": "CLOSED"}),
        patch("services.ai_service._get_mistral_client", return_value=client),
        patch("services.ai_service.MISTRAL_MIN_INTERVAL_SEC", 0.0),
    ):
        result = call_mistral_chat(
            "test-key-123",
            [{"role": "user", "content": "svc03"}],
            use_cache=False,
        )

    assert result["error"]["status_code"] == 502
    assert "unsupported response object" in result["error"]["message"]
    assert "missing model_dump" in caplog.text
    # The fabricated empty-success envelope must never come back.
    assert "choices" not in result


def test_svc03_response_with_model_dump_still_returns_data():
    client = MagicMock()
    data = {"choices": [{"message": {"content": "ok-from-sdk"}}], "usage": None}
    client.chat.complete.return_value = _FakeSDKResponse(data)
    with (
        patch("services.ai_service.is_openai_provider", return_value=False),
        patch.object(app_state.market, "is_circuit_open", return_value=False),
        patch.object(app_state.market, "get_circuit_state", return_value={"status": "CLOSED"}),
        patch("services.ai_service._get_mistral_client", return_value=client),
        patch("services.ai_service.MISTRAL_MIN_INTERVAL_SEC", 0.0),
    ):
        result = call_mistral_chat(
            "test-key-123",
            [{"role": "user", "content": "svc03-valid"}],
            use_cache=False,
        )

    assert result["choices"][0]["message"]["content"] == "ok-from-sdk"


class _FakeSDKResponse:
    """Minimal SDK-shaped response exposing ``model_dump``."""

    def __init__(self, data: dict):
        self._data = data

    def model_dump(self) -> dict:
        return dict(self._data)


# ---------------------------------------------------------------------------
# SVC-04 — the reasoning-effort fallback must anchor on a whole-word 400 /
# structured status, not on any substring "400".
# ---------------------------------------------------------------------------


def test_svc04_transient_400_substring_does_not_trigger_reasoning_fallback(caplog):
    exc = ConnectionError("upstream timeout after 1400 ms; parameter buffer full")
    client = MagicMock()
    client.chat.complete.side_effect = exc
    with (
        caplog.at_level(logging.WARNING, logger="services.ai_service"),
        patch("services.ai_service.is_openai_provider", return_value=False),
        patch.object(app_state.market, "is_circuit_open", return_value=False),
        patch.object(app_state.market, "get_circuit_state", return_value={"status": "CLOSED"}),
        patch("services.ai_service._get_mistral_client", return_value=client),
        patch("services.ai_service.MISTRAL_MIN_INTERVAL_SEC", 0.0),
        patch("services.ai_service._resolve_reasoning_effort", return_value="medium"),
    ):
        result = call_mistral_chat(
            "test-key-123",
            [{"role": "user", "content": "svc04-transient"}],
            reasoning_effort="medium",
            use_cache=False,
        )

    # A transient 503-class error whose text contains "1400" must not be
    # reclassified as a reasoning-parameter rejection (no silent full retry).
    assert client.chat.complete.call_count == 1
    assert "rejected reasoning_effort" not in caplog.text
    assert result["error"]["status_code"] == 503


def test_svc04_genuine_400_reasoning_rejection_still_retries(caplog):
    rejected = ConnectionError("400: reasoning_effort parameter rejected by model")
    rejected.status_code = 400
    ok = _FakeSDKResponse({"choices": [{"message": {"content": "retried-ok"}}]})
    client = MagicMock()
    client.chat.complete.side_effect = [rejected, ok]
    with (
        caplog.at_level(logging.WARNING, logger="services.ai_service"),
        patch("services.ai_service.is_openai_provider", return_value=False),
        patch.object(app_state.market, "is_circuit_open", return_value=False),
        patch.object(app_state.market, "get_circuit_state", return_value={"status": "CLOSED"}),
        patch("services.ai_service._get_mistral_client", return_value=client),
        patch("services.ai_service.MISTRAL_MIN_INTERVAL_SEC", 0.0),
        patch("services.ai_service._resolve_reasoning_effort", return_value="medium"),
    ):
        result = call_mistral_chat(
            "test-key-123",
            [{"role": "user", "content": "svc04-genuine"}],
            reasoning_effort="medium",
            use_cache=False,
        )

    assert client.chat.complete.call_count == 2
    assert "rejected reasoning_effort" in caplog.text
    assert result["choices"][0]["message"]["content"] == "retried-ok"


# ---------------------------------------------------------------------------
# SVC-05 / SVC-07 — non-list or empty ``messages`` must fail fast with a typed
# error instead of running an agent loop over an empty conversation.
# ---------------------------------------------------------------------------


def test_svc05_non_list_messages_error_without_model_call():
    with patch("services.ai_service.call_mistral_chat") as mock_chat:
        result = call_mistral_chat_with_tools("k", {"role": "user"})

    assert result["error"]["status_code"] == 502
    assert "messages" in result["error"]["message"]
    mock_chat.assert_not_called()


def test_svc05_empty_messages_error_without_model_call():
    with patch("services.ai_service.call_mistral_chat") as mock_chat:
        result = call_mistral_chat_with_tools("k", [])

    assert result["error"]["status_code"] == 502
    mock_chat.assert_not_called()


def test_svc05_valid_messages_still_run_agent_loop():
    final = {"choices": [{"message": {"content": "loop-done"}}]}
    with (
        patch("services.ai_service.is_openai_provider", return_value=False),
        patch("services.ai_service.call_mistral_chat", return_value=final) as mock_chat,
    ):
        result = call_mistral_chat_with_tools(
            "k", [{"role": "user", "content": "svc05-valid"}]
        )

    mock_chat.assert_called_once()
    assert result["choices"][0]["message"]["content"] == "loop-done"
    assert "error" not in result


def test_svc07_non_list_messages_error_without_model_call():
    with patch("services.openai_service.call_openai_chat") as mock_chat:
        result = call_openai_chat_with_tools("sk-test", {"role": "user"})

    assert result["error"]["status_code"] == 502
    assert "messages" in result["error"]["message"]
    mock_chat.assert_not_called()


def test_svc07_empty_messages_error_without_model_call():
    with patch("services.openai_service.call_openai_chat") as mock_chat:
        result = call_openai_chat_with_tools("sk-test", [])

    assert result["error"]["status_code"] == 502
    mock_chat.assert_not_called()


def test_svc07_valid_messages_still_run_agent_loop():
    final = {"choices": [{"message": {"content": "openai-loop-done"}}]}
    with patch(
        "services.openai_service.call_openai_chat", return_value=final
    ) as mock_chat:
        result = call_openai_chat_with_tools(
            "sk-test", [{"role": "user", "content": "svc07-valid"}]
        )

    mock_chat.assert_called_once()
    assert result["choices"][0]["message"]["content"] == "openai-loop-done"
    assert "error" not in result


# ---------------------------------------------------------------------------
# SVC-08 — malformed max_tool_iterations must be normalized like the Mistral
# twin instead of raising a raw ValueError/TypeError.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_iterations", ["abc", None, 10_000, -3])
def test_svc08_malformed_max_tool_iterations_normalized(bad_iterations):
    final = {"choices": [{"message": {"content": "normalized-ok"}}]}
    with patch(
        "services.openai_service.call_openai_chat", return_value=final
    ) as mock_chat:
        result = call_openai_chat_with_tools(
            "sk-test",
            [{"role": "user", "content": "svc08"}],
            max_tool_iterations=bad_iterations,
        )

    assert result["choices"][0]["message"]["content"] == "normalized-ok"
    assert "error" not in result
    assert 1 <= mock_chat.call_count <= 8


# ---------------------------------------------------------------------------
# SVC-09 — a 200 with a non-JSON / non-dict body must return the typed 502.
# Circuit accounting: deliberately NO failure is recorded — the pre-existing
# pinned contract tests/test_code_review_u14_circuit_and_scraper_fixes.py::
# test_call_openai_chat_invalid_json_body_does_not_trip_circuit requires that
# a garbage 200 body never opens the circuit (U14/K-10 decision). The typed
# 502 surfacing is the part of SVC-09 that is compatible with that pin.
# ---------------------------------------------------------------------------


def _openai_call_env(resp: MagicMock):
    """Common call_openai_chat doubles: closed circuit, spied accounting, stub client."""
    return (
        patch.object(app_state.market, "is_circuit_open", return_value=False),
        patch.object(app_state.market, "get_circuit_state", return_value={"status": "CLOSED"}),
        patch.object(app_state.market, "report_circuit_result"),
        patch(
            "services.openai_service._get_openai_client",
            return_value=MagicMock(post=MagicMock(return_value=resp)),
        ),
    )


def test_svc09_invalid_json_200_body_returns_502_without_tripping_circuit():
    resp = MagicMock()
    resp.status_code = 200
    resp.json.side_effect = json.JSONDecodeError("Expecting value", "<html>", 0)
    p_open, p_state, p_report, p_client = _openai_call_env(resp)
    with p_open, p_state, p_client, p_report as report:
        result = call_openai_chat(
            "sk-test",
            [{"role": "user", "content": "svc09-invalid-json"}],
            use_cache=False,
        )

    assert result["error"]["status_code"] == 502
    assert result["error"]["message"] == "Invalid JSON response from OpenAI provider"
    # Pinned U14 contract: no circuit failure may be recorded for this body.
    for call_args in report.call_args_list:
        assert call_args[1].get("success") is not False


def test_svc09_invalid_json_body_releases_claimed_half_open_probe():
    """A claimed recovery probe must not stay stuck on a malformed 200 body."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.side_effect = json.JSONDecodeError("Expecting value", "<html>", 0)
    p_open, p_state, p_report, p_client = _openai_call_env(resp)
    with (
        p_open,
        p_state,
        p_client,
        p_report as report,
        patch.object(app_state.market, "get_circuit_state", return_value={"status": "HALF_OPEN"}),
        patch.object(app_state.market, "try_claim_circuit_probe", return_value=True) as claim,
        patch.object(app_state.market, "release_circuit_probe") as release,
    ):
        result = call_openai_chat(
            "sk-test",
            [{"role": "user", "content": "svc09-probe-release"}],
            use_cache=False,
        )

    assert result["error"]["status_code"] == 502
    claim.assert_called_once()
    release.assert_called_once_with("openai_compatible")
    for call_args in report.call_args_list:
        assert call_args[1].get("success") is not False


def test_svc09_non_dict_json_200_body_returns_502_without_tripping_circuit():
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = []  # valid JSON, wrong shape
    p_open, p_state, p_report, p_client = _openai_call_env(resp)
    with p_open, p_state, p_client, p_report as report:
        result = call_openai_chat(
            "sk-test",
            [{"role": "user", "content": "svc09-non-dict"}],
            use_cache=False,
        )

    assert result["error"]["status_code"] == 502
    for call_args in report.call_args_list:
        assert call_args[1].get("success") is not False


def test_svc09_valid_json_200_body_reports_circuit_success():
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"choices": [{"message": {"content": "valid"}}]}
    p_open, p_state, p_report, p_client = _openai_call_env(resp)
    with p_open, p_state, p_client, p_report as report:
        result = call_openai_chat(
            "sk-test",
            [{"role": "user", "content": "svc09-valid"}],
            use_cache=False,
        )

    assert result["choices"][0]["message"]["content"] == "valid"
    report.assert_called_once_with("openai_compatible", success=True)


# ---------------------------------------------------------------------------
# SVC-10 / SVC-17 — Minkabu price extraction must never match an arbitrary
# "...円" figure (手数料290円); class-anchored and labelled contexts still work.
# ---------------------------------------------------------------------------


def test_svc10_fee_only_html_returns_none():
    provider = MinkabuProvider()
    resp = MagicMock()
    resp.status_code = 200
    resp.text = "<p>売買手数料290円</p><p>月額1,980円</p>"
    client = MagicMock()
    client.get.return_value = resp
    with patch.object(provider, "_get_client", return_value=(client, True)):
        assert provider.get_latest_quote("7203.T") is None


def test_svc10_class_anchored_and_labelled_prices_still_parse():
    provider = MinkabuProvider()
    client = MagicMock()
    with patch.object(provider, "_get_client", return_value=(client, True)):
        resp = MagicMock()
        resp.status_code = 200

        resp.text = '<div class="stock_price">1,234.5</div>'
        client.get.return_value = resp
        quote = provider.get_latest_quote("7203.T")
        assert quote is not None
        assert quote["regularMarketPrice"] == 1234.5
        assert quote["regularMarketPreviousClose"] is None  # never faked

        # Label-anchored fallback (markup drift away from the class selector).
        resp.text = "<td>現在値</td><td>5,678 円</td>"
        quote = provider.get_latest_quote("7203.T")
        assert quote is not None
        assert quote["regularMarketPrice"] == 5678.0


def test_svc17_scraper_fee_only_html_returns_none():
    scraper = MinkabuScraper()
    resp = MagicMock()
    resp.status_code = 200
    resp.text = "<p>売買手数料290円</p>"
    with patch.object(scraper.session, "get", return_value=resp):
        assert scraper.fetch_quote("7203.T") is None


def test_svc17_scraper_class_anchored_price_still_parses():
    scraper = MinkabuScraper()
    resp = MagicMock()
    resp.status_code = 200
    resp.text = '<div class="stock_price">2,983.5</div>'
    with patch.object(scraper.session, "get", return_value=resp):
        payload = scraper.fetch_quote("7203.T")
    assert payload is not None
    assert payload["price"] == 2983.5
    assert payload["source"] == "minkabu"


# ---------------------------------------------------------------------------
# SVC-18 — the Minkabu PTS fallback serves the regular-session page price; it
# must carry an explicit non-live marker instead of masquerading as a live PTS
# print.
# ---------------------------------------------------------------------------


def test_svc18_minkabu_pts_payload_marked_regular_session():
    scraper = MinkabuScraper()
    regular_payload = {
        "symbol": "7203.T",
        "price": 2983.5,
        "change": 0.0,
        "change_percent": 0.0,
        "source": "minkabu",
        "updated_at": 123.0,
    }
    with patch.object(scraper, "fetch_quote", return_value=dict(regular_payload)):
        pts = scraper.fetch_pts_quote("7203.T")

    assert pts is not None
    assert pts["source"] == "minkabu_pts"
    assert pts["pts"] is True
    assert pts["price"] == 2983.5
    # The regular-session provenance must be explicit for consumers.
    assert pts["pts_price_source"] == "regular_session"


def test_svc18_minkabu_pts_failure_still_returns_none():
    scraper = MinkabuScraper()
    with patch.object(scraper, "fetch_quote", return_value=None):
        assert scraper.fetch_pts_quote("7203.T") is None


# ---------------------------------------------------------------------------
# SVC-12 — the quote tool's last-resort ticker.history fallback must hold
# yfinance_history_semaphore (same invariant as _tool_calculate_technical_levels).
# ---------------------------------------------------------------------------


def _quote_tool_env(ticker: MagicMock):
    return (
        patch("utils.stock_payload.get_stock_info_cached", return_value={"name": "TestCorp"}),
        patch(
            "services.realtime_engine.realtime_market_engine.get_market_snapshot",
            return_value={},
        ),
        patch.object(app_state.payload_disk_cache, "get", return_value=None),
        patch("utils.market_utils.safe_get_ticker", return_value=ticker),
    )


def test_svc12_semaphore_exhausted_blocks_history_call():
    ticker = MagicMock()
    ticker.fast_info = None
    sem = threading.BoundedSemaphore(1)
    assert sem.acquire(blocking=False)  # exhaust the only slot
    p_info, p_snap, p_cache, p_ticker = _quote_tool_env(ticker)
    try:
        with (
            p_info,
            p_snap,
            p_cache,
            p_ticker,
            patch.object(app_state.market, "yfinance_history_semaphore", sem),
            patch("constants.HISTORY_SEMAPHORE_TIMEOUT", 0.05),
        ):
            result = _tool_get_stock_quote({"symbol": "TESTSEM", "market": "us"})

        assert result["status"] == "データ取得不可"
        ticker.history.assert_not_called()  # no unthrottled request
    finally:
        sem.release()


def test_svc12_semaphore_free_runs_history_and_releases():
    ticker = MagicMock()
    ticker.fast_info = None
    ticker.history.return_value = pd.DataFrame(
        {"Close": [150.0]}, index=pd.to_datetime(["2026-10-30"])
    )
    sem = threading.BoundedSemaphore(1)
    p_info, p_snap, p_cache, p_ticker = _quote_tool_env(ticker)
    with (
        p_info,
        p_snap,
        p_cache,
        p_ticker,
        patch.object(app_state.market, "yfinance_history_semaphore", sem),
        patch("constants.HISTORY_SEMAPHORE_TIMEOUT", 0.05),
    ):
        result = _tool_get_stock_quote({"symbol": "TESTFREE", "market": "us"})

    assert result["price"] == 150.0
    ticker.history.assert_called_once_with(period="1d")
    # BoundedSemaphore raises on unbalanced release — acquiring proves the
    # slot was returned exactly once.
    assert sem.acquire(blocking=False)
    sem.release()


# ---------------------------------------------------------------------------
# SVC-13 — the composite fallback chain must respect an aggregate wall-clock
# budget instead of stacking every per-hop timeout on the caller's thread.
# ---------------------------------------------------------------------------


def test_svc13_budget_exhausted_skips_later_hops():
    composite = CompositeFallbackProvider()

    def _slow_miss(_symbol):
        time.sleep(0.08)

    composite.alpha_vantage.get_latest_quote = MagicMock(side_effect=_slow_miss)
    composite.yahoo_jp.get_latest_quote = MagicMock(return_value=None)
    composite.nikkei225jp.get_latest_quote = MagicMock(return_value=None)
    composite.minkabu.get_latest_quote = MagicMock(return_value=None)

    started = time.monotonic()
    with patch("services.fallback_provider._COMPOSITE_QUOTE_BUDGET_SEC", 0.05):
        result = composite.get_latest_quote("7203.T")
    elapsed = time.monotonic() - started

    assert result is None
    # First hop always runs; every later hop is skipped once the budget is gone.
    composite.alpha_vantage.get_latest_quote.assert_called_once()
    composite.yahoo_jp.get_latest_quote.assert_not_called()
    composite.nikkei225jp.get_latest_quote.assert_not_called()
    composite.minkabu.get_latest_quote.assert_not_called()
    assert elapsed < 2.0


def test_svc13_hops_run_in_priority_order_within_budget():
    composite = CompositeFallbackProvider()
    composite.alpha_vantage.get_latest_quote = MagicMock(return_value=None)
    composite.yahoo_jp.get_latest_quote = MagicMock(return_value=None)
    composite.nikkei225jp.get_latest_quote = MagicMock(return_value=None)
    composite.minkabu.get_latest_quote = MagicMock(
        return_value={"symbol": "7203.T", "regularMarketPrice": 100.0}
    )

    # Default budget: fast misses keep the documented JP priority chain intact.
    result = composite.get_latest_quote("7203.T")

    assert result is not None
    assert result["source"] == "minkabu"
    composite.yahoo_jp.get_latest_quote.assert_called_once_with("7203.T")
    composite.nikkei225jp.get_latest_quote.assert_called_once_with("7203.T")
    composite.minkabu.get_latest_quote.assert_called_once_with("7203.T")


# ---------------------------------------------------------------------------
# SVC-14 — non-str theme must raise a typed ValueError, not AttributeError.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_theme", [None, 123, "", "   ", ("tuple",)])
def test_svc14_non_str_theme_raises_value_error(bad_theme):
    with pytest.raises(ValueError, match="non-empty string"):
        generate_ai_portfolio_by_theme(bad_theme)


def test_svc14_valid_theme_returns_saved_portfolio_without_regeneration():
    saved = {"id": "custom-1", "theme": "テーマ-回帰-テスト", "items": []}
    with patch(
        "services.ai_portfolio_service._find_saved_ai_portfolio", return_value=saved
    ) as mock_find:
        result = generate_ai_portfolio_by_theme("テーマ-回帰-テスト")

    mock_find.assert_called_once()
    assert result is saved  # retained: saved portfolios short-circuit, no network


# ---------------------------------------------------------------------------
# SVC-15 — get_major_holders must return a flat attribute→value mapping for
# both yfinance frame shapes, never nested column dicts.
# ---------------------------------------------------------------------------


def test_svc15_attribute_indexed_frame_flattens():
    provider = YFinanceProvider()
    ticker = MagicMock()
    ticker.get_major_holders.return_value = pd.DataFrame(
        {"Value": [0.04, 0.61, 0.35]},
        index=["insidersPercentHeld", "institutionsPercentHeld", "fundsPercentHeld"],
    )
    with patch.object(provider, "get_ticker", return_value=ticker):
        result = provider.get_major_holders("AAPL")

    assert result == {
        "insidersPercentHeld": 0.04,
        "institutionsPercentHeld": 0.61,
        "fundsPercentHeld": 0.35,
    }
    # No nested column dicts (the pre-fix shape was {"Value": {...}}).
    assert all(not isinstance(v, dict) for v in result.values())


def test_svc15_legacy_label_value_frame_flattens():
    provider = YFinanceProvider()
    ticker = MagicMock()
    ticker.get_major_holders.return_value = pd.DataFrame(
        {"Label": ["insidersPercentHeld", "fundsPercentHeld"], "Value": [0.04, 0.35]}
    )
    with patch.object(provider, "get_ticker", return_value=ticker):
        result = provider.get_major_holders("AAPL")

    assert result == {"insidersPercentHeld": 0.04, "fundsPercentHeld": 0.35}


def test_svc15_ticker_error_still_returns_empty_dict():
    provider = YFinanceProvider()
    ticker = MagicMock()
    ticker.get_major_holders.side_effect = RuntimeError("boom")
    with patch.object(provider, "get_ticker", return_value=ticker):
        assert provider.get_major_holders("AAPL") == {}


# ---------------------------------------------------------------------------
# SVC-20 — snapshots must copy per-symbol payloads; consumer mutations must not
# corrupt the live store.
# ---------------------------------------------------------------------------


def test_svc20_market_snapshot_mutations_isolated_from_store():
    engine = RealtimeMarketEngine()
    engine.market_store["SVC20.T"] = {"symbol": "SVC20.T", "price": 100.0, "change": 1.0}

    snapshot = engine.get_market_snapshot()
    snapshot["SVC20.T"]["price"] = 999.0  # consumer mutation
    snapshot["SVC20.T"]["injected"] = True

    with engine.store_lock:
        live = engine.market_store["SVC20.T"]
    assert live["price"] == 100.0
    assert "injected" not in live
    # Fresh snapshot reflects the untouched store values.
    assert engine.get_market_snapshot()["SVC20.T"]["price"] == 100.0


def test_svc20_pts_snapshot_mutations_isolated_from_store():
    engine = RealtimeMarketEngine()
    engine.pts_store["SVC20PTS.T"] = {"symbol": "SVC20PTS.T", "price": 500.0, "pts": True}

    snapshot = engine.get_pts_snapshot()
    snapshot["SVC20PTS.T"]["price"] = -1.0

    with engine.store_lock:
        live = engine.pts_store["SVC20PTS.T"]
    assert live["price"] == 500.0
    assert engine.get_pts_snapshot()["SVC20PTS.T"]["price"] == 500.0


# ---------------------------------------------------------------------------
# SVC-21 — the producer path must not block on sse_data_lock held by the SSE
# serializer: skip enrichment for the tick (drop-if-busy) instead of stalling.
# ---------------------------------------------------------------------------


def test_svc21_producer_update_returns_quickly_while_sse_lock_held():
    engine = RealtimeMarketEngine()
    holder_holds = threading.Event()
    holder_release = threading.Event()

    def _holder() -> None:
        with app_state.cache.sse_data_lock:
            holder_holds.set()
            holder_release.wait(timeout=5.0)

    holder = threading.Thread(target=_holder, daemon=True)
    holder.start()
    assert holder_holds.wait(timeout=2.0)
    try:
        started = time.monotonic()
        engine._handle_producer_update(
            {"symbol": "SVC21.T", "price": 1000.0, "source": "tradingview"}
        )
        elapsed = time.monotonic() - started

        # Pre-fix this blocked on the RLock until the holder released (~5 s).
        assert elapsed < 2.0
        with engine.store_lock:
            stored = engine.market_store.get("SVC21.T")
        assert stored is not None
        assert stored["price"] == 1000.0
    finally:
        holder_release.set()
        holder.join(timeout=5.0)


def test_svc21_previous_close_lookup_drops_while_lock_busy():
    holder_holds = threading.Event()
    holder_release = threading.Event()

    def _holder() -> None:
        with app_state.cache.sse_data_lock:
            holder_holds.set()
            holder_release.wait(timeout=5.0)

    holder = threading.Thread(target=_holder, daemon=True)
    holder.start()
    assert holder_holds.wait(timeout=2.0)
    try:
        started = time.monotonic()
        result = _get_yfinance_previous_close("SVC21B.T")
        elapsed = time.monotonic() - started
        assert result is None
        assert elapsed < 2.0
    finally:
        holder_release.set()
        holder.join(timeout=5.0)


def test_svc21_previous_close_lookup_still_enriches_when_lock_free():
    with patch(
        "utils.stock_payload.get_stock_previous_close", return_value=500.0
    ) as mock_lookup:
        result = _get_yfinance_previous_close("SVC21C.T")

    assert result == 500.0
    mock_lookup.assert_called_once_with("SVC21C.T")


# ---------------------------------------------------------------------------
# NO ACTION spot-checks — documented pinned behavior (see
# scratch/review/services_resolution.md for the triage rationale).
# ---------------------------------------------------------------------------


def test_no_action_svc06_chart_image_bounds_enforced_at_schema():
    """SVC-06 NO ACTION: the route-level schema is the trust boundary.

    AIAnalyzeChartImageRequest rejects URLs (pinned by
    tests/test_schema_contract_chart_image_2026_10_23.py::test_url_rejected)
    and caps payloads at CHART_IMAGE_MAX_IMAGE_DATA_CHARS, so the service API
    never sees the cases the finding hypothesized.
    """
    from schemas.analysis import AIAnalyzeChartImageRequest

    with pytest.raises(ValidationError) as url_exc:
        AIAnalyzeChartImageRequest(image_data="https://example.com/x.png")
    assert "image_url" in str(url_exc.value)

    with pytest.raises(ValidationError) as size_exc:
        AIAnalyzeChartImageRequest(image_data="A" * (CHART_IMAGE_MAX_IMAGE_DATA_CHARS + 1))
    assert "image_too_large" in str(size_exc.value)

    accepted = AIAnalyzeChartImageRequest(image_data="AAAA")
    assert accepted.image_data == "AAAA"


def test_no_action_svc11_close_clears_current_thread_state():
    """SVC-11 NO ACTION: close() is only invoked from app_state.shutdown()
    (app_state.py:409-414, process teardown) and tests; the report's own
    falsification ("close() only called at atexit ⇒ impact nil") applies, and
    an epoch rebuild would only create NEW sessions during shutdown. Same-thread
    reuse already rebuilds because close() clears _local.session.
    """
    from services.fallback_provider import YahooWebScraperProvider

    provider = YahooWebScraperProvider()
    fake_session = MagicMock()
    provider._local.session = fake_session
    with provider._sessions_lock:
        provider._all_sessions.add(fake_session)

    provider.close()

    fake_session.close.assert_called_once()
    assert getattr(provider._local, "session", None) is None
    with provider._sessions_lock:
        assert len(provider._all_sessions) == 0


def test_no_action_svc16_trending_titles_cache_hit_pinned():
    """SVC-16 NO ACTION (info): the synchronous cache-miss build is a pinned
    contract — tests/test_search_service_more.py:264-269
    (test_builds_when_cache_miss) asserts the synchronous result, so the route
    usage noted by the report cannot be changed without breaking that pin.
    Only the cache-hit path is spot-checked here.
    """
    with (
        patch(
            "services.search_service._get_cached_value", return_value=["Cached 1", "Cached 2"]
        ),
        patch("services.search_service._build_market_trending_titles") as mock_build,
    ):
        result = _get_market_trending_titles("us", "ddgs_only", "dummy", "")

    assert result == ["Cached 1", "Cached 2"]
    mock_build.assert_not_called()


def test_no_action_svc19_sbi_pts_price_shape_pinned():
    """SVC-19 NO ACTION: the SBI PTS parse window (scrapers.py fetch_pts_quote)
    extracts only the PTS price — the source data available to the parser
    carries no change/前日比 field (fixture mirrors
    tests/test_realtime_engine.py:463-481), so there is nothing to compute a
    change from without adding a yfinance call to the scrape path. The pinned
    price/pts/source shape is spot-checked here.
    """
    scraper = SBISecuritiesScraper()
    mock_html = (
        "<table><tr><td>現在値</td><td>2,983.5</td></tr></table>"
        "<div>PTS</div><div class='pts-price'>2,900.0</div>"
    )
    resp = MagicMock()
    resp.status_code = 200
    resp.content = mock_html.encode("cp932")
    with patch.object(scraper.session, "get", return_value=resp):
        payload = scraper.fetch_pts_quote("7203.T")

    assert payload is not None
    assert payload["price"] == 2900.0
    assert payload["pts"] is True
    assert payload["source"] == "sbi_pts"


def test_no_action_svc22_market_open_hours_contract_pinned():
    """SVC-22 NO ACTION (info): services.realtime.utils.is_jp_market_open has
    no production callers (only re-exports + tests); production market-open
    flags flow through utils.market_utils.is_market_open, which IS
    holiday-aware (utils/market_utils.py:350-357). The weekday/hours contract
    that tests/test_realtime_engine.py:37-54 pins is spot-checked here —
    holiday handling is intentionally left unchanged.
    """
    tue_open = datetime(2026, 8, 4, 10, 0, tzinfo=JST)
    sat_closed = datetime(2026, 8, 8, 10, 0, tzinfo=JST)
    assert is_jp_market_open(tue_open) is True
    assert is_jp_market_open(sat_closed) is False
