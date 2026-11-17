"""Comprehensive regression test suite for BUG-01 through BUG-21 (2026-11-17 review)."""

from __future__ import annotations

import json
import logging
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from flask import Flask
from requests.exceptions import HTTPError as RequestsHTTPError

import bg.common as bg_common
import credential_manager
import logging_config
import messaging
from ai_state import AIState
from app_factory.factory import add_request_hooks
from app_state import app_state
from bg.sync import process as bg_sync_process
from bg.sync import state as bg_sync_state
from constants import DEFAULT_USDJPY_RATE
from routes import api_analysis
from services import ai_service, embeddings_service, openai_service
from services.ai_service import ProviderAPIError
from services.fallback_provider import AlphaVantageProvider
from services.stock import retry as stock_retry
from utils import caching, chat_history
from utils.disk_cache import StockDiskCache, _remove_fields_recursive

ROOT_DIR = Path(__file__).resolve().parent.parent


def _run_node_script(script: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute frontend UI regression tests")
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT_DIR,
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert result.stdout.strip().endswith("ok")


# ---------------------------------------------------------------------------
# Unit 1: BUG-01 .. BUG-05
# ---------------------------------------------------------------------------


class _FakeInnerHttpClient:
    def __init__(self) -> None:
        self.is_closed = False
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        self.is_closed = True


class _FakeSdkConfiguration:
    def __init__(self, inner: _FakeInnerHttpClient) -> None:
        self.client: _FakeInnerHttpClient | None = inner
        self.async_client: _FakeInnerHttpClient | None = _FakeInnerHttpClient()
        self.client_supplied = False


class _FakeMistralSdkClient:
    """Mimics ``mistralai.Mistral``: no ``.close()`` or ``.is_closed`` attribute."""

    def __init__(self) -> None:
        self.inner = _FakeInnerHttpClient()
        self.sdk_configuration = _FakeSdkConfiguration(self.inner)
        self.exit_calls = 0

    def __exit__(self, _exc_type, _exc_val, _exc_tb) -> None:
        self.exit_calls += 1
        if self.sdk_configuration.client is not None:
            self.sdk_configuration.client.close()
        self.sdk_configuration.client = None


def test_bug01_ai_state_closes_mistral_sdk_clients_on_prune_and_eviction():
    """BUG-01: SDK clients without .close()/.is_closed must still be closed cleanly."""
    sdk_client = _FakeMistralSdkClient()
    assert AIState._is_client_closed(sdk_client) is False

    AIState._close_client_safely(sdk_client, "test-sdk")
    assert sdk_client.exit_calls == 1
    assert sdk_client.inner.close_calls == 1
    assert sdk_client.sdk_configuration.async_client is not None
    assert sdk_client.sdk_configuration.async_client.close_calls == 1
    assert AIState._is_client_closed(sdk_client) is True

    # Verify LRU eviction retires and prunes excess SDK clients
    retired: deque[tuple[float, object]] = deque()
    c1 = _FakeMistralSdkClient()
    c2 = _FakeMistralSdkClient()
    retired.append((time.monotonic(), c1))
    retired.append((time.monotonic(), c2))
    AIState._prune_retired_clients_locked(retired, max_retired=1, label="evict-test")
    assert c1.exit_calls == 1
    assert c1.inner.is_closed is True
    assert len(retired) == 1
    assert retired[0][1] is c2


def test_bug02_logging_fallback_attaches_single_stream_handler(monkeypatch):
    """BUG-02: OSError fallback to stderr must not double-log ERROR records."""
    root = logging.getLogger()
    werkzeug_logger = logging.getLogger("werkzeug")
    saved_root_handlers = list(root.handlers)
    saved_root_level = root.level
    saved_werkzeug_filters = list(werkzeug_logger.filters)

    test_app = Flask("bug02_logging_test")
    attached_handlers: list[logging.Handler] = []
    try:
        root.handlers.clear()
        monkeypatch.setattr(
            logging_config,
            "_create_restricted_file_handlers",
            lambda _b, _e: (_ for _ in ()).throw(OSError("read-only dir")),
        )
        logging_config.init_logging(test_app)
        attached_handlers = list(root.handlers)

        stream_handlers = [
            h for h in attached_handlers if isinstance(h, logging.StreamHandler)
        ]
        assert len(stream_handlers) == 1
        assert len(test_app.logger.handlers) == 1
        assert test_app.logger.handlers[0] is stream_handlers[0]

        records_seen: list[str] = []
        orig_emit = stream_handlers[0].emit

        def _capture_emit(record: logging.LogRecord) -> None:
            records_seen.append(record.getMessage())
            orig_emit(record)

        monkeypatch.setattr(stream_handlers[0], "emit", _capture_emit)
        test_app.logger.error("single-stderr-error-check")
        assert records_seen.count("single-stderr-error-check") == 1
    finally:
        for handler in attached_handlers:
            try:
                handler.close()
            except Exception:
                pass
        root.handlers[:] = saved_root_handlers
        root.setLevel(saved_root_level)
        werkzeug_logger.filters[:] = saved_werkzeug_filters


def test_bug03_cross_site_403_has_request_id_and_non_negative_elapsed_ms(caplog):
    """BUG-03: Early Sec-Fetch-Site 403 must still get request_id and start timestamp."""
    hooked_app = Flask("bug03_hooks_test")
    hooked_app.config["MNS_SKIP_BOOTSTRAP"] = True
    add_request_hooks(hooked_app)

    @hooked_app.route("/api/ping", methods=["POST"])
    def _ping():
        return {"ok": True}

    client = hooked_app.test_client()
    with caplog.at_level(logging.WARNING, logger="app"):
        res = client.post(
            "/api/ping",
            headers={"Sec-Fetch-Site": "cross-site"},
        )

    assert res.status_code == 403
    req_id = res.headers.get("X-MNS-Request-Id", "-")
    assert req_id != "-"
    assert len(req_id) >= 8
    warning_logs = [r.getMessage() for r in caplog.records if "status=403" in r.getMessage()]
    assert warning_logs
    assert any("elapsed_ms=-1" not in msg for msg in warning_logs)


def test_bug04_announcer_keeps_unbounded_maxsize_zero_listeners():
    """BUG-04: MessageAnnouncer must treat maxsize=0 as unbounded, not full."""
    announcer = messaging.MessageAnnouncer()
    unbounded_q: queue.Queue = queue.Queue(maxsize=0)
    bounded_q: queue.Queue = queue.Queue(maxsize=2)
    bounded_q.put_nowait("a")
    bounded_q.put_nowait("b")

    with announcer.lock:
        announcer.listeners = [unbounded_q, bounded_q]

    announcer.announce("hello-1")
    announcer.announce("hello-2")

    assert announcer.dropped_count == 1
    assert announcer.listeners == [unbounded_q]
    assert unbounded_q.get_nowait() == "hello-1"
    assert unbounded_q.get_nowait() == "hello-2"
    # Evicted bounded queue received None sentinel
    evicted_items = [bounded_q.get_nowait(), bounded_q.get_nowait()]
    assert None in evicted_items


def test_bug05_get_model_name_rejects_empty_and_whitespace(monkeypatch):
    """BUG-05: Blank or whitespace mistral_model config must fall back to default."""
    monkeypatch.setattr(
        credential_manager.config_store,
        "load_config",
        lambda: {"mistral_model": ""},
    )
    model_empty = credential_manager.get_model_name()
    assert model_empty.strip() != ""

    monkeypatch.setattr(
        credential_manager.config_store,
        "load_config",
        lambda: {"mistral_model": "   \t\n  "},
    )
    model_ws = credential_manager.get_model_name()
    assert model_ws == model_empty

    monkeypatch.setattr(
        credential_manager.config_store,
        "load_config",
        lambda: {"mistral_model": "  mistral-small-latest  "},
    )
    assert credential_manager.get_model_name() == "mistral-small-latest"


# ---------------------------------------------------------------------------
# Unit 2: BUG-06 .. BUG-10
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("test_key", "clear_fn"),
    [
        ("bug06_exact_key", lambda: caching.clear_cache_key("bug06_exact_key")),
        ("bug06_pref_item", lambda: caching.clear_cache_prefix("bug06_pref_")),
    ],
)
def test_bug06_get_cached_does_not_repopulate_after_inflight_invalidation(test_key, clear_fn):
    """BUG-06: clear_cache_key / clear_cache_prefix mid-fetch must prevent stale write-back."""
    caching.clear_cache_key(test_key)
    started = threading.Event()
    proceed = threading.Event()
    holder: dict[str, object] = {}

    def _fetch():
        started.set()
        assert proceed.wait(timeout=5.0)
        return {"stale": True}

    def _worker():
        holder["result"] = caching.get_cached(test_key, _fetch, duration=60)

    t = threading.Thread(target=_worker)
    t.start()
    try:
        assert started.wait(timeout=5.0)
        clear_fn()
        proceed.set()
        t.join(timeout=5.0)
        assert not t.is_alive()
    finally:
        proceed.set()
        t.join(timeout=1.0)

    assert holder["result"] == {"stale": True}
    assert caching.peek_cached(test_key, duration=60) is None


