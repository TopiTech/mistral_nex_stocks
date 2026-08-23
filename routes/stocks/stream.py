# routes/stocks/stream.py
"""Server-Sent Events (SSE) streaming and connection ticket endpoints."""

from __future__ import annotations

import json
import logging
import os
import queue
import time
from collections.abc import Iterator
from contextlib import nullcontext
from typing import Any

from flask import (
    Blueprint,
    Response,
    current_app,
    g,
    jsonify,
    request,
    stream_with_context,
)

from app_state import app_state
from constants import (
    MAX_SSE_LISTENERS,
    SSE_HEARTBEAT_INTERVAL,
    SSE_MODE2_FULL_SNAPSHOT_INTERVAL_SEC,
)
from error_codes import ErrorCode
from messaging import sse_event_log
from route_helpers import rate_limit
from routes.stocks.common import (
    _json_safe,
    _parse_last_event_id,
    _replay_frame_for_entry,
    is_market_open,
    require_sse_auth,
    require_trusted_or_admin,
    resolve_indices_for_response,
    resolve_stocks_for_response,
)
from services.realtime_engine import realtime_market_engine
from utils.networking import (
    SSE_TICKET_TTL_SEC,
    SseTicketSessionUnavailable,
    create_sse_ticket,
)
from utils.stock_payload import (
    error_response,
)
from utils.tradingview_mapper import (
    get_tradingview_symbol,
    get_tradingview_ticker_tape_symbols,
)

logger = logging.getLogger(__name__)

stream_bp = Blueprint("stream", __name__)


@stream_bp.route("/api/stocks/stream/ticket", methods=["POST"])
@rate_limit(max_requests=30, window_seconds=60)
def api_create_sse_ticket() -> Any:
    """Issue a short-lived SSE connection ticket for browser clients."""
    ok, reason = require_trusted_or_admin(request, require_origin=False)
    if not ok:
        return jsonify({"ok": False, "error": reason}), 403

    try:
        from routes.stocks.common import _get_api_stocks_attr
        ticket_fn = _get_api_stocks_attr("create_sse_ticket", create_sse_ticket)
        ticket = ticket_fn(request)
    except SseTicketSessionUnavailable as exc:
        current_app.logger.warning("Refused to issue SSE ticket without a session: %s", exc)
        return jsonify({"ok": False, "error": "session required for SSE ticket"}), 403

    resp = jsonify({"ok": True, "ticket": ticket, "expires_in": SSE_TICKET_TTL_SEC})
    from utils.env_helpers import _is_production_env

    _cookie_secure = _is_production_env() or os.environ.get("MNS_COOKIE_SECURE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    resp.set_cookie(
        "sse_ticket",
        ticket,
        max_age=int(SSE_TICKET_TTL_SEC),
        httponly=True,
        secure=_cookie_secure,
        partitioned=_cookie_secure,
        samesite="Strict",
        path="/api/stocks",
    )
    return resp


