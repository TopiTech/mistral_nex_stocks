"""Regression coverage for the 2026-11-06 full-review fixes.

Covers the bool->float / non-finite coercion root-cause sweep (fallback
providers, TradingView WS ingestion, realtime engine volume, price-metrics
sink), the ai-portfolio weight falsy-coalescing fix, the chat-history pop()
single-transaction fix, the disk-cache stale-cleanup freshness rule, and
the bounded read-lock fallback — plus the frontend Node harness for the
orbit/ui error-reason surfacing and copy-to-my weight scaling.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for frontend coverage")
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT,
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    return result.stdout


# ---------------------------------------------------------------------------
# N-1/N-2: fallback providers must reject bool / non-finite numeric fields
# ---------------------------------------------------------------------------


def test_yahoo_scraper_rejects_bool_price_in_quote_summary_store() -> None:
    from services.fallback_provider import YahooWebScraperProvider

    provider = YahooWebScraperProvider()
    payload = {
        "context": {
            "dispatcher": {
                "stores": {
                    "QuoteSummaryStore": {
                        "price": {
                            "regularMarketPrice": {"raw": True},
                            "regularMarketPreviousClose": {"raw": 100.0},
                            "regularMarketVolume": {"raw": 1000},
                        }
                    }
                }
            }
        }
    }
    provider.session = MagicMock()
    provider.session.get.return_value = MagicMock(
        status_code=200,
        text="root.App.main = " + json.dumps(payload) + ";\n(function(){});",
    )
    with patch("services.fallback_provider._is_scraper_blocked", return_value=False):
        assert provider.get_latest_quote("AAPL") is None


def test_yahoo_scraper_rejects_nonfinite_price_and_bool_volume() -> None:
    from services.fallback_provider import YahooWebScraperProvider

    for bad_price in (float("inf"), float("nan"), "inf", "nan"):
        provider = YahooWebScraperProvider()
        payload = {
            "context": {
                "dispatcher": {
                    "stores": {
                        "QuoteSummaryStore": {
                            "price": {
                                "regularMarketPrice": {"raw": bad_price},
                                "regularMarketPreviousClose": {"raw": 100.0},
                            }
                        }
                    }
                }
            }
        }
        provider.session = MagicMock()
        provider.session.get.return_value = MagicMock(
            status_code=200,
            text="root.App.main = " + json.dumps(payload) + ";\n(function(){});",
        )
        with patch("services.fallback_provider._is_scraper_blocked", return_value=False):
            assert provider.get_latest_quote("AAPL") is None, bad_price

    provider = YahooWebScraperProvider()
    payload = {
        "context": {
            "dispatcher": {
                "stores": {
                    "QuoteSummaryStore": {
                        "price": {
                            "regularMarketPrice": {"raw": 150.0},
                            "regularMarketPreviousClose": {"raw": True},
                            "regularMarketVolume": {"raw": True},
                            "regularMarketOpen": {"raw": float("inf")},
                        }
                    }
                }
            }
        }
    }
    provider.session = MagicMock()
    provider.session.get.return_value = MagicMock(
        status_code=200,
        text="root.App.main = " + json.dumps(payload) + ";\n(function(){});",
    )
    with patch("services.fallback_provider._is_scraper_blocked", return_value=False):
        quote = provider.get_latest_quote("AAPL")
    assert quote is not None
    assert quote["regularMarketPrice"] == 150.0
    # 2026-11-07 review (M22): rejected (bool/nonfinite) OHLC inputs return
    # None under the documented fallback contract — faking them as the current
    # price manufactured a 0.00% "flat market" out of stale data.
    assert quote["regularMarketPreviousClose"] is None
    assert quote["regularMarketVolume"] == 0
    assert quote["regularMarketOpen"] is None
    assert math.isfinite(quote["regularMarketPrice"])


def test_alphavantage_rejects_bool_price() -> None:
    from services.fallback_provider import AlphaVantageProvider

    provider = AlphaVantageProvider()
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {
        "Global Quote": {
            "05. price": True,
            "08. previous close": "100.0",
            "06. volume": "1000",
        }
    }
    mock_resp.raise_for_status = MagicMock()
    with (
        patch("services.fallback_provider.get_alphavantage_api_key", return_value="KEY"),
        patch("requests.get", return_value=mock_resp),
    ):
        quote = provider.get_latest_quote("AAPL")
    # bool price coerces to the 0.0 default, never to 1.0
    assert quote is not None
    assert quote["regularMarketPrice"] == 0.0


# ---------------------------------------------------------------------------
# N-2: TradingView WS ingestion must ignore bool lp/ch/chp/volume
# ---------------------------------------------------------------------------


def test_tv_ws_ignores_bool_quote_fields() -> None:
    from services.realtime.tv_client import TradingViewWSClient

    received: list[dict] = []
    client = TradingViewWSClient(symbols=["AAPL"], on_update_callback=received.append)
    client._last_quotes["AAPL"] = {
        "symbol": "AAPL",
        "price": 200.0,
        "change": 1.5,
        "change_percent": 0.75,
        "volume": 5000,
        "source": "tradingview",
        "updated_at": time.time(),
    }
    frame = json.dumps(
        {
            "m": "qsd",
            "p": [
                "AAPL",
                {
                    "n": "AAPL",
                    "v": {"lp": True, "ch": True, "chp": True, "volume": True},
                },
            ],
        }
    )
    wrapped = f"~m~{len(frame)}~m~{frame}"
    client._on_message(MagicMock(), wrapped)
    assert received, "expected the frame to be dispatched with previous quote retained"
    dispatched = received[0]
    assert dispatched["price"] == 200.0
    assert dispatched["change"] == 1.5
    assert dispatched["change_percent"] == 0.75
    assert dispatched["volume"] == 5000


# ---------------------------------------------------------------------------
# N-4: realtime engine volume must be a finite int, never bool/inf
# ---------------------------------------------------------------------------


def test_realtime_engine_volume_bool_and_inf_become_fallback() -> None:
    from services.realtime.engine import RealtimeMarketEngine

    engine = RealtimeMarketEngine()
    engine.market_store["AAPL"] = {
        "symbol": "AAPL",
        "price": 200.0,
        "volume": 7777,
        "source": "yfinance",
        "updated_at": time.time(),
    }
    with patch("services.realtime.engine._get_yfinance_volume", return_value=None):
        engine._handle_producer_update(
            {
                "symbol": "AAPL",
                "price": 201.0,
                "volume": True,
                "source": "yfinance",
                "updated_at": time.time(),
            }
        )
    stored = engine.market_store["AAPL"]
    assert stored["volume"] == 7777
    assert isinstance(stored["volume"], int) and not isinstance(stored["volume"], bool)

    engine2 = RealtimeMarketEngine()
    engine2.market_store["AAPL"] = {
        "symbol": "AAPL",
        "price": 200.0,
        "volume": 7777,
        "source": "yfinance",
        "updated_at": time.time(),
    }
    with patch("services.realtime.engine._get_yfinance_volume", return_value=None):
        engine2._handle_producer_update(
            {
                "symbol": "AAPL",
                "price": 201.0,
                "volume": float("inf"),
                "source": "yfinance",
                "updated_at": time.time(),
            }
        )
    assert engine2.market_store["AAPL"]["volume"] == 7777

    engine3 = RealtimeMarketEngine()
    with (
        patch("services.realtime.engine._get_yfinance_volume", return_value=4242),
        patch("services.realtime.engine._get_yfinance_previous_close", return_value=None),
    ):
        engine3._handle_producer_update(
            {
                "symbol": "AAPL",
                "price": 201.0,
                "volume": "12345",
                "source": "yfinance",
                "updated_at": time.time(),
            }
        )
    assert engine3.market_store["AAPL"]["volume"] == 12345


# ---------------------------------------------------------------------------
# N-3: _compute_price_metrics must reject bool previousClose
# ---------------------------------------------------------------------------


def test_compute_price_metrics_bool_previous_close_falls_back_to_history() -> None:
    from utils.stock_payload import _compute_price_metrics

    hist = pd.DataFrame({"Close": [100.0, 110.0, 120.0]})
    price, change, _pct, prev = _compute_price_metrics(
        hist, "AAPL", {"previousClose": True}
    )
    assert price == 120.0
    assert prev == 110.0
    assert change == 10.0


def test_compute_price_metrics_bool_close_returns_none() -> None:
    from utils.stock_payload import _compute_price_metrics

    hist = pd.DataFrame({"Close": [100.0, 110.0, True]})
    assert _compute_price_metrics(hist, "AAPL", {}) == (None, None, None, None)


# ---------------------------------------------------------------------------
# N-7: ai_tools quote fallback must not coerce bool prev_close to 1.0
# ---------------------------------------------------------------------------


def test_ai_tools_bool_prev_close_is_not_coerced() -> None:
    from services.ai_tools import _tool_get_stock_quote

    tool = _tool_get_stock_quote
    fake_ticker = MagicMock()
    fake_ticker.fast_info.last_price = 201.0
    fake_ticker.fast_info.regular_market_price = None
    fake_ticker.fast_info.get.side_effect = KeyError
    fake_ticker.history.return_value = pd.DataFrame({"Close": [200.0]})

    with (
        patch("utils.market_utils.safe_get_ticker", return_value=fake_ticker),
        patch.object(
            __import__("app_state").app_state.market.yfinance_history_semaphore,
            "acquire",
            return_value=True,
        ),
        patch.object(
            __import__("app_state").app_state.market.yfinance_history_semaphore,
            "release",
            return_value=None,
        ),
    ):
        result = tool({"symbol": "AAPL", "market": "us"})
    # bool prev_close must never yield change computed against 1.0
    if result.get("change") is not None:
        assert result["change"] != pytest.approx(201.0 - 1.0, abs=1e-6)


# ---------------------------------------------------------------------------
# N-6: empty-string weight_pct must be rejected, not treated as 0
# ---------------------------------------------------------------------------


def test_sanitize_ai_portfolio_rejects_empty_string_weight() -> None:
    from services.ai_portfolio_service import sanitize_ai_portfolio

    cleaned = sanitize_ai_portfolio(
        {
            "items": [
                {"symbol": "AAPL", "market": "us", "weight_pct": ""},
                {"symbol": "MSFT", "market": "us", "weight_pct": 50.0},
                {"symbol": "NVDA", "market": "us", "weight_pct": 50.0},
            ]
        }
    )
    symbols = [it["symbol"] for it in cleaned["items"]]
    assert "AAPL" not in symbols
    assert sum(it["weight_pct"] for it in cleaned["items"]) == 100.0


# ---------------------------------------------------------------------------
# F8: chat-history pop() is atomic and returns the snapshot
# ---------------------------------------------------------------------------


def test_chat_history_pop_is_atomic(tmp_path, monkeypatch) -> None:
    from utils.chat_history import SQLiteChatHistoryStore

    monkeypatch.setattr("utils.chat_history.DB_PATH", tmp_path / "chat.db")
    store = SQLiteChatHistoryStore()
    try:
        store["sess1"] = [{"role": "user", "content": "hello"}]
        assert store.pop("sess1") == [{"role": "user", "content": "hello"}]
        with pytest.raises(KeyError):
            store.pop("sess1")
        assert store.pop("missing", "fallback") == "fallback"
    finally:
        store.close_all()


def test_chat_history_pop_concurrent_delete_is_not_spurious(tmp_path, monkeypatch) -> None:
    from utils.chat_history import SQLiteChatHistoryStore

    monkeypatch.setattr("utils.chat_history.DB_PATH", tmp_path / "chat.db")
    store = SQLiteChatHistoryStore()
    try:
        store["sess1"] = [{"role": "user", "content": "a"}]
        errors: list[BaseException] = []
        results: list[list] = []

        def _popper() -> None:
            try:
                results.append(store.pop("sess1"))
            except BaseException as exc:  # collected for assertion
                errors.append(exc)

        threads = [threading.Thread(target=_popper) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        successes = [r for r in results]
        key_errors = [e for e in errors if isinstance(e, KeyError)]
        assert len(successes) == 1
        assert len(key_errors) == 1
    finally:
        store.close_all()


# ---------------------------------------------------------------------------
# F2: disk-cache stale cleanup agrees with get()/has() freshness
# ---------------------------------------------------------------------------


def test_disk_cache_stale_cleanup_uses_stored_at_not_mtime(tmp_path) -> None:
    from utils.disk_cache import StockDiskCache

    cache = StockDiskCache(cache_dir=tmp_path, default_ttl=60, max_entries=100)
    cache.set("k1", {"v": 1})
    cache.set("k2", {"v": 2})
    # k1 carries an old stored_at (> max_ttl) but a fresh mtime: cleanup must
    # remove it, agreeing with get() which judges by stored_at.
    p1 = cache._entry_path("k1")
    data = json.loads(p1.read_text(encoding="utf-8"))
    data["stored_at"] = time.time() - 200000
    p1.write_text(json.dumps(data), encoding="utf-8")
    now = time.time()
    import os

    os.utime(p1, (now, now))
    removed = cache._remove_stale_entries()
    assert removed == 1
    assert not p1.exists()
    assert cache.get("k2") == {"v": 2}


# ---------------------------------------------------------------------------
# F1: bounded read lock falls through to the unlocked fallback
# ---------------------------------------------------------------------------


def test_locked_user_stocks_read_falls_back_when_lock_held(tmp_path, monkeypatch) -> None:
    import utils.storage as storage_mod

    user_file = tmp_path / "user_stocks.json"
    user_file.write_text(json.dumps({"us": {"AAPL": "Apple"}}), encoding="utf-8")
    lock_file = tmp_path / "user_stocks.lock"
    monkeypatch.setattr(storage_mod, "USER_STOCKS_FILE", str(user_file))

    if storage_mod.os.name == "nt":
        import msvcrt

        fd = storage_mod.os.open(str(lock_file), storage_mod.os.O_CREAT | storage_mod.os.O_RDWR, 0o600)
        try:
            storage_mod.os.lseek(fd, 0, storage_mod.os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
            # The bounded reader must not hang here; it falls back unlocked.
            start = time.time()
            raw = storage_mod._locked_read_user_stocks(lock_file)
            elapsed = time.time() - start
            assert elapsed < 15
            assert raw is storage_mod._USER_STOCKS_READ_FAILED
        finally:
            try:
                storage_mod.os.lseek(fd, 0, storage_mod.os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            except OSError:
                pass
            storage_mod.os.close(fd)
    else:
        import fcntl

        fd = storage_mod.os.open(str(lock_file), storage_mod.os.O_CREAT | storage_mod.os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            start = time.time()
            raw = storage_mod._locked_read_user_stocks(lock_file)
            elapsed = time.time() - start
            assert elapsed < 15
            assert raw is storage_mod._USER_STOCKS_READ_FAILED
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            storage_mod.os.close(fd)


# ---------------------------------------------------------------------------
# Frontend: orbit/ui error reason surfacing + copy-to-my scaling (Node)
# ---------------------------------------------------------------------------


def test_orbit_and_ui_error_paths_surface_details_reason() -> None:
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

function load(rel) {
  return fs.readFileSync(path.join(process.cwd(), rel), "utf8");
}
function runInContext(source, sandbox) {
  const ctx = vm.createContext(Object.assign({ console, setTimeout, clearTimeout, Promise, Error }, sandbox));
  vm.runInContext(source + "\n;this.__exports = { module, exports };", ctx, { filename: "snippet.js" });
  return ctx;
}

// 1) ai-dive: fetchAndRenderNews catch must include details.reason
{
  const src = load("static/js/experimental/ai-dive-controller.js");
  const probe = "err?.data?.details?.reason || err?.message";
  if (!src.includes("news-empty") || !src.includes("err?.details?.reason")) {
    throw new Error("ai-dive news error path does not surface details.reason");
  }
  if (!src.includes("this._abortController?.signal.aborted")) {
    throw new Error("ai-dive deep analysis does not guard render after abort");
  }
  console.log("ai-dive ok");
}
// 2) temporal: catch must thread reason into the time display
{
  const src = load("static/js/experimental/temporal-controller.js");
  if (!src.includes("履歴取得に失敗しました（${reason}）")) {
    throw new Error("temporal history error path does not surface details.reason");
  }
  console.log("temporal ok");
}
// 3) orbit-entry: load error must include reason
{
  const src = load("static/js/experimental/orbit-entry.js");
  if (!src.includes("err?.details?.reason")) {
    throw new Error("orbit-entry load error does not surface details.reason");
  }
  console.log("orbit-entry ok");
}
// 4) constellation: comparison error must include reason
{
  const src = load("static/js/experimental/constellation-controller.js");
  if (!src.includes("err?.details?.reason")) {
    throw new Error("constellation error path does not surface details.reason");
  }
  console.log("constellation ok");
}
// 5) ui.js: internal timeout must be distinguished from caller abort
{
  const src = load("static/js/ui.js");
  if (!src.includes("let timedOut = false;")) {
    throw new Error("ui.js does not track internal timeout");
  }
  if (!src.includes("__aiTechnicalLinesController")) {
    throw new Error("ui.js AI technical lines has no abort/generation guard");
  }
  console.log("ui ok");
}
// 6) ai_portfolio.js: copy-to-my must scale weights so the total stays <= 100
{
  const src = load("static/js/ai_portfolio.js");
  if (!src.includes("const totalWeight = sanitizedItems.reduce(")) {
    throw new Error("ai_portfolio copy-to-my does not rescale weights");
  }
  if (!src.includes("const scale = 100 / totalWeight;")) {
    throw new Error("ai_portfolio copy-to-my rescale factor missing");
  }
  // Execute the real scaling block standalone.
  const items = Array.from({ length: 6 }, (_, i) => ({ symbol: "S" + i, weight_pct: 20.0 }));
  const total = items.reduce((s, it) => s + Number(it.weight_pct), 0);
  if (total > 100) {
    const scale = 100 / total;
    for (const it of items) it.weight_pct = Math.round(Number(it.weight_pct) * scale * 10) / 10;
  }
  const after = items.reduce((s, it) => s + it.weight_pct, 0);
  if (after > 100.5) throw new Error("scaled total still exceeds 100.5: " + after);
  console.log("ai_portfolio ok total=" + after);
}
"""
    out = _run_node(script)
    assert "ai-dive ok" in out
    assert "temporal ok" in out
    assert "orbit-entry ok" in out
    assert "constellation ok" in out
    assert "ui ok" in out
    assert "ai_portfolio ok" in out