def test_bug07_sse_frame_publish_lock_guards_announce_execution(monkeypatch):
    """BUG-07: announce_fn must run inside _sse_frame_publish_lock so order cannot invert."""
    observed_other_thread_acquired: list[bool] = []

    def _probe_announce(_announcer, _frame, _mode):
        probe_res: list[bool] = []

        def _try_acquire():
            acquired = bg_common._sse_frame_publish_lock.acquire(blocking=False)
            if acquired:
                bg_common._sse_frame_publish_lock.release()
            probe_res.append(acquired)

        pt = threading.Thread(target=_try_acquire)
        pt.start()
        pt.join(timeout=2.0)
        observed_other_thread_acquired.extend(probe_res)

    monkeypatch.setattr(bg_common, "_get_app_bg_attr", lambda _n, d=None: d)
    monkeypatch.setattr(bg_common, "_set_app_bg_attr", lambda _n, _v: None)
    monkeypatch.setattr(bg_common, "is_market_open", lambda _m: False)
    monkeypatch.setattr(bg_common, "_announce_frame", _probe_announce)

    orig_stocks = app_state.market.current_stocks_cache
    orig_indices = app_state.market.current_indices_cache
    try:
        app_state.market.current_stocks_cache = {
            "us": [{"symbol": "AAPL", "price": 201.0}],
            "jp": [],
            "idx": [],
        }
        app_state.market.current_indices_cache = {}
        bg_common._raw_announce_current_market_state()
    finally:
        app_state.market.current_stocks_cache = orig_stocks
        app_state.market.current_indices_cache = orig_indices

    assert observed_other_thread_acquired == [False]