@stream_bp.route("/api/stocks/stream", methods=["GET"])
@rate_limit(max_requests=30, window_seconds=60)
def api_stocks_stream() -> Any:
    """SSEストリームエンドポイント（接続数・モード切替対応）"""
    ok, reason = require_sse_auth(request)
    if not ok:
        return jsonify({"ok": False, "error": reason}), 403

    request_id = getattr(g, "request_id", "-")

    raw_mode = str(request.args.get("mode", "2")).strip().lower()
    if raw_mode in ("0", "disabled", "off"):
        return jsonify(
            {"status": "disabled", "sse_mode": 0, "message": "SSE streaming disabled by client"}
        ), 200

    sse_mode = 2 if raw_mode in ("2", "tradingview", "tradingview_realtime") else 1

    reservation = app_state.sse_listener_limiter.reserve()
    if reservation is None:
        current_app.logger.warning("SSE listener limit exceeded id=%s", request_id)
        return error_response(
            ErrorCode.TOO_MANY_REQUESTS,
            status_code=429,
            details={"reason": "too many SSE connections"},
        )

    announcer = app_state.sse_announcer_mode2 if sse_mode == 2 else app_state.sse_announcer_mode1

    def stream() -> Iterator[str]:
        try:
            with announcer.listener_context(enforce_limit=False) as q:
                rt_ctx: Any
                if sse_mode == 2:
                    rt_ctx = realtime_market_engine.client_context()
                else:
                    rt_ctx = nullcontext(None)

                with rt_ctx as rt_client_id:
                    current_app.logger.info(
                        "SSE Stream client connected id=%s (mode=%d)", request_id, sse_mode
                    )

                    last_event_id = _parse_last_event_id()
                    replay_entries = None
                    replayed_frames_count = 0
                    replay_requires_initial = False
                    if last_event_id > 0:
                        replay_requires_initial = not sse_event_log.contains(
                            last_event_id, sse_mode
                        )
                        replay_entries = sse_event_log.replay_after(last_event_id, sse_mode)
                        if replay_entries is not None:
                            for seq, kind, payload in replay_entries:
                                try:
                                    frame = _replay_frame_for_entry(seq, kind, payload, sse_mode)
                                    if frame is not None:
                                        yield frame
                                        replayed_frames_count += 1
                                    else:
                                        replay_requires_initial = True
                                except Exception as exc:
                                    current_app.logger.warning(
                                        "Error processing replay frame seq=%s (mode=%d): %s",
                                        seq,
                                        sse_mode,
                                        exc,
                                    )
                                    replay_requires_initial = True
                            current_app.logger.info(
                                "SSE Stream replayed %d event(s) (%d frame(s)) id=%s (mode=%d, last_event_id=%s)",
                                len(replay_entries),
                                replayed_frames_count,
                                request_id,
                                sse_mode,
                                last_event_id,
                            )

                    send_initial = (
                        last_event_id <= 0
                        or replay_entries is None
                        or replay_requires_initial
                        or (len(replay_entries) > 0 and replayed_frames_count == 0)
                    )

                    if send_initial:
                        stocks_payload = resolve_stocks_for_response(
                            include_portfolio=False, real_data_only=(sse_mode == 2)
                        )
                        for market in ("us", "jp", "idx"):
                            if market in stocks_payload and isinstance(
                                stocks_payload[market], list
                            ):
                                for s in stocks_payload[market]:
                                    if isinstance(s, dict) and "symbol" in s:
                                        s["tv_symbol"] = s.get(
                                            "tv_symbol"
                                        ) or get_tradingview_symbol(
                                            s["symbol"], exchange=s.get("exchange")
                                        )

                        indices_payload = resolve_indices_for_response()
                        all_stocks_list = stocks_payload.get("us", []) + stocks_payload.get(
                            "jp", []
                        )
                        tv_ticker_tape = get_tradingview_ticker_tape_symbols(
                            indices=indices_payload,
                            stocks=all_stocks_list,
                        )

                        with app_state.cache.sse_data_lock:
                            initial_payload = json.dumps(
                                _json_safe(
                                    {
                                        "stream_event": "initial_snapshot",
                                        "sse_mode": sse_mode,
                                        "stocks": stocks_payload,
                                        "indices": indices_payload,
                                        "tv_ticker_tape": tv_ticker_tape,
                                        "is_us_market_open": is_market_open("us"),
                                        "is_jp_market_open": is_market_open("jp"),
                                    }
                                ),
                                allow_nan=False,
                            )
                        initial_frame = f"retry: 3000\ndata: {initial_payload}\n\n"
                        initial_seq = sse_event_log.next_id()
                        sse_event_log.record(initial_seq, sse_mode, "frame", initial_frame)
                        yield f"id: {initial_seq}\n{initial_frame}"
                        if sse_mode == 2:
                            try:
                                realtime_market_engine.get_market_deltas(rt_client_id)
                                realtime_market_engine.get_pts_deltas(rt_client_id)
                            except Exception as purge_exc:
                                current_app.logger.debug(
                                    "Failed to purge initial deltas: %s", purge_exc
                                )

                    heartbeat_interval = SSE_HEARTBEAT_INTERVAL
                    last_heartbeat_time = time.time()
                    last_mode2_full_ts = 0.0

                    def _queued_frame(item: str | tuple[Any, Any]) -> str:
                        if isinstance(item, tuple):
                            seq, msg = item
                            if isinstance(msg, str) and msg.startswith(":"):
                                return msg
                            return f"id: {seq}\n{msg}"
                        msg = item
                        if msg.startswith(":"):
                            return msg
                        seq = sse_event_log.next_id()
                        sse_event_log.record(seq, sse_mode, "frame", msg)
                        return f"id: {seq}\n{msg}"

                    while True:
                        dropped = False
                        while True:
                            try:
                                msg = q.get_nowait()
                                if msg is None:
                                    current_app.logger.info(
                                        "SSE listener dropped due to backpressure id=%s", request_id
                                    )
                                    dropped = True
                                    break
                                yield _queued_frame(msg)
                            except queue.Empty:
                                break
                        if dropped:
                            break

                        now = time.time()
                        if sse_mode == 2:
                            try:
                                deltas = realtime_market_engine.get_market_deltas(rt_client_id)
                                if deltas:
                                    seq = sse_event_log.next_id()
                                    current_app.logger.debug(
                                        "SSE sending realtime_update to client id=%s with %d symbol(s): %s",
                                        request_id,
                                        len(deltas),
                                        list(deltas.keys()),
                                    )
                                    delta_data = json.dumps(
                                        _json_safe(
                                            {"stream_event": "realtime_update", "deltas": deltas}
                                        ),
                                        allow_nan=False,
                                    )
                                    delta_frame = f"event: realtime_update\ndata: {delta_data}\n\n"
                                    sse_event_log.record(seq, 2, "frame", delta_frame)
                                    yield f"id: {seq}\n{delta_frame}"

                                pts_deltas = realtime_market_engine.get_pts_deltas(rt_client_id)
                                if pts_deltas:
                                    seq = sse_event_log.next_id()
                                    pts_data = json.dumps(
                                        _json_safe(
                                            {"stream_event": "pts_update", "deltas": pts_deltas}
                                        ),
                                        allow_nan=False,
                                    )
                                    pts_frame = f"event: pts_update\ndata: {pts_data}\n\n"
                                    sse_event_log.record(seq, 2, "frame", pts_frame)
                                    yield f"id: {seq}\n{pts_frame}"
                            except Exception as e:
                                current_app.logger.debug(
                                    "Failed fetching realtime engine deltas: %s", e
                                )

                            if now - last_mode2_full_ts >= SSE_MODE2_FULL_SNAPSHOT_INTERVAL_SEC:
                                last_mode2_full_ts = now
                                try:
                                    snapshot = realtime_market_engine.get_market_snapshot(
                                        rt_client_id
                                    )
                                    if snapshot:
                                        seq = sse_event_log.next_id()
                                        full_data = json.dumps(
                                            _json_safe(
                                                {
                                                    "stream_event": "realtime_update",
                                                    "deltas": snapshot,
                                                }
                                            ),
                                            allow_nan=False,
                                        )
                                        full_frame = (
                                            f"event: realtime_update\ndata: {full_data}\n\n"
                                        )
                                        sse_event_log.record(seq, 2, "frame", full_frame)
                                        yield f"id: {seq}\n{full_frame}"
                                except Exception as e:
                                    current_app.logger.debug(
                                        "Failed emitting mode-2 periodic snapshot: %s", e
                                    )

                        if now - last_heartbeat_time >= heartbeat_interval:
                            heartbeat_data = json.dumps({"type": "heartbeat", "timestamp": now})
                            yield f"event: heartbeat\ndata: {heartbeat_data}\n\n"
                            last_heartbeat_time = now

                        if sse_mode == 2 and rt_client_id is not None:
                            realtime_market_engine.wait_for_updates(rt_client_id, timeout=0.5)
                        else:
                            market_open = is_market_open("us") or is_market_open("jp")
                            wait_msg = None
                            got_msg = False
                            try:
                                wait_msg = q.get(timeout=0.5 if market_open else 2.0)
                                got_msg = True
                            except queue.Empty:
                                pass
                            if got_msg:
                                if wait_msg is None:
                                    current_app.logger.info(
                                        "SSE listener dropped id=%s", request_id
                                    )
                                    break
                                yield _queued_frame(wait_msg)
                            elif not market_open:
                                yield ": keepalive\n\n"
        except GeneratorExit:
            raise
        except RuntimeError as exc:
            if (
                "too many" in str(exc).lower()
                or "limit" in str(exc).lower()
                or app_state.sse_listener_limiter.listener_count() >= MAX_SSE_LISTENERS
            ):
                current_app.logger.warning(
                    "SSE listener limit exceeded concurrently id=%s: %s", request_id, exc
                )
                err_data = json.dumps({"error": "too many SSE connections"})
                yield f"event: error\ndata: {err_data}\n\n"
                return
            current_app.logger.exception("SSE stream error id=%s", request_id)
            try:
                err_data = json.dumps({"error": "stream error"})
                yield f"event: error\ndata: {err_data}\n\n"
            except Exception:  # nosec B110
                pass
        except Exception:
            current_app.logger.exception("SSE stream error id=%s", request_id)
            try:
                err_data = json.dumps({"error": "stream error"})
                yield f"event: error\ndata: {err_data}\n\n"
            except Exception:  # nosec B110
                pass
        finally:
            reservation.release()

    try:
        response = Response(
            stream_with_context(stream()),  # type: ignore[call-overload,no-matching-overload]
            mimetype="text/event-stream",
        )
    except Exception:
        reservation.release()
        raise
    response.call_on_close(reservation.release)
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response
