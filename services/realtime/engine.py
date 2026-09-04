# services/realtime/engine.py
"""Unified Realtime Market Engine (Producer-Consumer Queue & Delta Update Dispatcher)."""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager

from services.realtime.scrapers import (
    MinkabuScraper,
    Nikkei225JPScraper,
    SBISecuritiesScraper,
    YahooJPRealtimeScraper,
)
from services.realtime.tv_client import TradingViewWSClient
from services.realtime.utils import (
    PTS_CACHE_STALE_SECONDS,
    PTS_POLL_INTERVAL_ACTIVE,
    PTS_POLL_INTERVAL_IDLE,
    TickerPayload,
    _dedupe_pts_symbols,
    _get_yfinance_previous_close,
    _interruptible_sleep,
    _is_scraper_blocked,
    _is_yf_rate_limited,
    _normalize_tv_symbol,
    _scraper_market_state,
    _tv_purge_key_variants,
    is_pts_session,
)
from utils.threading import DaemonThreadPoolExecutor

logger = logging.getLogger(__name__)

PTS_JOIN_TIMEOUT_SEC: float = 10.0


class RealtimeMarketEngine:
    """Core Market Engine maintaining unified market state & dispatching SSE deltas."""

    def __init__(self) -> None:
        self.market_store: dict[str, TickerPayload] = {}
        self.previous_store: dict[str, TickerPayload] = {}
        self.pts_store: dict[str, TickerPayload] = {}
        self.previous_pts_store: dict[str, TickerPayload] = {}
        self.store_lock = threading.RLock()
        self._client_states: dict[str, dict[str, TickerPayload]] = {}
        self._client_pts_states: dict[str, dict[str, TickerPayload]] = {}
        self._client_events: dict[str, threading.Event] = {}
        self._client_last_seen: dict[str, float] = {}
        self._client_counter = 0
        self._last_stale_client_purge = time.time()
        self._dirty_symbols: set[str] = set()
        self._dirty_pts_symbols: set[str] = set()
        self._client_pending: dict[str, set[str]] = {}
        self._client_pts_pending: dict[str, set[str]] = {}

        self.sbi_scraper = SBISecuritiesScraper()
        self.nikkei225jp_scraper = Nikkei225JPScraper()
        self.minkabu_scraper = MinkabuScraper()
        self.yahoojp_scraper = YahooJPRealtimeScraper(
            on_update_callback=self._handle_producer_update,
            fallback_provider=self.sbi_scraper,
        )
        self.yahoojp_scraper.secondary_fallback_provider = self.nikkei225jp_scraper
        self.yahoojp_scraper.tertiary_fallback_provider = self.minkabu_scraper
        self.tv_client = TradingViewWSClient(on_update_callback=self._handle_producer_update)

        self._bg_executor = DaemonThreadPoolExecutor(max_workers=4, thread_name_prefix="RealtimeBg")

        self.running = False
        self._lifecycle_lock = threading.RLock()
        self.pts_thread: threading.Thread | None = None
        self._pts_epoch = 0
        self._registration_tokens: dict[tuple[str, str], object] = {}
        self._engine_epoch = 0

    def _notify_all_clients(self) -> None:
        """Wake up all active SSE client threads on incoming price updates."""
        with self.store_lock:
            evts = list(self._client_events.values())
        for evt in evts:
            evt.set()

    def _activate_registration(self, symbol: str, market: str) -> tuple[str, str, object, int]:
        token = object()
        with self.store_lock:
            self._registration_tokens[(market, symbol)] = token
            return market, symbol, token, self._engine_epoch

    def _invalidate_registration(self, symbol: str, market: str) -> None:
        with self.store_lock:
            self._registration_tokens.pop((market, symbol), None)

    def _registration_is_current_locked(self, registration: tuple[str, str, object, int]) -> bool:
        market, symbol, token, epoch = registration
        return (
            self._engine_epoch == epoch and self._registration_tokens.get((market, symbol)) is token
        )

    def _registration_is_current(self, registration: tuple[str, str, object, int]) -> bool:
        with self.store_lock:
            return self._registration_is_current_locked(registration)

    def _handle_producer_update(
        self,
        payload: TickerPayload,
        *,
        registration: tuple[str, str, object, int] | None = None,
    ) -> None:
        if registration is not None and not self._registration_is_current(registration):
            return
        symbol = payload["symbol"]
        price = payload.get("price")
        if (
            price is not None
            and isinstance(price, (int, float))
            and not isinstance(price, bool)
            and math.isfinite(price)
            and price > 0
        ):
            prev_close = _get_yfinance_previous_close(symbol)
            if prev_close and prev_close > 0:
                change = price - prev_close
                change_pct = (change / prev_close) * 100
                is_jpy = symbol.endswith(".T") or symbol.replace(".T", "").isdigit()
                decimals = 2 if is_jpy else 4
                payload["change"] = round(change, decimals)
                payload["change_percent"] = round(change_pct, 2)
                payload["previous_close"] = prev_close
                try:
                    from app_state import app_state

                    app_state.market.update_previous_close_cache(symbol, prev_close)
                except Exception as exc:
                    logger.debug("Failed updating previous_close cache for %s: %s", symbol, exc)

        with self.store_lock:
            if registration is not None and not self._registration_is_current_locked(registration):
                return
            self.market_store[symbol] = payload
            self._dirty_symbols.add(symbol)
            for pending in self._client_pending.values():
                pending.add(symbol)
            self._notify_all_clients()

    def _handle_pts_update(
        self,
        payload: TickerPayload,
        *,
        registration: tuple[str, str, object, int] | None = None,
    ) -> None:
        symbol = payload["symbol"]
        with self.store_lock:
            if registration is not None and not self._registration_is_current_locked(registration):
                return
            self.pts_store[symbol] = payload
            self._dirty_pts_symbols.add(symbol)
            for pending in self._client_pts_pending.values():
                pending.add(symbol)
            self._notify_all_clients()

    def _purge_stale_clients(self, ttl_seconds: float = 120.0) -> None:
        """Purge client cursors that have been inactive for > 2 minutes."""
        now = time.time()
        with self.store_lock:
            stale_ids = [
                cid
                for cid, last_seen in self._client_last_seen.items()
                if (now - last_seen) > ttl_seconds
            ]
            for cid in stale_ids:
                self._client_states.pop(cid, None)
                self._client_pts_states.pop(cid, None)
                self._client_pending.pop(cid, None)
                self._client_pts_pending.pop(cid, None)
                evt = self._client_events.pop(cid, None)
                if evt:
                    evt.set()
                self._client_last_seen.pop(cid, None)
                logger.debug("[Realtime Engine] Purged inactive client cursor id=%s", cid)

    def register_client(self) -> str:
        """Register an SSE delta consumer and return its cursor id."""
        with self.store_lock:
            self._purge_stale_clients()
            self._client_counter += 1
            client_id = f"client_{self._client_counter}"
            self._client_states[client_id] = {
                sym: dict(payload) for sym, payload in self.market_store.items()
            }
            self._client_pts_states[client_id] = {
                sym: dict(payload) for sym, payload in self.pts_store.items()
            }
            self._client_pending[client_id] = set()
            self._client_pts_pending[client_id] = set()
            evt = threading.Event()
            evt.set()
            self._client_events[client_id] = evt
            self._client_last_seen[client_id] = time.time()
            return client_id

    def unregister_client(self, client_id: str) -> None:
        """Drop a client's delta cursors (stream closed / disconnected)."""
        with self.store_lock:
            self._client_states.pop(client_id, None)
            self._client_pts_states.pop(client_id, None)
            self._client_pending.pop(client_id, None)
            self._client_pts_pending.pop(client_id, None)
            evt = self._client_events.pop(client_id, None)
            if evt:
                evt.set()
            self._client_last_seen.pop(client_id, None)

    @contextmanager
    def client_context(self) -> Generator[str, None, None]:
        """Context manager that registers an SSE client and guarantees unregistration on exit."""
        cid = self.register_client()
        try:
            yield cid
        finally:
            self.unregister_client(cid)

    def wait_for_updates(self, client_id: str, timeout: float = 0.5) -> bool:
        """Wait for delta updates on the specified client's event handle."""
        with self.store_lock:
            evt = self._client_events.get(client_id)
            if evt is None:
                # Purged / unknown client. Never sleep while holding store_lock:
                # every producer update and other clients' delta reads serialize
                # on it, so a zombie client here would halve the whole
                # realtime pipeline's throughput.
                pass
            elif evt.is_set():
                evt.clear()
                return True
            else:
                signaled = evt.wait(timeout)
                if signaled:
                    with self.store_lock:
                        if evt.is_set():
                            evt.clear()
                            return True
                return signaled
        time.sleep(timeout)
        return False

    def register_symbols(self, tv_symbols: list[str], jp_symbols: list[str]) -> None:
        """Register US / Index / ETF symbols for TV and JP symbols for Yahoo JP."""
        for sym in tv_symbols:
            self.tv_client.add_symbol(_normalize_tv_symbol(sym))
        for sym in jp_symbols:
            self.yahoojp_scraper.add_symbol(sym)
            registration = self._activate_registration(sym, "jp")
            if self._pts_cached_payload(sym) is None:

                def _bg_fetch(
                    target_sym: str = sym,
                    current_registration: tuple[str, str, object, int] = registration,
                ) -> None:
                    try:
                        pts_payload = self._fetch_pts_with_fallback(target_sym)
                        if pts_payload:
                            self._handle_pts_update(pts_payload, registration=current_registration)
                    except Exception as e:
                        logger.debug("Background PTS fetch failed for %s: %s", target_sym, e)

                try:
                    self._bg_executor.submit(_bg_fetch)
                except (RuntimeError, Exception) as exc:
                    logger.debug(
                        "Background PTS fetch could not be submitted for %s: %s",
                        sym,
                        exc,
                    )

    def register_symbol(self, symbol: str, market: str) -> None:
        """Register a single symbol for realtime updates (incremental)."""
        with self.store_lock:
            if (market, symbol) in self._registration_tokens:
                return
            registration = self._activate_registration(symbol, market)
        if market == "us":
            self.tv_client.add_symbol(_normalize_tv_symbol(symbol))
        elif market == "jp":
            self.yahoojp_scraper.add_symbol(symbol)

            def _priority_fetch(
                current_registration: tuple[str, str, object, int] = registration,
            ) -> None:
                try:
                    payload = self.yahoojp_scraper._fetch_regular_with_fallback(symbol)
                    if payload:
                        self._handle_producer_update(payload, registration=current_registration)
                    if not self._registration_is_current(current_registration):
                        return
                    if is_pts_session() or self._pts_cached_payload(symbol) is None:
                        pts_payload = self._fetch_pts_with_fallback(symbol)
                        if pts_payload:
                            self._handle_pts_update(pts_payload, registration=current_registration)
                except Exception as e:
                    logger.debug("Priority fetch failed for %s: %s", symbol, e)

            try:
                self._bg_executor.submit(_priority_fetch)
            except (RuntimeError, Exception) as exc:
                logger.debug(
                    "Priority fetch could not be submitted for %s: %s",
                    symbol,
                    exc,
                )

    def unregister_symbol(self, symbol: str, market: str) -> None:
        """Unregister a symbol and purge its stored quote state (incl. PTS)."""
        self._invalidate_registration(symbol, market)
        if market == "us":
            self.tv_client.remove_symbol(_normalize_tv_symbol(symbol))
        elif market == "jp":
            self.yahoojp_scraper.remove_symbol(symbol)
        purge_keys = set(_tv_purge_key_variants(symbol))
        with self.store_lock:
            for key in list(self.market_store):
                if key in purge_keys:
                    self.market_store.pop(key, None)
                    self.previous_store.pop(key, None)
                    self._dirty_symbols.discard(key)
                    for client_state in self._client_states.values():
                        client_state.pop(key, None)
                    for client_pending in self._client_pending.values():
                        client_pending.discard(key)
            for pkey in purge_keys:
                self.pts_store.pop(pkey, None)
                self.previous_pts_store.pop(pkey, None)
                self._dirty_pts_symbols.discard(pkey)
                for client_state in self._client_pts_states.values():
                    client_state.pop(pkey, None)
                for client_pending in self._client_pts_pending.values():
                    client_pending.discard(pkey)

    def get_market_snapshot(self, client_id: str | None = None) -> dict[str, TickerPayload]:
        """Return a copy of the current unified market snapshot."""
        with self.store_lock:
            if client_id is not None and client_id in self._client_last_seen:
                self._client_last_seen[client_id] = time.time()
            return dict(self.market_store)

    def get_pts_snapshot(self) -> dict[str, TickerPayload]:
        """Return a copy of the current PTS quote snapshot."""
        with self.store_lock:
            return dict(self.pts_store)

    def get_market_deltas(self, client_id: str | None = None) -> dict[str, TickerPayload]:
        """Return symbols changed since the given client's last check."""
        deltas: dict[str, TickerPayload] = {}
        prev_store: dict[str, TickerPayload]
        pending: set[str]
        with self.store_lock:
            if client_id is not None:
                client_prev = self._client_states.get(client_id)
                client_pending = self._client_pending.get(client_id)
                if client_prev is None or client_pending is None:
                    return {}
                prev_store = client_prev
                pending = client_pending
            else:
                prev_store = self.previous_store
                pending = self._dirty_symbols
            if not prev_store:
                for sym, current in self.market_store.items():
                    deltas[sym] = current
                    prev_store[sym] = dict(current)
                pending.clear()
            else:
                for sym in list(pending):
                    cur = self.market_store.get(sym)
                    if cur is None:
                        pending.discard(sym)
                        continue
                    prev = prev_store.get(sym)
                    if (
                        not prev
                        or prev.get("price") != cur.get("price")
                        or prev.get("change") != cur.get("change")
                        or prev.get("change_percent") != cur.get("change_percent")
                        or prev.get("volume") != cur.get("volume")
                    ):
                        deltas[sym] = cur
                        prev_store[sym] = dict(cur)
                    pending.discard(sym)
        if deltas:
            logger.debug(
                "[Realtime Engine] Market deltas generated for %d symbol(s): %s",
                len(deltas),
                list(deltas.keys()),
            )
        return deltas

    def get_pts_deltas(self, client_id: str | None = None) -> dict[str, TickerPayload]:
        """Return changed PTS quotes since the given client's last check."""
        deltas: dict[str, TickerPayload] = {}
        prev_store: dict[str, TickerPayload]
        pending: set[str]
        with self.store_lock:
            if client_id is not None:
                client_prev = self._client_pts_states.get(client_id)
                client_pending = self._client_pts_pending.get(client_id)
                if client_prev is None or client_pending is None:
                    return {}
                prev_store = client_prev
                pending = client_pending
            else:
                prev_store = self.previous_pts_store
                pending = self._dirty_pts_symbols
            if not prev_store:
                for sym, current in self.pts_store.items():
                    deltas[sym] = current
                    prev_store[sym] = dict(current)
                pending.clear()
            else:
                for sym in list(pending):
                    cur = self.pts_store.get(sym)
                    if cur is None:
                        pending.discard(sym)
                        continue
                    prev = prev_store.get(sym)
                    if (
                        not prev
                        or prev.get("price") != cur.get("price")
                        or prev.get("volume") != cur.get("volume")
                        or prev.get("change") != cur.get("change")
                        or prev.get("pts_trading") != cur.get("pts_trading")
                    ):
                        deltas[sym] = cur
                        prev_store[sym] = dict(cur)
                    pending.discard(sym)
        if deltas:
            logger.debug(
                "[Realtime Engine] PTS deltas generated for %d symbol(s): %s",
                len(deltas),
                list(deltas.keys()),
            )
        return deltas

    def _pts_cached_payload(self, symbol: str) -> TickerPayload | None:
        """Return the cached PTS quote for *symbol* (any key form), or None."""
        clean_sym = symbol.replace(".T", "").replace(".t", "")
        with self.store_lock:
            return (
                self.pts_store.get(symbol)
                or self.pts_store.get(f"{clean_sym}.T")
                or self.pts_store.get(clean_sym)
            )

    def _fetch_pts_with_fallback(self, symbol: str) -> TickerPayload | None:
        """Fetch a PTS quote: Yahoo JP first, then SBI, then Nikkei225JP, then Minkabu as lowest fallback."""
        payload = self.yahoojp_scraper.fetch_pts_symbol(symbol)
        if not payload:
            try:
                payload = self.sbi_scraper.fetch_pts_quote(symbol)
                if payload:
                    logger.debug("[Realtime Engine] SBI PTS fallback quote for %s", symbol)
            except Exception as exc:
                logger.debug("SBI PTS fallback failed for %s: %s", symbol, exc)
        if not payload:
            try:
                payload = self.nikkei225jp_scraper.fetch_pts_quote(symbol)
                if payload:
                    logger.debug("[Realtime Engine] Nikkei225JP PTS fallback quote for %s", symbol)
            except Exception as exc:
                logger.debug("Nikkei225JP PTS fallback failed for %s: %s", symbol, exc)
        if not payload:
            try:
                payload = self.minkabu_scraper.fetch_pts_quote(symbol)
                if payload:
                    logger.debug("[Realtime Engine] Minkabu PTS fallback quote for %s", symbol)
            except Exception as exc:
                logger.debug("Minkabu PTS fallback failed for %s: %s", symbol, exc)
        return payload

    def _pts_worker_loop(self) -> None:
        my_epoch = self._pts_epoch
        while self.running and self._pts_epoch == my_epoch:
            try:
                now_ts = time.time()
                if (now_ts - self._last_stale_client_purge) > 60.0:
                    self._last_stale_client_purge = now_ts
                    self._purge_stale_clients()

                if not self.yahoojp_scraper._is_startup_ready():
                    _interruptible_sleep(lambda: self.running and self._pts_epoch == my_epoch, 1.0)
                    continue

                if _is_scraper_blocked() or _is_yf_rate_limited():
                    market = _scraper_market_state()
                    remains = (
                        market.scraper_block_clears_in()
                        if market and hasattr(market, "scraper_block_clears_in")
                        else 2.0
                    )
                    sleep_time = max(2.0, min(remains, 5.0)) if remains > 0 else 2.0
                    _interruptible_sleep(
                        lambda: self.running and self._pts_epoch == my_epoch, sleep_time
                    )
                    continue

                active = is_pts_session()
                interval = PTS_POLL_INTERVAL_ACTIVE if active else PTS_POLL_INTERVAL_IDLE

                with self.yahoojp_scraper.lock:
                    scraper_symbols = list(self.yahoojp_scraper.symbols)

                user_jp_symbols: set[str] = set()
                try:
                    from app_state import app_state

                    if hasattr(app_state, "market") and app_state.market is not None:
                        with app_state.market.user_stocks_lock:
                            user_jp_symbols = set(app_state.market.user_jp.keys())
                except Exception as exc:
                    logger.warning(
                        "[Realtime Engine] Failed to read user_jp symbols for PTS polling: %s",
                        exc,
                    )

                target_symbols = _dedupe_pts_symbols(scraper_symbols, user_jp_symbols)
                target_symbols = self.yahoojp_scraper._active_symbols(target_symbols, kind="pts")

                now_ts = time.time()
                for sym in target_symbols:
                    if not self.running:
                        break
                    cached_payload = self._pts_cached_payload(sym)
                    is_stale = (
                        cached_payload is not None
                        and (now_ts - cached_payload.get("updated_at", 0.0))
                        > PTS_CACHE_STALE_SECONDS
                    )
                    if active or cached_payload is None or is_stale:
                        payload = self._fetch_pts_with_fallback(sym)
                        if (
                            payload
                            and self.running
                            and self._pts_epoch == my_epoch
                            and self.yahoojp_scraper._is_symbol_current(sym)
                        ):
                            self._handle_pts_update(payload)
                        from constants import SCRAPER_REQUEST_STAGGER_SEC

                        time.sleep(SCRAPER_REQUEST_STAGGER_SEC)

                _interruptible_sleep(lambda: self.running and self._pts_epoch == my_epoch, interval)
            except Exception as exc:
                logger.error("[Realtime Engine] PTS worker loop error: %s", exc)
                _interruptible_sleep(lambda: self.running and self._pts_epoch == my_epoch, 2.0)

    def worker_threads(self) -> list[threading.Thread]:
        """Return the engine's internal producer threads (watchdog target)."""
        threads: list[threading.Thread] = []
        if self.tv_client.thread is not None:
            threads.append(self.tv_client.thread)
        if self.yahoojp_scraper.thread is not None:
            threads.append(self.yahoojp_scraper.thread)
        if self.pts_thread is not None:
            threads.append(self.pts_thread)
        return threads

    def restart(self) -> None:
        """Stop and restart the engine producers (crash recovery)."""
        with self._lifecycle_lock:
            try:
                self.stop()
            except Exception as exc:
                logger.warning("Realtime engine stop during restart failed: %s", exc)
            if (
                self.pts_thread is not None
                and self.pts_thread is not threading.current_thread()
                and self.pts_thread.is_alive()
            ):
                self.pts_thread.join(timeout=PTS_JOIN_TIMEOUT_SEC)
            if self.pts_thread is not None and self.pts_thread.is_alive():
                logger.warning(
                    "Realtime engine restart deferred: previous PTS worker is still running"
                )
                return
            time.sleep(1.0)
            try:
                self.start()
            except Exception as exc:
                logger.warning("Realtime engine restart failed: %s", exc)

    def start(self) -> None:
        with self._lifecycle_lock:
            if not self.running:
                if self.pts_thread is not None and self.pts_thread.is_alive():
                    raise RuntimeError("previous PTS worker is still running")
                self.running = True
                logger.info("Starting RealtimeMarketEngine producers...")
                if self._bg_executor is None or getattr(self._bg_executor, "_shutdown", True):
                    self._bg_executor = DaemonThreadPoolExecutor(
                        max_workers=4, thread_name_prefix="RealtimeBg"
                    )
                self.tv_client.start()
                self.yahoojp_scraper.start()
                self._pts_epoch += 1
                self.pts_thread = threading.Thread(
                    target=self._pts_worker_loop, daemon=True, name="JPPTSWorker"
                )
                self.pts_thread.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            with self.store_lock:
                self._engine_epoch += 1
            self.running = False
            self._pts_epoch += 1
            if (
                self.pts_thread is not None
                and self.pts_thread is not threading.current_thread()
                and self.pts_thread.is_alive()
            ):
                self.pts_thread.join(timeout=5.0)
            if self.pts_thread is not None and not self.pts_thread.is_alive():
                self.pts_thread = None
            self.tv_client.stop()
            self.yahoojp_scraper.stop()
            self.yahoojp_scraper.close()
            self.sbi_scraper.close()
            self.nikkei225jp_scraper.close()
            self.minkabu_scraper.close()
            try:
                self._bg_executor.shutdown(wait=False, cancel_futures=True)
            except Exception as exc:
                logger.debug("Failed shutting down realtime bg executor: %s", exc)