def test_bug08_warm_payload_cache_preserves_all_markets_in_mtimes(tmp_path, monkeypatch):
    """BUG-08: Pruning _last_loaded_mtimes must preserve us, jp, and idx keys."""
    disk_cache = StockDiskCache(cache_dir=tmp_path / "payload", enable_cleanup=False)
    for sym, mkt in (("AAPL", "us"), ("7203.T", "jp"), ("^N225", "idx")):
        disk_cache.set(f"payload_{sym}_{mkt}", {"symbol": sym, "price": 100.0})

    orig_cache = app_state.payload_disk_cache
    orig_mtimes = dict(bg_sync_state._last_loaded_mtimes)
    orig_target = app_state.market.target_stocks_cache
    orig_current = app_state.market.current_stocks_cache
    with app_state.market.user_stocks_lock:
        orig_us = dict(app_state.market.user_us)
        orig_jp = dict(app_state.market.user_jp)
        orig_idx = dict(app_state.market.user_idx)
        app_state.market.user_us = {"AAPL": "Apple"}
        app_state.market.user_jp = {"7203.T": "Toyota"}
        app_state.market.user_idx = {"^N225": "Nikkei"}

    try:
        app_state.payload_disk_cache = disk_cache
        app_state.market.target_stocks_cache = {"us": [], "jp": [], "idx": []}
        app_state.market.current_stocks_cache = {"us": [], "jp": [], "idx": []}
        bg_sync_state._last_loaded_mtimes = {
            "payload_STALE_us": 1.0,
        }
        monkeypatch.setattr(bg_sync_process, "load_user_stocks", lambda force=False: None)
        monkeypatch.setattr(
            bg_sync_process,
            "_get_app_bg_attr",
            lambda name, default=None: (
                (lambda force=False: None) if name == "load_user_stocks" else default
            ),
        )
        monkeypatch.setattr(bg_sync_process, "_default_stock_names", lambda _m: [])
        monkeypatch.setattr(bg_sync_process, "_invalidate_sse_payload_cache", lambda: None)

        bg_sync_process._warm_payload_cache_from_disk()

        kept = set(bg_sync_state._last_loaded_mtimes.keys())
        assert "payload_AAPL_us" in kept
        assert "payload_7203.T_jp" in kept
        assert "payload_^N225_idx" in kept
        assert "payload_STALE_us" not in kept
    finally:
        app_state.payload_disk_cache = orig_cache
        bg_sync_state._last_loaded_mtimes = orig_mtimes
        app_state.market.target_stocks_cache = orig_target
        app_state.market.current_stocks_cache = orig_current
        with app_state.market.user_stocks_lock:
            app_state.market.user_us = orig_us
            app_state.market.user_jp = orig_jp
            app_state.market.user_idx = orig_idx


def test_bug09_chat_history_reconnects_other_thread_after_close_all(tmp_path, monkeypatch):
    """BUG-09: Cross-thread close_all() must invalidate cached thread-local connections."""
    monkeypatch.setattr(chat_history, "DB_PATH", tmp_path / "chat_bug09.db")
    monkeypatch.setattr(chat_history, "_db_initialized", False)
    store = chat_history.SQLiteChatHistoryStore()
    errors: list[BaseException] = []
    ready_to_close = threading.Event()
    closed_done = threading.Event()
    first_conn_id: list[int] = []
    second_conn_id: list[int] = []
    closed_check_raised: list[bool] = []

    def _worker() -> None:
        try:
            c1 = store._get_connection()
            first_conn_id.append(id(c1))
            store.record_turn("k:us:AAPL", init_messages=[], append={"role": "user", "content": "hi"})
            ready_to_close.set()
            assert closed_done.wait(timeout=5.0)
            try:
                c1.execute("SELECT 1")
                closed_check_raised.append(False)
            except sqlite3.ProgrammingError:
                closed_check_raised.append(True)
            c2 = store._get_connection()
            second_conn_id.append(id(c2))
            msgs = store.get("k:us:AAPL")
            assert len(msgs) == 1
        except BaseException as exc:
            errors.append(exc)
        finally:
            store.close()

    t = threading.Thread(target=_worker)
    t.start()
    try:
        assert ready_to_close.wait(timeout=5.0)
        store.close_all()
        closed_done.set()
        t.join(timeout=5.0)
        assert not t.is_alive()
    finally:
        closed_done.set()
        t.join(timeout=1.0)
        store.close_all()

    assert errors == [], f"Unexpected worker errors: {errors!r}"
    assert closed_check_raised == [True]
    assert len(first_conn_id) == 1 and len(second_conn_id) == 1
    assert first_conn_id[0] != second_conn_id[0]


def test_bug10_remove_fields_recursive_handles_deep_nesting_and_recursion_error(
    tmp_path, monkeypatch
):
    """BUG-10: Deep JSON (> sys.getrecursionlimit()) must migrate without RecursionError."""
    depth = sys.getrecursionlimit() + 300
    payload: dict[str, object] = {"api_key": "secret-leaf", "safe": 42}
    for i in range(depth):
        payload = {"api_key": f"secret-{i}" if i == 0 else None, "nested": payload}
        if payload["api_key"] is None:
            del payload["api_key"]

    cleaned, changed = _remove_fields_recursive(payload, frozenset({"api_key"}))
    assert changed is True

    cursor = cleaned
    for _ in range(depth):
        cursor = cursor["nested"]
    assert cursor == {"safe": 42}

    # Verify StockDiskCache.remove_fields_recursive handles RecursionError defensively
    cache = StockDiskCache(cache_dir=tmp_path / "disk_bug10", enable_cleanup=False)
    target_file = cache._cache_dir / "bad_deep.json"
    cache._cache_dir.mkdir(parents=True, exist_ok=True)
    target_file.write_text(json.dumps({"api_key": "x"}), encoding="utf-8")

    monkeypatch.setattr(
        "utils.disk_cache._remove_fields_recursive",
        lambda _v, _f: (_ for _ in ()).throw(RecursionError("simulated recursion depth")),
    )
    migrated = cache.remove_fields_recursive(["api_key"])
    assert migrated == 0
    assert not target_file.exists()


