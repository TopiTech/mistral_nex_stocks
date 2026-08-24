# services/realtime/tv_client.py
"""TradingView WebSocket client implementing TV message framing (~m~len~m~payload)."""

from __future__ import annotations

import json
import logging
import math
import secrets
import string
import threading
import time
from collections.abc import Callable
from typing import Any

from services.realtime.utils import (
    TickerPayload,
    _interruptible_sleep,
    websocket,
)

logger = logging.getLogger(__name__)


def _get_rt_attr(name: str, fallback: Any) -> Any:
    import sys
    rt_mod = sys.modules.get("services.realtime_engine")
    if rt_mod is not None and name in rt_mod.__dict__:
        return rt_mod.__dict__[name]
    return fallback


def _get_logger() -> Any:
    return _get_rt_attr("logger", logger)


class TradingViewWSClient:
    """TradingView WebSocket client implementing TV message framing (~m~len~m~payload)."""

    WS_URL = "wss://data.tradingview.com/socket.io/websocket"
    ORIGIN = "https://data.tradingview.com"
    STOP_JOIN_TIMEOUT_SEC = 2.0

    def __init__(
        self,
        symbols: list[str] | None = None,
        on_update_callback: Callable[[TickerPayload], None] | None = None,
    ) -> None:
        self.symbols: set[str] = set(symbols or [])
        self.on_update_callback = on_update_callback
        self.session_id = "qs_" + "".join(
            secrets.choice(string.ascii_lowercase) for _ in range(12)
        )
        self.ws: Any = None
        self.running = False
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self._subscriptions_managed = bool(self.symbols)
        self._lifecycle_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._worker_epoch = 0
        self._last_quotes: dict[str, TickerPayload] = {}
        self.connected = False
        self.last_connected_at = 0.0

    def _safe_send(self, ws: Any, message: str) -> bool:
        """Thread-safely send a message over WebSocket socket."""
        if ws is None:
            return False
        with self._send_lock:
            try:
                ws.send(message)
                return True
            except Exception as exc:
                logger.debug("Failed sending WS frame: %s", exc)
                return False

    def _is_worker_current(self, epoch: int) -> bool:
        with self._lifecycle_lock:
            return self.running and epoch == self._worker_epoch

    @staticmethod
    def format_tv_message(func: str, args: list[Any]) -> str:
        """Wrap payload in ~m~len~m~ TradingView framing."""
        payload = json.dumps({"m": func, "p": args}, separators=(",", ":"))
        return f"~m~{len(payload)}~m~{payload}"

    @staticmethod
    def parse_tv_messages(raw: str) -> list[dict[str, Any]]:
        """Parse concatenated ~m~len~m~json messages from raw WS stream."""
        results = []
        pos = 0
        raw_len = len(raw)
        while pos < raw_len:
            start_m = raw.find("~m~", pos)
            if start_m == -1:
                break
            end_m = raw.find("~m~", start_m + 3)
            if end_m == -1:
                break
            len_str = raw[start_m + 3 : end_m]
            if not len_str.isdigit():
                pos = end_m + 3
                continue
            length = int(len_str)
            start_body = end_m + 3
            end_body = start_body + length
            if end_body <= raw_len:
                msg_body = raw[start_body:end_body]
                try:
                    results.append(json.loads(msg_body))
                except Exception:
                    logger.debug("Failed to parse TV json body: %s", msg_body)
                pos = end_body
            else:
                break
        return results

    def add_symbol(self, symbol: str) -> None:
        with self.lock:
            self._subscriptions_managed = True
            if symbol not in self.symbols:
                self.symbols.add(symbol)
                ws_to_send = None
                session_id = None
                can_send = False
                if self._lifecycle_lock.acquire(timeout=1.0):
                    try:
                        can_send = self.ws is not None and self.running and self.connected
                        ws_to_send = self.ws
                        session_id = self.session_id
                    finally:
                        self._lifecycle_lock.release()
                if can_send and ws_to_send:
                    msg = self.format_tv_message("quote_add_symbols", [session_id, symbol])
                    self._safe_send(ws_to_send, msg)

    def remove_symbol(self, symbol: str) -> None:
        with self.lock:
            self._subscriptions_managed = True
            if symbol in self.symbols:
                self.symbols.remove(symbol)
                self._last_quotes.pop(symbol, None)
                ws_to_send = None
                session_id = None
                can_send = False
                if self._lifecycle_lock.acquire(timeout=1.0):
                    try:
                        can_send = self.ws is not None and self.running and self.connected
                        ws_to_send = self.ws
                        session_id = self.session_id
                    finally:
                        self._lifecycle_lock.release()
                if can_send and ws_to_send:
                    msg = self.format_tv_message("quote_remove_symbols", [session_id, symbol])
                    self._safe_send(ws_to_send, msg)

    def _on_message(self, ws: Any, message: str) -> None:
        pos = 0
        raw_len = len(message)
        has_hb = False
        hb_replies: list[str] = []

        while pos < raw_len:
            start_m = message.find("~m~", pos)
            if start_m == -1:
                break
            end_m = message.find("~m~", start_m + 3)
            if end_m == -1:
                break
            len_str = message[start_m + 3 : end_m]
            if not len_str.isdigit():
                pos = end_m + 3
                continue
            length = int(len_str)
            start_body = end_m + 3
            end_body = start_body + length
            if end_body <= raw_len:
                msg_body = message[start_body:end_body]
                if msg_body.startswith("~h~"):
                    has_hb = True
                    hb_replies.append(f"~m~{len(msg_body)}~m~{msg_body}")
                pos = end_body
            else:
                break

        if has_hb:
            for hb_reply in hb_replies:
                self._safe_send(ws, hb_reply)

        parsed_list = self.parse_tv_messages(message)
        for msg in parsed_list:
            if not isinstance(msg, dict):
                continue
            m_type = msg.get("m")
            p_args = msg.get("p")
            if m_type == "qsd" and isinstance(p_args, list) and len(p_args) >= 2:
                qsd_data = p_args[1]
                if isinstance(qsd_data, dict):
                    symbol = qsd_data.get("n")
                    values = qsd_data.get("v", {})
                    if not symbol or not values or not self.on_update_callback:
                        continue

                    with self.lock:
                        if self._subscriptions_managed and symbol not in self.symbols:
                            continue
                        prev_quote = dict(self._last_quotes.get(symbol, {}))
                    price = prev_quote.get("price")
                    change = prev_quote.get("change", 0.0)
                    change_percent = prev_quote.get("change_percent", 0.0)
                    volume = prev_quote.get("volume", 0)

                    if "lp" in values and values["lp"] is not None:
                        try:
                            p_val = float(values["lp"])
                            if math.isfinite(p_val) and p_val > 0:
                                price = p_val
                        except (TypeError, ValueError):
                            pass

                    if "ch" in values and values["ch"] is not None:
                        try:
                            c_val = float(values["ch"])
                            if math.isfinite(c_val):
                                change = c_val
                        except (TypeError, ValueError):
                            pass

                    if "chp" in values and values["chp"] is not None:
                        try:
                            cp_val = float(values["chp"])
                            if math.isfinite(cp_val):
                                change_percent = cp_val
                        except (TypeError, ValueError):
                            pass

                    if "volume" in values and values["volume"] is not None:
                        try:
                            v_val = int(float(values["volume"]))
                            if v_val >= 0:
                                volume = v_val
                        except (TypeError, ValueError):
                            pass

                    if price is None or not math.isfinite(price) or price <= 0:
                        continue

                    payload: TickerPayload = {
                        "symbol": symbol,
                        "price": price,
                        "change": change,
                        "change_percent": change_percent,
                        "volume": volume,
                        "source": "tradingview",
                        "updated_at": time.time(),
                    }
                    with self.lock:
                        self._last_quotes[symbol] = payload.copy()
                    logger.debug(
                        "[TradingView WS] Realtime quote update for %s: price=%.2f, change=%.2f (source=tradingview)",
                        symbol,
                        payload["price"],
                        payload["change"],
                    )
                    self.on_update_callback(payload.copy())

                    if ":" in symbol:
                        bare_sym = symbol.split(":")[-1]
                        bare_payload = dict(payload)
                        bare_payload["symbol"] = bare_sym
                        self.on_update_callback(bare_payload)
                        if "." in bare_sym:
                            dash_payload = dict(payload)
                            dash_payload["symbol"] = bare_sym.replace(".", "-")
                            self.on_update_callback(dash_payload)

    def _on_ws_error(self, ws: Any, err: Any) -> None:
        self.connected = False
        err_str = str(err)
        if (
            "opcode=8" in err_str
            or "0x03e8" in err_str
            or "goodbye" in err_str.lower()
            or "1000" in err_str
        ):
            _get_logger().info("TradingView WS clean close frame received: %s", err)
        else:
            _get_logger().info("TradingView WS notice: %s", err)

    def _on_ws_close(self, ws: Any, close_status_code: Any, close_msg: Any) -> None:
        self.connected = False
        logger.info(
            "TradingView WS closed (status=%s msg=%s)", close_status_code, close_msg
        )

    def _run_ws(self, epoch: int) -> None:
        import sys
        backoff = 1.0
        worker_thread = threading.current_thread()
        try:
            while self._is_worker_current(epoch):
                from app_state import app_state

                ws_mod = getattr(sys.modules.get("services.realtime_engine"), "websocket", websocket)
                if ws_mod is None:
                    logger.info("websocket-client not available. TV WS worker sleeping...")
                    if app_state.execution.shutdown_event.wait(10.0):
                        break
                    continue

                ws_app: Any = None
                try:
                    session_id = "qs_" + "".join(
                        secrets.choice(string.ascii_lowercase) for _ in range(12)
                    )

                    logger.info(
                        "Connecting to TradingView WS (%d subscribed symbol(s))...",
                        len(self.symbols),
                    )

                    def _on_message_current(ws: Any, message: Any) -> None:
                        if self._is_worker_current(epoch):
                            self._on_message(ws, message)

                    def _on_error_current(ws: Any, err: Any) -> None:
                        if self._is_worker_current(epoch):
                            self._on_ws_error(ws, err)

                    def _on_close_current(ws: Any, status: Any, message: Any) -> None:
                        if self._is_worker_current(epoch):
                            self._on_ws_close(ws, status, message)

                    ws_app = ws_mod.WebSocketApp(
                        self.WS_URL,
                        header={"Origin": self.ORIGIN},
                        on_message=_on_message_current,
                        on_error=_on_error_current,
                        on_close=_on_close_current,
                    )
                    with self._lifecycle_lock:
                        if not self.running or epoch != self._worker_epoch:
                            return
                        self.session_id = session_id
                        self.ws = ws_app

                    def _on_open(ws: Any, current_session_id: str = session_id) -> None:
                        nonlocal backoff
                        with self._lifecycle_lock:
                            is_current = self.running and epoch == self._worker_epoch
                            if is_current:
                                self.connected = True
                                backoff = 1.0
                                self.last_connected_at = time.time()
                        if not is_current:
                            try:
                                ws.close()
                            except Exception as exc:
                                logger.debug("Failed closing stale TradingView WS: %s", exc)
                            return
                        logger.info(
                            "TradingView WS connected (session=%s, symbols=%d)",
                            current_session_id,
                            len(self.symbols),
                        )
                        self._safe_send(
                            ws,
                            self.format_tv_message(
                                "set_auth_token", ["unauthorized_user_token"]
                            ),
                        )
                        self._safe_send(
                            ws,
                            self.format_tv_message(
                                "quote_create_session", [current_session_id]
                            ),
                        )
                        self._safe_send(
                            ws,
                            self.format_tv_message(
                                "quote_set_fields",
                                [
                                    current_session_id,
                                    "lp",
                                    "ch",
                                    "chp",
                                    "volume",
                                    "ask",
                                    "bid",
                                    "description",
                                ],
                            ),
                        )
                        with self.lock:
                            sym_list = list(self.symbols)
                        for sym in sym_list:
                            if not self._is_worker_current(epoch):
                                break
                            self._safe_send(
                                ws,
                                self.format_tv_message(
                                    "quote_add_symbols", [current_session_id, sym]
                                ),
                            )

                    ws_app.on_open = _on_open
                    ws_app.run_forever(ping_interval=20, ping_timeout=10)
                except Exception as exc:
                    if self._is_worker_current(epoch):
                        logger.info("TradingView WS Exception: %s", exc)

                if not self._is_worker_current(epoch):
                    break
                self.connected = False
                with self._lifecycle_lock:
                    if self.ws is ws_app:
                        self.ws = None
                logger.info("Reconnecting TradingView WS in %.1f seconds...", backoff)
                sleep_fn = _get_rt_attr("_interruptible_sleep", _interruptible_sleep)
                sleep_fn(lambda: self._is_worker_current(epoch), backoff)
                backoff = min(backoff * 1.5, 10.0)
        finally:
            restart_pending = False
            restart_epoch = 0
            with self._lifecycle_lock:
                if self.thread is worker_thread:
                    self.thread = None
                    restart_pending = self.running and epoch != self._worker_epoch
                    restart_epoch = self._worker_epoch
                if epoch == self._worker_epoch:
                    self.running = False
                    self.connected = False
            if restart_pending:
                replacement: threading.Thread | None = None
                with self._lifecycle_lock:
                    if (
                        self.running
                        and self._worker_epoch == restart_epoch
                        and self.thread is None
                    ):
                        self._worker_epoch += 1
                        replacement_epoch = self._worker_epoch
                        replacement = threading.Thread(
                            target=self._run_ws,
                            args=(replacement_epoch,),
                            daemon=True,
                            name="TradingViewWSWorker",
                        )
                        self.thread = replacement
                        replacement.start()

    def start(self) -> None:
        with self._lifecycle_lock:
            if self.thread is not None and self.thread.is_alive():
                self.running = True
                return
            self.running = True
            self._worker_epoch += 1
            epoch = self._worker_epoch
            worker = threading.Thread(
                target=self._run_ws,
                args=(epoch,),
                daemon=True,
                name="TradingViewWSWorker",
            )
            self.thread = worker
            worker.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            self.running = False
            self._worker_epoch += 1
            ws_app = self.ws
            worker = self.thread
            self.ws = None
        if ws_app:
            try:
                ws_app.close()
                sock = getattr(ws_app, "sock", None)
                if sock is not None:
                    try:
                        sock.close()
                    except (OSError, AttributeError, ValueError) as exc:
                        logger.debug("Failed closing TradingView WS socket: %s", exc)
            except Exception as exc:
                logger.debug("Failed closing TradingView WS connection: %s", exc)
        if (
            worker is not None
            and worker is not threading.current_thread()
            and worker.is_alive()
        ):
            worker.join(timeout=self.STOP_JOIN_TIMEOUT_SEC)
        with self._lifecycle_lock:
            if self.thread is worker and (worker is None or not worker.is_alive()):
                self.thread = None
            self.connected = False