# ---------------------------------------------------------------------------
# Unit 3: BUG-11 .. BUG-14
# ---------------------------------------------------------------------------


def test_bug11_retry_yfinance_catches_http_error_before_oserror(monkeypatch):
    """BUG-11: RequestsHTTPError subclasses OSError; 429 must mark RL and 404 must fail fast."""
    monkeypatch.setattr(stock_retry.time, "sleep", lambda _s: None)
    rate_limit_calls: list[int] = []
    monkeypatch.setattr(
        stock_retry,
        "_handle_rate_limit_in_retry",
        lambda *a, **kw: (rate_limit_calls.append(1) or None, True),
    )

    attempts_429 = {"count": 0}

    @stock_retry.with_yfinance_retry(max_retries=1, base_delay=0.01)
    def _fn_429() -> None:
        attempts_429["count"] += 1
        resp = SimpleNamespace(status_code=429, headers={})
        raise RequestsHTTPError("429 Too Many Requests", response=resp)

    with pytest.raises(RequestsHTTPError):
        _fn_429()
    assert attempts_429["count"] == 2
    assert len(rate_limit_calls) >= 1

    attempts_404 = {"count": 0}

    @stock_retry.with_yfinance_retry(max_retries=2, base_delay=0.01)
    def _fn_404() -> None:
        attempts_404["count"] += 1
        resp = SimpleNamespace(status_code=404, headers={})
        raise RequestsHTTPError("404 Not Found", response=resp)

    with pytest.raises(RequestsHTTPError):
        _fn_404()
    assert attempts_404["count"] == 1


def test_bug12_extract_mistral_error_status_unifies_bool_and_raw_response():
    """BUG-12: Exclude bool and inspect raw_response/response across chat & stream paths."""

    class _FakeSdkError(Exception):
        def __init__(self, status_code=None, raw_response=None) -> None:
            super().__init__("sdk error")
            self.status_code = status_code
            self.raw_response = raw_response

    assert (
        ai_service._extract_mistral_error_status(
            _FakeSdkError(status_code=None, raw_response=SimpleNamespace(status_code=429))
        )
        == 429
    )
    assert (
        ai_service._extract_mistral_error_status(
            _FakeSdkError(status_code=True, raw_response=SimpleNamespace(status_code=403))
        )
        == 403
    )
    assert (
        ai_service._extract_mistral_error_status(_FakeSdkError(status_code=True))
        == 0
    )
    assert ai_service._extract_mistral_error_status(httpx.ReadTimeout("timeout")) == 504
    assert ai_service._extract_mistral_error_status(ConnectionError("conn")) == 503


def test_bug13_openai_embeddings_stops_on_429_and_rejects_bool_indices(monkeypatch):
    """BUG-13: Stop remaining batches on 429 and reject bool index/status coercion."""
    post_calls: list[list[str]] = []

    class _FakeClient429:
        def post(self, _url, json=None, headers=None):
            post_calls.append(list((json or {}).get("input") or []))
            return SimpleNamespace(status_code=429, text="rate limit")

    monkeypatch.setattr(openai_service, "get_openai_base_url", lambda: "https://api.example.com/v1")
    monkeypatch.setattr(openai_service, "validate_outbound_base_url", lambda _u: (True, ""))
    monkeypatch.setattr(openai_service, "_get_openai_client", lambda _b, _k: _FakeClient429())
    monkeypatch.setattr(app_state.market, "is_circuit_open", lambda _k: False)
    monkeypatch.setattr(
        app_state.market, "get_circuit_state", lambda _k: {"status": "CLOSED"}
    )
    monkeypatch.setattr(app_state.market, "report_circuit_result", lambda *_a, **_kw: None)

    out_429 = openai_service.get_openai_embeddings_batch(
        ["first batch", "second batch"], api_key="sk-test-valid", batch_size=1
    )
    assert out_429 == [None, None]
    assert len(post_calls) == 1

    class _FakeClientBoolIndex:
        def post(self, _url, json=None, headers=None):
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "data": [
                        {"index": True, "embedding": [1.0, 2.0]},
                        {"index": False, "embedding": [3.0, 4.0]},
                    ]
                },
            )

    monkeypatch.setattr(
        openai_service, "_get_openai_client", lambda _b, _k: _FakeClientBoolIndex()
    )
    out_bool = openai_service.get_openai_embeddings_batch(
        ["t0", "t1"], api_key="sk-test-valid", batch_size=2
    )
    assert out_bool == [[1.0, 2.0], [3.0, 4.0]]

    # Fallback provider bool status guard
    provider = AlphaVantageProvider()
    with (
        patch("services.fallback_provider.get_alphavantage_api_key", return_value="av-key"),
        patch(
            "requests.get",
            return_value=SimpleNamespace(
                status_code=True,
                text="normal text without limit",
                headers={},
                raise_for_status=lambda: None,
                json=lambda: {"Error Message": "bad"},
            ),
        ),
    ):
        assert provider.get_latest_quote("AAPL") is None
        assert provider._is_locally_blocked() is False


def test_bug14_embeddings_cache_namespace_separates_openai_base_urls(monkeypatch):
    """BUG-14: Switching OpenAI base URL must not return cached embeddings from previous URL."""
    current_url = {"url": "https://endpoint-a.example/v1"}
    calls: list[str] = []

    def _fake_batch(texts, api_key=None, model=None, batch_size=32):
        calls.append(current_url["url"])
        vec = [1.0] if "endpoint-a" in current_url["url"] else [2.0]
        return [vec for _ in texts]

    monkeypatch.setattr(credential_manager, "is_openai_provider", lambda: True)
    monkeypatch.setattr(
        credential_manager, "get_openai_embeddings_model", lambda: "text-embed-same"
    )
    monkeypatch.setattr(credential_manager, "get_openai_base_url", lambda: current_url["url"])
    monkeypatch.setattr(credential_manager, "get_openai_api_key", lambda: "sk-test-123")
    monkeypatch.setattr(openai_service, "get_openai_embeddings_batch", _fake_batch)

    with embeddings_service._EMBEDDINGS_CACHE_LOCK:
        embeddings_service._EMBEDDINGS_CACHE.clear()
    try:
        res_a = embeddings_service.get_mistral_embeddings_batch(
            ["same prompt"], api_key="sk-test-123"
        )
        current_url["url"] = "https://endpoint-b.example/v1"
        res_b = embeddings_service.get_mistral_embeddings_batch(
            ["same prompt"], api_key="sk-test-123"
        )
    finally:
        with embeddings_service._EMBEDDINGS_CACHE_LOCK:
            embeddings_service._EMBEDDINGS_CACHE.clear()

    assert res_a == [[1.0]]
    assert res_b == [[2.0]]
    assert calls == ["https://endpoint-a.example/v1", "https://endpoint-b.example/v1"]


# ---------------------------------------------------------------------------
# Unit 4: BUG-15 .. BUG-16
# ---------------------------------------------------------------------------


def test_bug15_chat_error_preserves_provider_status_over_401_substring():
    """BUG-15: Explicit 429/403/503 ProviderAPIError must not be coerced to 401 by message text."""
    from app import app

    with app.app_context():
        resp_429, status_429 = api_analysis._chat_error_response(
            ProviderAPIError("認証上限 401 の可能性もあるが実際は429", status_code=429),
            SimpleNamespace(request_id="req-429"),
        )
        assert status_429 == 429
        assert resp_429.get_json()["ok"] is False

        _, status_403 = api_analysis._chat_error_response(
            ProviderAPIError("HTTP 401 or 認証 keyword in 403 body", status_code=403),
            SimpleNamespace(request_id="req-403"),
        )
        assert status_403 == 403

        _, status_503 = api_analysis._chat_error_response(
            ProviderAPIError("認証 upstream timeout", status_code=503),
            SimpleNamespace(request_id="req-503"),
        )
        assert status_503 == 503

    # Also verify no raw {symbol} prompt interpolation remains at fixed sites
    src_text = (ROOT_DIR / "routes" / "api_analysis.py").read_text(encoding="utf-8")
    assert "[Current context: {symbol}" not in src_text
    assert "あなたは{symbol}銘柄の専門家" not in src_text
    assert "[Current context: {safe_symbol}" in src_text
    assert "あなたは{safe_symbol}銘柄の専門家" in src_text


@pytest.mark.parametrize("bad_fx", [None, "not-a-number", True])
def test_bug16_copy_ai_portfolio_handles_non_numeric_fx_rate(bad_fx):
    """BUG-16: Non-numeric or bool resolved_fx from get_current_usdjpy_rate must not raise TypeError."""
    from app import app

    orig_csrf = app.config.get("WTF_CSRF_ENABLED")
    orig_rate = app_state.market.last_usdjpy_rate
    orig_ts = app_state.market.last_usdjpy_rate_ts
    with app_state.market.user_stocks_lock:
        orig_us = dict(app_state.market.user_us)
        orig_jp = dict(app_state.market.user_jp)
        app_state.market.user_us = {}
        app_state.market.user_jp = {}

    app.config["WTF_CSRF_ENABLED"] = False
    try:
        items = [{"symbol": "NVDA", "market": "us", "weight_pct": 100.0, "target_price": 160.0}]
        with (
            patch("utils.networking.require_trusted_or_admin", return_value=(True, None)),
            patch("routes.stocks.ai_portfolio.save_user_stocks", return_value=None),
            patch("app_bg.schedule_sync_all_stocks_now", return_value=None),
            patch("routes.stocks.common._announce_watchlist_state", return_value=None),
            patch("routes.stocks.common._sync_realtime_symbol", return_value=None),
            patch(
                "routes.stocks.ai_portfolio.get_current_usdjpy_rate",
                return_value=(bad_fx, True),
            ),
        ):
            app_state.market.last_usdjpy_rate = DEFAULT_USDJPY_RATE
            app_state.market.last_usdjpy_rate_ts = 0.0
            res = app.test_client().post("/api/ai-portfolio/copy-to-my", json={"items": items})

        assert res.status_code == 200
        payload = res.get_json()
        assert payload["ok"] is True
        assert "stale_warning" in payload
        with app_state.market.user_stocks_lock:
            stored = app_state.market.user_us.get("NVDA")
        assert isinstance(stored, dict)
        assert stored["avg_fx_rate"] == DEFAULT_USDJPY_RATE
    finally:
        app.config["WTF_CSRF_ENABLED"] = orig_csrf
        app_state.market.last_usdjpy_rate = orig_rate
        app_state.market.last_usdjpy_rate_ts = orig_ts
        with app_state.market.user_stocks_lock:
            app_state.market.user_us = orig_us
            app_state.market.user_jp = orig_jp


# ---------------------------------------------------------------------------
# Unit 5: BUG-17 .. BUG-21
# ---------------------------------------------------------------------------


def test_bug17_fullscreen_refresh_skips_double_draw_and_disconnected_raf():
    """BUG-17: skipFullscreenDraw suppresses double draw, and disconnected rAF skips drawing."""
    node_script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

function assert(c, m) { if (!c) throw new Error(m); }

function extractFn(src, marker) {
  const idx = src.indexOf(marker);
  assert(idx !== -1, "Missing marker: " + marker);
  let i = src.indexOf("(", idx), p = 0, seen = false;
  for (; i < src.length; i++) {
    if (src[i] === "(") { p++; seen = true; }
    else if (src[i] === ")") { p--; if (seen && p === 0) break; }
  }
  let open = src.indexOf("{", i), d = 0;
  for (let j = open; j < src.length; j++) {
    if (src[j] === "{") d++;
    else if (src[j] === "}") { d--; if (d === 0) return src.slice(idx, j + 1); }
  }
  throw new Error("Unbalanced: " + marker);
}

const chartSrc = fs.readFileSync(path.join(process.cwd(), "static/js/chart.js"), "utf8");
const refreshCode = extractFn(chartSrc, "async function refreshStockChart(");
let fsCalls = 0;
let cardCalls = 0;
const chartCtx = {
  Date,
  Promise,
  window: { updateIntervalControlsVisibility: () => {} },
  getStockByKey: () => ({ symbol: "AAPL", market: "us" }),
  getChartPref: () => "auto",
  setChartPref: () => {},
  resolveEffectiveInterval: () => "1d",
  clearChartError: () => {},
  applyHistoryToStockAndWrapper: () => {},
  getFreshPrefetchedHistory: () => ({ formattedData: [{ x: 1, y: 10 }, { x: 2, y: 20 }], ohlcData: [] }),
  drawChart: () => { cardCalls++; },
  drawFullscreenChartIfActive: () => { fsCalls++; },
  drawPnLChart: () => {},
  document: { getElementById: () => null, querySelector: () => null },
};
vm.runInNewContext(refreshCode + "; this.refreshStockChart = refreshStockChart;", chartCtx);

(async () => {
  const wrapper = {
    isConnected: true,
    dataset: { stockKey: "us:AAPL", marketContext: "us" },
    __stockData: { symbol: "AAPL", market: "us" },
    querySelector: () => null,
  };
  await chartCtx.refreshStockChart(wrapper, "3mo", "auto", { skipFullscreenDraw: true });
  assert(cardCalls === 1, "expected 1 card draw");
  assert(fsCalls === 0, "expected 0 fullscreen draws when skipFullscreenDraw is true");
  await chartCtx.refreshStockChart(wrapper, "3mo", "auto");
  assert(fsCalls === 1, "expected 1 fullscreen draw without skipFullscreenDraw");

  // Also assert ui.js openFullscreenChart passes skipFullscreenDraw: true on both call sites
  const uiSrc = fs.readFileSync(path.join(process.cwd(), "static/js/ui.js"), "utf8");
  const matches = uiSrc.match(/skipFullscreenDraw:\s*true/g) || [];
  assert(matches.length >= 2, "ui.js must pass skipFullscreenDraw: true at toolbar and view-mode refresh sites");

  // Verify updateStockUI rAF respects wrapper.isConnected === false and canvas.isConnected === false
  const updateCardCode = extractFn(uiSrc, "function updateStockUI(");
  let rafCb = null;
  let pnlDrawCount = 0;
  const pnlCanvas = { isConnected: false };
  const cardEl = {
    isConnected: true,
    classList: { contains: (c) => c === "open" },
    dataset: { stockKey: "us:AAPL", marketContext: "portfolio", lastDataHash: "old" },
    querySelector: (sel) => {
      if (sel === ".detail-panel") return { classList: { contains: () => true } };
      if (sel === ".chart-container") return { classList: { contains: () => false } };
      if (sel === ".chart-canvas-pnl") return pnlCanvas;
      return null;
    },
    querySelectorAll: () => [],
  };
  const uiCtx = {
    Date,
    Number,
    Array,
    Map,
    requestAnimationFrame: (cb) => { rafCb = cb; },
    document: { getElementById: () => null, querySelector: () => null },
    stockRealtimeUpdateAt: new Map(),
    checkAlerts: () => {},
    updateOpenStockDetailDrawerHeader: () => {},
    hasFreshSparklineData: () => false,
    setSparklineVisibility: () => {},
    updatePortfolioInfoElements: () => {},
    isSparklineHidden: () => true,
    shouldUpdateSparkline: () => false,
    isElementInViewport: () => false,
    isStockDetailsCacheFresh: () => false,
    stockDetailsCache: new Map(),
    renderDetailExtras: () => {},
    triggerPriceFlash: () => {},
    formatStockChangeMetrics: () => ({ className: "", text: "", ariaLabel: "" }),
    getCurrencySymbol: () => "$",
    formatPrice: (p) => String(p),
    stockRenderHash: () => "new-hash",
    drawPnLChart: () => { pnlDrawCount++; },
    chartInstances: new Map(),
    getChartPref: () => "3mo",
  };
  vm.runInNewContext(updateCardCode + "; this.updateStockUI = updateStockUI;", uiCtx);
  uiCtx.updateStockUI(cardEl, { symbol: "AAPL", market: "us", price: 150, avg_price: 100, chart_data: [{ x: 1, y: 1 }] });
  assert(typeof rafCb === "function", "expected requestAnimationFrame callback");
  // Disconnected canvas must skip draw even if wrapper is still connected
  rafCb();
  assert(pnlDrawCount === 0, "disconnected pnlCanvas must skip drawing in rAF");
  // Connected canvas + disconnected wrapper must also skip
  pnlCanvas.isConnected = true;
  cardEl.isConnected = false;
  rafCb();
  assert(pnlDrawCount === 0, "disconnected wrapper must skip drawing in rAF");
  // Connected wrapper + connected canvas draws
  cardEl.isConnected = true;
  rafCb();
  assert(pnlDrawCount === 1, "connected wrapper and canvas must draw in rAF");
  console.log("ok");
})().catch((e) => { console.error(e); process.exit(1); });
"""
    _run_node_script(node_script)


def test_bug18_price_alert_zero_threshold_preserved_and_triggered():
    """BUG-18: Price threshold of 0 must restore in modal and trigger in checkAlerts."""
    node_script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

function assert(c, m) { if (!c) throw new Error(m); }

function extractFn(src, marker) {
  const idx = src.indexOf(marker);
  assert(idx !== -1, "Missing marker: " + marker);
  let i = src.indexOf("(", idx), p = 0, seen = false;
  for (; i < src.length; i++) {
    if (src[i] === "(") { p++; seen = true; }
    else if (src[i] === ")") { p--; if (seen && p === 0) break; }
  }
  let open = src.indexOf("{", i), d = 0;
  for (let j = open; j < src.length; j++) {
    if (src[j] === "{") d++;
    else if (src[j] === "}") { d--; if (d === 0) return src.slice(idx, j + 1); }
  }
  throw new Error("Unbalanced: " + marker);
}

const src = fs.readFileSync(path.join(process.cwd(), "static/js/index_main.js"), "utf8");
const openAlertCode = extractFn(src, "function openAlertModal(");
const checkAlertsCode = extractFn(src, "function checkAlerts(");

const inputs = {
  "alert-modal-symbol": { textContent: "" },
  "alert-price-up": { value: "stale" },
  "alert-price-down": { value: "stale" },
  "alert-ma-cross": { checked: false },
  "saveAlertBtn": {},
};
const alertsCfg = {
  "us:TEST": { priceUp: 0, priceDown: 0, maCross: false, triggeredUp: false, triggeredDown: false },
};
const toasts = [];
const ctx = {
  Number,
  window: {},
  DOM: { get: (id) => inputs[id] },
  getStockByKey: () => ({ symbol: "TEST", market: "us" }),
  makeStockKey: (m, s) => `${m}:${s}`,
  getAlertsConfig: () => alertsCfg,
  saveAlertsConfig: (next) => { Object.assign(alertsCfg, next); },
  openModal: (_id, cb) => cb(),
  showToast: (msg) => toasts.push(msg),
  showBrowserNotification: () => {},
};
vm.runInNewContext(
  openAlertCode + "\n" + checkAlertsCode + "\nthis.openAlertModal = openAlertModal; this.checkAlerts = checkAlerts;",
  ctx,
);

ctx.openAlertModal("us:TEST");
assert(inputs["alert-price-up"].value === 0, "expected 0 restored for priceUp, got " + inputs["alert-price-up"].value);
assert(inputs["alert-price-down"].value === 0, "expected 0 restored for priceDown, got " + inputs["alert-price-down"].value);

ctx.checkAlerts({ symbol: "TEST", market: "us", price: 0 }, 1);
assert(alertsCfg["us:TEST"].triggeredUp === true, "expected triggeredUp for priceUp=0");
assert(alertsCfg["us:TEST"].triggeredDown === true, "expected triggeredDown for priceDown=0");
assert(toasts.length === 2, "expected 2 alert toasts for thresholds at 0");
console.log("ok");
"""
    _run_node_script(node_script)


def test_bug19_ai_dive_news_polling_timeout_shows_error_not_empty_claim():
    """BUG-19: Exhausted news polling while data.fetching===true must show timeout error."""
    node_script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

function assert(c, m) { if (!c) throw new Error(m); }

const ctrlSrc = fs.readFileSync(
  path.join(process.cwd(), "static/js/experimental/ai-dive-controller.js"),
  "utf8",
);

function makeEl(tag) {
  let _text = "";
  return {
    tagName: tag,
    className: "",
    children: [],
    get textContent() { return _text; },
    set textContent(v) {
      _text = String(v);
      if (_text === "") this.children = [];
    },
    appendChild(c) { this.children.push(c); },
  };
}
const newsContent = makeEl("div");
const ctx = {
  window: {},
  AbortController,
  Error,
  Promise,
  JSON,
  document: { createElement: (t) => makeEl(t), getElementById: () => null },
  apiFetch: async () => ({
    response: { ok: true, status: 200 },
    data: { fetching: true, message: "取得中" },
  }),
};
ctx.window = ctx;
vm.runInNewContext(ctrlSrc, ctx);

const stateStore = {
  state: { aiDiveOpen: true, aiDiveSymbol: "AAPL" },
  subscribe: () => () => {},
  closeAiDive: () => {},
};
const controller = new ctx.AiDiveController(stateStore, { tier3Container: newsContent });
controller.waitForNewsRetry = async () => {};

(async () => {
  await controller.fetchAndRenderNews({ symbol: "AAPL", market: "us" });
  assert(newsContent.children.length === 1, "expected error message element");
  const rendered = newsContent.children[0].textContent;
  assert(!rendered.includes("市場ニュースはまだ利用できません"), "must not claim no news on timeout: " + rendered);
  assert(rendered.includes("ニュースの取得に失敗しました"), "expected timeout failure notice, got: " + rendered);
  console.log("ok");
})().catch((e) => { console.error(e); process.exit(1); });
"""
    _run_node_script(node_script)


def test_bug20_pts_delta_syncs_window_state_and_render_hash():
    """BUG-20: _applyPtsDeltasNow must sync window.state.stocks and dataset.lastDataHash."""
    node_script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

function assert(c, m) { if (!c) throw new Error(m); }

const rtSrc = fs.readFileSync(path.join(process.cwd(), "static/js/realtime_client.js"), "utf8");
const stateStock = { symbol: "7203.T", market: "jp", price: 3000, pts_price: null };
const wrapper = {
  dataset: { stockKey: "jp:7203.T", lastDataHash: "stale-hash" },
  __stockData: { symbol: "7203.T", market: "jp", price: 3000, pts_price: null },
};
const ptsEl = {
  textContent: "",
  hidden: true,
  closest: () => wrapper,
  setAttribute: () => {},
  removeAttribute: () => {},
};
const ctx = {
  Set,
  Array,
  Object,
  Number,
  Map,
  window: {
    state: { stocks: { us: [], jp: [stateStock], idx: [] } },
    formatPrice: (p) => "JPY " + p,
    stockRenderHash: (s) => `${s.price}|pts:${s.pts_price}`,
  },
  document: {
    hidden: true,
    readyState: "complete",
    addEventListener: () => {},
    querySelectorAll: () => [],
  },
  setTimeout,
  clearTimeout,
};
ctx.window.window = ctx.window;
vm.runInNewContext(rtSrc, ctx);

const client = new ctx.window.RealtimeStockClient();
client._getElements = () => ({
  bareSymbol: "7203.T",
  cleanCode: "7203",
  wrappers: [wrapper],
  ptsElements: [ptsEl],
});
client._applyPtsDeltasNow({ "7203": { price: 3055.5 } });

assert(ptsEl.textContent === "PTS JPY 3055.5", "unexpected PTS text: " + ptsEl.textContent);
assert(wrapper.__stockData.pts_price === 3055.5, "wrapper.__stockData.pts_price not synced");
assert(stateStock.pts_price === 3055.5, "window.state.stocks pts_price not synced");
assert(wrapper.dataset.lastDataHash === "3000|pts:3055.5", "lastDataHash not synced: " + wrapper.dataset.lastDataHash);
console.log("ok");
"""
    _run_node_script(node_script)


def test_bug21_stream_chat_reply_handles_crlf_sse_delimiters():
    """BUG-21: streamChatReply must parse CRLF (\\r\\n\\r\\n) frames even across chunk splits."""
    node_script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

function assert(c, m) { if (!c) throw new Error(m); }

function extractFn(src, marker) {
  const idx = src.indexOf(marker);
  assert(idx !== -1, "Missing marker: " + marker);
  let i = src.indexOf("(", idx), p = 0, seen = false;
  for (; i < src.length; i++) {
    if (src[i] === "(") { p++; seen = true; }
    else if (src[i] === ")") { p--; if (seen && p === 0) break; }
  }
  let open = src.indexOf("{", i), d = 0;
  for (let j = open; j < src.length; j++) {
    if (src[j] === "{") d++;
    else if (src[j] === "}") { d--; if (d === 0) return src.slice(idx, j + 1); }
  }
  throw new Error("Unbalanced: " + marker);
}

const apiSrc = fs.readFileSync(path.join(process.cwd(), "static/js/api.js"), "utf8");
const streamCode = extractFn(apiSrc, "async function streamChatReply(");
const enc = new TextEncoder();

function makeChunkReader(chunks) {
  let idx = 0;
  return {
    read: async () => (idx < chunks.length ? { done: false, value: enc.encode(chunks[idx++]) } : { done: true }),
    cancel: async () => {},
  };
}

let currentReader = null;
const ctx = {
  TextDecoder,
  JSON,
  Error,
  String,
  CHAT_STREAM_IDLE_TIMEOUT_MS: 30000,
  readStreamChunk: (reader) => reader.read(),
  csrfFetch: async () => ({
    ok: true,
    status: 200,
    headers: { get: (k) => (String(k).toLowerCase() === "content-type" ? "text/event-stream" : null) },
    body: { getReader: () => currentReader },
  }),
};
vm.runInNewContext(streamCode + "; this.streamChatReply = streamChatReply;", ctx);

(async () => {
  // Test CRLF split right at \r | \n\r\n boundary
  currentReader = makeChunkReader([
    'data: {"delta":"Hel"}\r',
    '\n\r\ndata: {"done":true,"reply":"Hello!"}\r\n\r\n',
  ]);
  const deltas = [];
  const reply = await ctx.streamChatReply(
    { symbol: "AAPL", message: "hi", request_token: "tok" },
    (p) => deltas.push(p),
  );
  assert(reply === "Hello!", "expected Hello!, got " + reply);
  assert(deltas[0] === "Hel", "expected delta callback");

  // Also ensure truncated CRLF stream without done frame falls back cleanly
  currentReader = makeChunkReader(['data: {"delta":"half"}\r\n\r\n']);
  let sawFallback = false;
  try {
    await ctx.streamChatReply({ symbol: "AAPL", message: "hi", request_token: "tok2" }, () => {});
  } catch (err) {
    sawFallback = Boolean(err && err.isStreamFallback);
  }
  assert(sawFallback, "truncated CRLF stream must throw isStreamFallback");
  console.log("ok");
})().catch((e) => { console.error(e); process.exit(1); });
"""
    _run_node_script(node_script)
