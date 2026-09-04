import json
import logging
import os
import secrets
import signal
import threading
import time
from datetime import UTC, datetime
from typing import Any

from flask import Blueprint, current_app, g, jsonify, request
from werkzeug.exceptions import BadRequest

from app_state import app_state

# R1: PID + lock files now live in the per-user runtime data directory;
# the legacy project-root file is removed on shutdown so a long-running
# installation stops leaving stale state in the source tree.
from config_store import APP_DATA_DIR
from constants import (
    BASE_DIR,
    LANGSEARCH_API_KEY_MIN_LENGTH,
    MISTRAL_API_KEY_MIN_LENGTH,
    TAVILY_API_KEY_MIN_LENGTH,
)
from credential_manager import (
    clear_api_credentials,
    get_api_credential_state,
    get_custom_ai_prompt,
    get_model_badge,
    get_model_name,
    save_api_credentials,
)
from error_codes import ErrorCode
from route_helpers import _seconds_until, rate_limit
from utils.env_helpers import _env_bool
from utils.networking import (
    _is_allowed_shutdown_origin,
    _is_local_request,
    require_trusted_or_admin,
)
from utils.stock_payload import error_response
from utils.text_utils import (
    _is_valid_api_key,
    _parse_json_request,
    _parse_optional_json_request,
    _token_fingerprint,
)

api_system_bp = Blueprint("api_system", __name__)


def _terminate_current_process(logger: logging.Logger) -> None:
    """End the serving process after the shutdown response has been sent."""
    if os.name == "nt":
        # sys.exit() raised in the daemon shutdown thread only terminates that
        # thread. os._exit() is intentional here: all application cleanup has
        # already run in the caller and Windows has no SIGTERM equivalent.
        logger.info("Exiting process on Windows")
        try:
            logging.shutdown()
        except Exception:
            pass
        os._exit(0)

    try:
        logger.info("Sending SIGTERM to self for graceful shutdown")
        try:
            logging.shutdown()
        except Exception:
            pass
        os.kill(os.getpid(), signal.SIGTERM)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        try:
            logger.error(
                "Graceful shutdown failed: %s. Process must be terminated externally.",
                exc,
            )
        except Exception:
            pass
        try:
            logging.shutdown()
        except Exception:
            pass
        os._exit(1)


def _require_admin_token_if_remote(request_obj):
    """Require the admin token when the app is exposed beyond loopback or when configured."""
    allow_remote = os.environ.get("MNS_ALLOW_REMOTE_API", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    admin_token = os.environ.get("MNS_ADMIN_TOKEN", "").strip()
    if allow_remote and len(admin_token) < 32:
        return False, error_response(
            ErrorCode.FORBIDDEN,
            details={"reason": "admin token required for remote mode"},
            status_code=403,
        )

    if not admin_token:
        return True, None

    provided_token = request_obj.headers.get("X-MNS-Admin-Token", "").strip()
    if not provided_token or not secrets.compare_digest(provided_token, admin_token):
        return False, error_response(
            ErrorCode.FORBIDDEN,
            details={"reason": "invalid admin token"},
            status_code=403,
        )
    return True, None


def _build_safe_credentials_response() -> dict[str, Any]:
    """Build the public-safe credential state payload.

    R4 (ROUTE-3): explicitly enumerate allowed fields instead of spreading
    the full get_api_credential_state() dict, so that any future internal-only
    fields added to the state function are not automatically exposed in the
    API response across GET, POST, or DELETE.
    """
    _state = get_api_credential_state()
    _allowed_keys: set[str] = {
        "has_mistral_api_key",
        "has_langsearch_api_key",
        "has_tavily_api_key",
        "has_alphavantage_api_key",
        "mistral_model",
        "is_ai_technical_lines_eligible",
        "is_free_tier_model",
        "model_tier",
        "credentials_ephemeral",
        "credentials_ephemeral_keys",
        "credentials_ephemeral_warning",
        "mistral_api_key_min_length",
        "langsearch_api_key_min_length",
        "tavily_api_key_min_length",
    }
    state: dict[str, Any] = {k: _state[k] for k in _allowed_keys if k in _state}
    state["custom_ai_prompt"] = get_custom_ai_prompt()
    from config_utils import get_model_catalog, resolve_model_target
    from credential_manager import get_model_badge

    state["available_models"] = get_model_catalog()
    state["model_badge"] = get_model_badge()
    configured_model_str = str(state.get("mistral_model") or "")
    resolved_info = resolve_model_target(configured_model_str)
    state["model_label"] = (
        str(resolved_info.get("label", configured_model_str))
        if resolved_info
        else configured_model_str
    )
    return state


@api_system_bp.route("/api/credentials", methods=["GET", "POST", "DELETE", "OPTIONS"])
@rate_limit(max_requests=30, window_seconds=60)
def api_credentials():
    """Handles API credential retrieval, updating, and removal.

    Personal / local-first defaults:
      * localhost + CSRF (+ trusted Origin on writes) is enough for GET/POST/DELETE.

    Hardened remote mode:
      * When ``MNS_ALLOW_REMOTE_API`` is enabled, ``MNS_ADMIN_TOKEN`` is mandatory
        for all methods. Without it the endpoint fails closed (503) so a
        misconfigured remote deployment cannot silently expose or mutate keys.
      * When an admin token IS configured (even in local mode), every method must
        present a matching ``X-MNS-Admin-Token`` header (constant-time compare).
        The first-party browser UI does not send this header, so leave
        ``MNS_ADMIN_TOKEN`` unset for personal localhost use. Configure the
        token only for reverse-proxy / remote deployments that can supply it.
    """
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    admin_token = os.environ.get("MNS_ADMIN_TOKEN", "").strip()
    allow_remote = os.environ.get("MNS_ALLOW_REMOTE_API", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    provided_token = request.headers.get("X-MNS-Admin-Token", "").strip()

    # Fail closed: remote deployments must configure an admin token before any
    # credential endpoint is usable. 413464a: normalize to typed 403 envelope
    # so anonymous probing cannot distinguish "remote without token"
    # (former 503) from "invalid token" (403). Bootstrap still hard-fails
    # with RuntimeError FATAL for the same misconfiguration.
    if allow_remote and len(admin_token) < 32:
        current_app.logger.error(
            "Credentials access denied id=%s reason=admin_token_required_for_remote remote=%s",
            getattr(g, "request_id", "-"),
            request.remote_addr,
        )
        return error_response(
            ErrorCode.FORBIDDEN,
            details={"reason": "admin token required for remote mode"},
            status_code=403,
        )

    # When an admin token is configured, every credentials request must present it.
    # Local personal use typically leaves MNS_ADMIN_TOKEN unset so the existing
    # setup/settings UI continues to work with CSRF + local-origin only.
    if admin_token and (
        not provided_token or not secrets.compare_digest(provided_token, admin_token)
    ):
        current_app.logger.warning(
            "Credentials access denied id=%s reason=invalid_admin_token remote=%s",
            getattr(g, "request_id", "-"),
            request.remote_addr,
        )
        return error_response(
            ErrorCode.FORBIDDEN,
            details={"reason": "invalid admin token"},
            status_code=403,
        )

    # Use the same deployment-aware gate as the other protected APIs.  Remote
    # mode is authenticated by the mandatory admin token; local mode retains
    # loopback plus trusted-Origin protection for writes.
    ok, reason = require_trusted_or_admin(
        request, require_origin=request.method in ("POST", "DELETE")
    )
    if not ok:
        current_app.logger.warning(
            "Credentials access denied id=%s reason=%s remote=%s",
            getattr(g, "request_id", "-"),
            reason,
            request.remote_addr,
        )
        return error_response(ErrorCode.FORBIDDEN, details={"reason": reason}, status_code=403)

    # R4 (ROUTE-3): GET reveals credential state, so apply a lenient Origin
    # check on top of the loopback gate. A present Origin must be trusted
    # (blocks cross-origin probing / CSRF-style reads), while a missing Origin
    # is allowed because same-origin browser GETs do not send an Origin header
    # and the loopback check above already restricts the caller.  In remote
    # mode the mandatory admin token is the credential and the local browser
    # Origin allow-list must not be applied (mirrors require_trusted_or_admin).
    if (
        request.method == "GET"
        and not allow_remote
        and request.headers.get("Origin")
        and not _is_allowed_shutdown_origin(request)
    ):
        current_app.logger.warning(
            "Credentials GET denied id=%s reason=untrusted_origin remote=%s",
            getattr(g, "request_id", "-"),
            request.remote_addr,
        )
        return error_response(
            ErrorCode.FORBIDDEN, details={"reason": "untrusted origin"}, status_code=403
        )

    if request.method == "GET":
        current_app.logger.info("Credentials state requested id=%s", getattr(g, "request_id", "-"))
        return jsonify({"ok": True, **_build_safe_credentials_response()})

    if request.method == "DELETE":
        failed_keys = clear_api_credentials()
        safe_state = _build_safe_credentials_response()
        if failed_keys:
            current_app.logger.warning(
                "Credentials cleared but failed to remove from OS Keyring for: %s, id=%s",
                failed_keys,
                getattr(g, "request_id", "-"),
            )
            # Partial deletion is a real failure: config may be cleared while
            # OS keyring still holds secrets. Use a non-2xx status so clients
            # do not treat logout as success.
            return jsonify(
                {
                    "ok": False,
                    "error": "設定ファイルから資格情報を削除しましたが、OSのセキュアストア（Keyring）からの削除に一部失敗しました。",
                    "failed_keys": failed_keys,
                    **safe_state,
                }
            ), 500
        current_app.logger.info("Credentials cleared id=%s", getattr(g, "request_id", "-"))
        return jsonify({"ok": True, **safe_state})

    data = _parse_json_request()
    if data is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )
    mistral_api_key = data.get("mistral_api_key")
    langsearch_api_key = data.get("langsearch_api_key")
    tavily_api_key = data.get("tavily_api_key")
    alphavantage_api_key = data.get("alphavantage_api_key")

    # Reject non-string credential values (int/bool/list/dict/...). Calling
    # .strip() on such values would raise AttributeError and surface as a
    # generic 500 instead of a client input error.
    for field_name, field_value in (
        ("mistral_api_key", mistral_api_key),
        ("langsearch_api_key", langsearch_api_key),
        ("tavily_api_key", tavily_api_key),
        ("alphavantage_api_key", alphavantage_api_key),
    ):
        if field_value is not None and not isinstance(field_value, str):
            current_app.logger.warning(
                "Credentials save rejected id=%s reason=non_string_key field=%s",
                getattr(g, "request_id", "-"),
                field_name,
            )
            return error_response(
                ErrorCode.INVALID_INPUT,
                details={"fields": [field_name], "reason": "APIキーは文字列で指定してください"},
                status_code=400,
            )

    if mistral_api_key is not None:
        mistral_api_key = mistral_api_key.strip()
        if mistral_api_key and not _is_valid_api_key(
            mistral_api_key, min_length=MISTRAL_API_KEY_MIN_LENGTH
        ):
            current_app.logger.warning(
                "Credentials save rejected id=%s reason=invalid_mistral_key len=%s min_len=%s",
                getattr(g, "request_id", "-"),
                len(mistral_api_key),
                MISTRAL_API_KEY_MIN_LENGTH,
            )
            return error_response(
                ErrorCode.INVALID_API_KEY,
                details={
                    "fields": ["mistral_api_key"],
                    "min_length": MISTRAL_API_KEY_MIN_LENGTH,
                },
            )

    if langsearch_api_key is not None:
        langsearch_api_key = langsearch_api_key.strip()
        if langsearch_api_key and not _is_valid_api_key(
            langsearch_api_key, min_length=LANGSEARCH_API_KEY_MIN_LENGTH
        ):
            current_app.logger.warning(
                "Credentials save rejected id=%s reason=invalid_langsearch_key len=%s min_len=%s",
                getattr(g, "request_id", "-"),
                len(langsearch_api_key),
                LANGSEARCH_API_KEY_MIN_LENGTH,
            )
            return error_response(
                ErrorCode.UNSAFE_INPUT,
                details={
                    "fields": ["langsearch_api_key"],
                    "min_length": LANGSEARCH_API_KEY_MIN_LENGTH,
                },
            )

    if tavily_api_key is not None:
        tavily_api_key = tavily_api_key.strip()
        if tavily_api_key and not _is_valid_api_key(
            tavily_api_key, min_length=TAVILY_API_KEY_MIN_LENGTH
        ):
            current_app.logger.warning(
                "Credentials save rejected id=%s reason=invalid_tavily_key len=%s min_len=%s",
                getattr(g, "request_id", "-"),
                len(tavily_api_key),
                TAVILY_API_KEY_MIN_LENGTH,
            )
            return error_response(
                ErrorCode.UNSAFE_INPUT,
                details={
                    "fields": ["tavily_api_key"],
                    "min_length": TAVILY_API_KEY_MIN_LENGTH,
                },
            )

    if alphavantage_api_key is not None:
        alphavantage_api_key = alphavantage_api_key.strip()
        # Alpha Vantage keys are typically 16 characters long.
        if alphavantage_api_key and not _is_valid_api_key(alphavantage_api_key, min_length=10):
            current_app.logger.warning(
                "Credentials save rejected id=%s reason=invalid_alphavantage_key len=%s min_len=10",
                getattr(g, "request_id", "-"),
                len(alphavantage_api_key),
            )
            return error_response(
                ErrorCode.UNSAFE_INPUT,
                details={
                    "fields": ["alphavantage_api_key"],
                    "min_length": 10,
                },
            )

    try:
        # Validate prompt length BEFORE any side effects to prevent
        # partial state update (credentials saved but prompt rejected).
        prompt_value: str | None = None
        if "custom_ai_prompt" in data:
            raw_prompt = data.get("custom_ai_prompt")
            if raw_prompt is not None and not isinstance(raw_prompt, str):
                return error_response(
                    ErrorCode.INVALID_INPUT,
                    details={"reason": "custom_ai_promptは文字列で指定してください"},
                    status_code=400,
                )
            prompt_value = (raw_prompt or "").strip()
            if len(prompt_value) > 5000:
                return error_response(
                    ErrorCode.UNSAFE_INPUT,
                    details={"reason": "カスタムプロンプトは5000文字以内で入力してください"},
                )
        # Validate the requested model before starting the single settings
        # transaction below.  Do not call set_model_name() here: doing so
        # would commit the model even if credential/prompt persistence fails.
        target_model_name: str | None = None
        raw_model = data.get("mistral_model")
        if raw_model is not None:
            if not isinstance(raw_model, str) or not raw_model.strip():
                return error_response(
                    ErrorCode.INVALID_INPUT,
                    details={"reason": "mistral_modelは有効な文字列で指定してください"},
                    status_code=400,
                )
            from config_utils import (
                MISTRAL_LEGACY_ALIASES,
                MISTRAL_SUPPORTED_MODELS,
                resolve_model_target,
            )

            model_str = raw_model.strip()
            resolved_model = resolve_model_target(model_str)
            if (
                not resolved_model
                and model_str not in MISTRAL_SUPPORTED_MODELS
                and model_str not in MISTRAL_LEGACY_ALIASES
            ):
                return error_response(
                    ErrorCode.INVALID_INPUT,
                    details={"reason": f"未対応のMistralモデルです: {model_str}"},
                    status_code=400,
                )
            target_model_name = str(
                resolved_model.get("name", model_str) if resolved_model else model_str
            )

        has_credentials_update = (
            mistral_api_key is not None
            or langsearch_api_key is not None
            or tavily_api_key is not None
            or alphavantage_api_key is not None
        )
        has_prompt_update = "custom_ai_prompt" in data
        if has_credentials_update or has_prompt_update or target_model_name is not None:
            save_api_credentials(
                mistral_api_key=mistral_api_key,
                langsearch_api_key=langsearch_api_key,
                tavily_api_key=tavily_api_key,
                alphavantage_api_key=alphavantage_api_key,
                custom_ai_prompt=prompt_value if has_prompt_update else None,
                update_custom_ai_prompt=has_prompt_update,
                mistral_model=target_model_name,
            )
    except RuntimeError as exc:
        current_app.logger.warning(
            "Credentials save failed id=%s reason=secure_storage_failure",
            getattr(g, "request_id", "-"),
        )
        exc_msg = str(exc)
        if "MNS_EPHEMERAL_FALLBACK" in exc_msg or "keyring" in exc_msg or "DPAPI" in exc_msg:
            reason_msg = "セキュアストレージ (keyring/DPAPI) が利用できません。ヘッドレス環境やDocker環境の場合は、環境変数 MNS_EPHEMERAL_FALLBACK=1 を設定して再起動してください。"
        else:
            reason_msg = "設定の保存に失敗しました。再試行してください。"
        return error_response(
            ErrorCode.CONFIG_ERROR,
            status_code=500,
            details={"reason": reason_msg},
        )

    current_app.logger.info(
        "Credentials/Settings saved id=%s mistral=%s langsearch=%s tavily=%s alpha=%s custom_prompt_len=%d model=%s",
        getattr(g, "request_id", "-"),
        _token_fingerprint(mistral_api_key),
        _token_fingerprint(langsearch_api_key),
        _token_fingerprint(tavily_api_key),
        _token_fingerprint(alphavantage_api_key),
        len(str(data.get("custom_ai_prompt") or "")),
        data.get("mistral_model", "-"),
    )
    return jsonify({"ok": True, **_build_safe_credentials_response()})


@api_system_bp.route("/api/credentials/verify", methods=["POST", "OPTIONS"])
@rate_limit(max_requests=20, window_seconds=60)
def api_credentials_verify():
    """Verify Mistral API key and query available models and tier status."""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    ok, reason = require_trusted_or_admin(request, require_origin=True)
    if not ok:
        return error_response(ErrorCode.FORBIDDEN, details={"reason": reason}, status_code=403)

    data = _parse_optional_json_request()
    if data is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )
    api_key = data.get("mistral_api_key")
    if not api_key:
        from credential_manager import get_mistral_api_key

        api_key = get_mistral_api_key()

    if not api_key or not isinstance(api_key, str) or not api_key.strip():
        return (
            jsonify(
                {
                    "ok": False,
                    "valid": False,
                    "error": "Mistral APIキーが指定されていません。",
                }
            ),
            400,
        )

    api_key = api_key.strip()
    start_ts = time.time()
    try:
        client = app_state.ai.get_or_create_mistral_client(api_key)
        if client is None:
            raise RuntimeError("Mistral client could not be initialized")
        models_response = client.models.list()
        latency_ms = int((time.time() - start_ts) * 1000)

        model_ids = []
        raw_list = getattr(models_response, "data", []) or []
        for m in raw_list:
            mid = getattr(m, "id", None)
            if isinstance(mid, str):
                model_ids.append(mid)
            elif isinstance(m, dict) and "id" in m:
                model_ids.append(m["id"])

        has_large = any("large" in mid.lower() for mid in model_ids)
        is_free_tier = not has_large

        tier_name = "Paid / Commercial Tier" if has_large else "Free (Experiment) Tier"
        recommended_model = "mistral-medium-2604" if has_large else "mistral-small-2603"
        recommended_label = "Mistral Medium 3.5" if has_large else "Mistral Small 4"

        return jsonify(
            {
                "ok": True,
                "valid": True,
                "tier": "paid" if has_large else "free",
                "tier_name": tier_name,
                "is_free_tier": is_free_tier,
                "model_count": len(model_ids),
                "accessible_models": model_ids[:30],
                "recommended_model": recommended_model,
                "recommended_label": recommended_label,
                "latency_ms": latency_ms,
                "message": f"接続成功 ({tier_name}) - 推奨モデル: {recommended_label} (応答: {latency_ms}ms)",
            }
        )
    except Exception as exc:
        latency_ms = int((time.time() - start_ts) * 1000)
        current_app.logger.warning("Mistral API key verification failed: %s", exc)
        return (
            jsonify(
                {
                    "ok": False,
                    "valid": False,
                    "latency_ms": latency_ms,
                    # Provider exceptions can contain request URLs, account
                    # metadata, or credentials. Keep those diagnostics in the
                    # server log and return a stable, user-actionable message.
                    "error": "APIキーの検証に失敗しました。設定と接続を確認してください。",
                }
            ),
            400,
        )


@api_system_bp.route("/api/health", methods=["GET", "OPTIONS"])
@rate_limit(max_requests=60, window_seconds=60)
def api_health():
    """ヘルスチェックエンドポイント"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    ok, denied = _require_admin_token_if_remote(request)
    if not ok:
        return denied
    yf_limited = app_state.market.is_yf_rate_limited()
    yf_until = None
    if yf_limited:
        from app_state import yf_session_manager

        rl_until = yf_session_manager.get_rate_limit_until("yfinance")
        if rl_until:
            yf_until = datetime.fromtimestamp(rl_until, tz=UTC).isoformat()

    bootstrap_skipped = _env_bool("MNS_SKIP_BOOTSTRAP")
    bootstrap_ready = app_state.bootstrap_ready.is_set() and not bootstrap_skipped
    with app_state._extension_origins_cache_lock:
        manifest_ok = app_state._extension_manifest_status.get("ok", True)
        manifest_error = app_state._extension_manifest_status.get("error", "")

    health_data = {
        "ok": True,
        "ready": bootstrap_ready,
        "bootstrap_skipped": bootstrap_skipped,
        "app": "Mistral NeX Stocks",
        "model": get_model_name(),
        "badge": get_model_badge(),
        "is_yfinance_rate_limited": yf_limited,
        "yfinance_rate_limit_until": yf_until,
        "extension_manifest_ok": manifest_ok,
        "extension_manifest_error": manifest_error,
        "timestamp": datetime.now(UTC).isoformat(),
    }

    # APIキーの設定状態はローカルリクエストのみに暴露（リモートモードでは非開示）
    allow_remote = os.environ.get("MNS_ALLOW_REMOTE_API", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if not allow_remote and _is_local_request(request):
        health_data.update(get_api_credential_state())

    return jsonify(health_data)


@api_system_bp.route("/api/cache-stats", methods=["GET", "OPTIONS"])
@rate_limit(max_requests=30, window_seconds=60)
def api_cache_stats():
    """キャッシュ統計情報エンドポイント"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    ok, denied = _require_admin_token_if_remote(request)
    if not ok:
        return denied
    allow_remote = os.environ.get("MNS_ALLOW_REMOTE_API", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if not allow_remote and not _is_local_request(request):
        return error_response(ErrorCode.FORBIDDEN, details={"reason": "forbidden"}, status_code=403)
    stats = app_state.cache.get_stats()
    with app_state.cache.cache_lock:
        cache_sizes = {str(dur): len(c) for dur, c in app_state.cache.caches.items()}
    stats["cache_sizes"] = cache_sizes
    # Include disk cache statistics
    try:
        stats.update(app_state.stock_disk_cache.stats())
    except Exception as exc:
        current_app.logger.debug("Failed to read disk cache stats: %s", exc)
    try:
        stats.update(app_state.payload_disk_cache.stats())
    except Exception as exc:
        current_app.logger.debug("Failed to read payload disk cache stats: %s", exc)
    return jsonify({"ok": True, "cache_stats": stats})


@api_system_bp.route("/api/metrics", methods=["GET", "OPTIONS"])
@rate_limit(max_requests=30, window_seconds=60)
def api_metrics():
    """Expose safe operational metrics for local troubleshooting.
    SECURITY: This endpoint is restricted to localhost only.
    Sensitive internal state is intentionally excluded.
    """
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    ok, denied = _require_admin_token_if_remote(request)
    if not ok:
        return denied
    allow_remote = os.environ.get("MNS_ALLOW_REMOTE_API", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if not allow_remote and not _is_local_request(request):
        return error_response(ErrorCode.FORBIDDEN, details={"reason": "forbidden"}, status_code=403)

    # Only expose safe, non-sensitive operational metrics
    with app_state.cache.cache_lock:
        cache_sizes = {str(dur): len(c) for dur, c in app_state.cache.caches.items()}

    with app_state.market.yfinance_lock:
        yfinance_metrics = {
            "rate_limited": (
                app_state.market.is_yfinance_rate_limited
                and time.time() < app_state.market.yfinance_rate_limit_until
            ),
            "rate_limit_clears_in_sec": _seconds_until(app_state.market.yfinance_rate_limit_until),
        }

    with app_state.market.scraper_block_lock:
        scraper_metrics = {
            "blocked": app_state.market.is_scraper_blocked(),
            "block_clears_in_sec": app_state.market.scraper_block_clears_in(),
        }

    with app_state.cache.sse_data_lock:
        current_stock_counts = {
            market: len(items) for market, items in app_state.market.current_stocks_cache.items()
        }
        current_indices_count = len(app_state.market.current_indices_cache)

    with app_state.market.is_syncing_lock:
        is_syncing = app_state.market.is_syncing

    # Expose thread-pool saturation so operators can see when the AI-bound
    # `executor` or the market-data `data_executor` are backing up (H3/M6).
    executors = {
        "ai": app_state.execution.executor_stats(app_state.execution.executor),
        "data": app_state.execution.executor_stats(app_state.execution.data_executor),
        "news": app_state.execution.executor_stats(app_state.execution.news_executor),
        "sync": app_state.execution.executor_stats(app_state.execution.sync_refresh_executor),
    }

    # Realtime engine diagnostics so producer-level failures (dead WS, blocked
    # scrapers, stale market_store) are visible in one screen. All reads are
    # defensive: the engine is optional and its threads may not be started.
    try:
        from services.realtime_engine import realtime_market_engine as _rt_engine

        _latest_ts = 0.0
        try:
            for _p in _rt_engine.get_market_snapshot().values():
                _ts = _p.get("updated_at") or 0.0
                _latest_ts = max(_latest_ts, _ts)
        except Exception:
            _latest_ts = 0.0

        engine_metrics: dict[str, Any] = {
            "running": bool(getattr(_rt_engine, "running", False)),
            "market_store_count": len(_rt_engine.get_market_snapshot()),
            "pts_store_count": len(_rt_engine.get_pts_snapshot()),
            "last_update_at": _latest_ts or None,
            "tv_ws_connected": bool(getattr(_rt_engine.tv_client, "connected", False)),
            "tv_last_connected_at": (
                getattr(_rt_engine.tv_client, "last_connected_at", 0.0) or None
            ),
            "tv_subscribed_symbols": len(getattr(_rt_engine.tv_client, "symbols", set())),
            "jp_scraper_symbols": len(getattr(_rt_engine.yahoojp_scraper, "symbols", set())),
        }
        # Thread liveness reads the inner ``.thread`` attribute of each producer.
        engine_metrics["tv_thread_alive"] = bool(
            getattr(getattr(_rt_engine.tv_client, "thread", None), "is_alive", lambda: False)()
        )
        engine_metrics["jp_scraper_thread_alive"] = bool(
            getattr(
                getattr(_rt_engine.yahoojp_scraper, "thread", None), "is_alive", lambda: False
            )()
        )
        engine_metrics["pts_thread_alive"] = bool(
            getattr(getattr(_rt_engine, "pts_thread", None), "is_alive", lambda: False)()
        )
    except Exception as exc:
        current_app.logger.debug("Failed to collect realtime engine metrics: %s", exc)
        engine_metrics = {"error": str(exc)[:200]}

    with app_state.market.scraper_block_lock:
        engine_metrics["scraper_blocked"] = app_state.market.is_scraper_blocked()
        engine_metrics["scraper_block_clears_in_sec"] = app_state.market.scraper_block_clears_in()
    with app_state.market.yfinance_lock:
        engine_metrics["yf_rate_limited"] = app_state.market.is_yf_rate_limited()

    return jsonify(
        {
            "ok": True,
            "timestamp": datetime.now(UTC).isoformat(),
            "cache": {
                "sizes": cache_sizes,
                **app_state.stock_disk_cache.stats(),
                **app_state.payload_disk_cache.stats(),
            },
            "market_data": {
                "yfinance": yfinance_metrics,
                "scraper": scraper_metrics,
                "is_syncing": is_syncing,
                "stock_counts": current_stock_counts,
                "indices_count": current_indices_count,
            },
            "sse": {
                # Admission reservations include a response returned by Flask
                # but not yet iterated by the WSGI server, so this is the
                # authoritative global-capacity metric.  Per-mode figures
                # remain queue-level observability only.
                "listeners": app_state.sse_listener_limiter.listener_count(),
                "mode1_listeners": app_state.sse_announcer_mode1.listener_count(),
                "mode2_listeners": app_state.sse_announcer_mode2.listener_count(),
                "mode1_announced": app_state.sse_announcer_mode1.stats()["announced"],
                "mode1_dropped": app_state.sse_announcer_mode1.stats()["dropped"],
                "mode2_announced": app_state.sse_announcer_mode2.stats()["announced"],
                "mode2_dropped": app_state.sse_announcer_mode2.stats()["dropped"],
            },
            "engine": engine_metrics,
            "executors": executors,
            "mistral": {
                **app_state.ai.mistral_usage_stats(),
                "circuit_open": app_state.market.is_circuit_open("mistral"),
                "next_allowed_ts": app_state.ai.mistral_next_allowed_ts,
                "response_cache_size": app_state.ai.response_cache_size(),
                "clients_cached": app_state.ai.clients_cached_count(),
            },
            "config": {
                "model": get_model_name(),
                "badge": get_model_badge(),
            },
        }
    )


@api_system_bp.route("/api/csrf-token", methods=["GET", "OPTIONS"])
@rate_limit(max_requests=30, window_seconds=60)
def api_csrf_token():
    """Issue a fresh CSRF token for long-lived browser sessions.

    The page renders its CSRF token once into ``<meta name="csrf-token">`` at
    load time, but the token expires after ``WTF_CSRF_TIME_LIMIT`` (1h). The
    dashboard is designed to stay open for hours (SSE stream), so a long-running
    tab would otherwise hit CSRF 400 rejections on every mutating request after
    the first hour. The frontend (utils.js) periodically calls this endpoint and
    swaps the fresh token into the meta tag so the tab keeps working without a
    manual reload.

    Security: the token alone is useless without the HttpOnly session cookie
    (SameSite=Strict), the response is not readable cross-origin (no CORS header
    is emitted for foreign origins), and state-changing routes additionally gate
    on local-origin / Sec-Fetch-Site checks. The endpoint itself is still
    local-only, matching /api/metrics and /api/cache-stats.
    """
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    ok, denied = _require_admin_token_if_remote(request)
    if not ok:
        return denied
    allow_remote = os.environ.get("MNS_ALLOW_REMOTE_API", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if not allow_remote and not _is_local_request(request):
        return error_response(ErrorCode.FORBIDDEN, details={"reason": "forbidden"}, status_code=403)

    from flask_wtf.csrf import generate_csrf

    return jsonify({"ok": True, "csrf_token": generate_csrf()})


@api_system_bp.route("/api/csp-report", methods=["POST"])
@rate_limit(max_requests=10, window_seconds=60)
def api_csp_report():
    """CSP report receiver for Report-Only mode (accepts JSON POST)."""
    try:
        payload = request.get_json(force=True, silent=True) or {}
        if (
            isinstance(payload, dict)
            and "csp-report" in payload
            and isinstance(payload["csp-report"], dict)
        ):
            payload = payload["csp-report"]
        elif isinstance(payload, list):
            reports = [
                item.get("body", item) if isinstance(item, dict) else {}
                for item in payload
                if isinstance(item, dict)
            ]
            payload = reports
        if not isinstance(payload, dict):
            payload = payload if isinstance(payload, list) else [{}]
        modern_key_map = {
            "documentURL": "document-uri",
            "blockedURL": "blocked-uri",
            "effectiveDirective": "effective-directive",
            "originalPolicy": "original-policy",
            "lineNumber": "line-number",
            "columnNumber": "column-number",
            "sourceFile": "source-file",
            "statusCode": "status-code",
            "sample": "script-sample",
        }
        payloads = payload if isinstance(payload, list) else [payload]
        payloads = [
            {modern_key_map.get(key, key): value for key, value in item.items()}
            for item in payloads
            if isinstance(item, dict)
        ]
        # Sanitize: remove potentially sensitive fields before logging
        safe_keys = {
            "document-uri",
            "violated-directive",
            "effective-directive",
            "original-policy",
            "disposition",
            "blocked-uri",
            "line-number",
            "column-number",
            "source-file",
            "status-code",
            "referrer",
            "script-sample",
        }
        sanitized_reports = []
        for report in payloads[:20]:
            sanitized = {k: v for k, v in report.items() if k in safe_keys}
            # Truncate URI values and strip control characters to prevent log injection
            for key in ("document-uri", "blocked-uri", "source-file", "referrer"):
                if key in sanitized and isinstance(sanitized[key], str):
                    sanitized[key] = sanitized[key][:200]
            for key, val in sanitized.items():
                if isinstance(val, str):
                    sanitized[key] = "".join(c for c in val if ord(c) >= 0x20 or c in ("\t", "\n"))
            sanitized_reports.append(sanitized)
        current_app.logger.info(
            "CSP report received: %s",
            json.dumps(sanitized_reports, ensure_ascii=False)[:2000],
        )
    except (BadRequest, TypeError, ValueError) as exc:
        current_app.logger.debug("Failed to parse CSP report: %s", exc)
    # Return 204 No Content as recommended for CSP reports
    return ("", 204)


@api_system_bp.route("/api/shutdown", methods=["POST", "OPTIONS"])
# Matches /api/stocks/reset (the other destructive endpoint). A failed attempt
# writes a warning log line and touches the used-token marker file, so an
# unthrottled local caller could otherwise drive unbounded log/IO churn.
@rate_limit(max_requests=5, window_seconds=60)
def api_shutdown():
    """シャットダウンエンドポイント（ワンタイムトークン使用）"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    # Disable shutdown endpoint in production
    is_prod = os.environ.get("MNS_PROD", "").strip().lower() in ("1", "true", "yes")
    if is_prod:
        current_app.logger.warning("Shutdown request rejected: disabled in production environment")
        return error_response(
            ErrorCode.FORBIDDEN,
            details={"reason": "shutdown is disabled in production"},
            status_code=403,
        )

    # F-4: Block shutdown in remote/proxy mode. Shutdown is a local-only
    # operation; remote callers should not be able to terminate the server
    # even with a valid admin token + shutdown token.
    allow_remote = os.environ.get("MNS_ALLOW_REMOTE_API", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if allow_remote:
        current_app.logger.warning(
            "Shutdown request rejected: not available in remote API mode id=%s",
            getattr(g, "request_id", "-"),
        )
        return error_response(
            ErrorCode.FORBIDDEN,
            details={"reason": "shutdown is not available in remote API mode"},
            status_code=403,
        )

    if not _is_local_request(request):
        current_app.logger.warning(
            "Shutdown request rejected from non-local address: %s", request.remote_addr
        )
        return error_response(
            ErrorCode.UNSAFE_INPUT, details={"reason": "forbidden"}, status_code=403
        )

    # Double check connection raw remote IP to resist any proxy-override headers spoofing
    raw_remote = request.environ.get("RAW_REMOTE_ADDR") or request.environ.get("REMOTE_ADDR", "")
    raw_remote = str(raw_remote).strip()
    from utils.networking import _is_loopback_ip

    if raw_remote and not _is_loopback_ip(raw_remote):
        current_app.logger.warning(
            "Shutdown request rejected: WSGI REMOTE_ADDR %s is not loopback", raw_remote
        )
        return error_response(
            ErrorCode.UNSAFE_INPUT, details={"reason": "forbidden"}, status_code=403
        )

    if not _is_allowed_shutdown_origin(request):
        current_app.logger.warning("Shutdown request rejected from untrusted origin")
        return error_response(
            ErrorCode.UNSAFE_INPUT, details={"reason": "untrusted origin"}, status_code=403
        )

    # JSON body validation
    data = _parse_json_request()
    if data is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )

    if data.get("confirm") is not True:
        return jsonify({"ok": False, "error": "confirm flag required"}), 400

    token_header = request.headers.get("X-MNS-Shutdown-Token")
    token_json = data.get("shutdown_token")
    # Preserve the established fallback from an empty header to the JSON field.
    token_value = token_header or token_json
    if token_value is not None and not isinstance(token_value, str):
        current_app.logger.warning("Shutdown request rejected: malformed shutdown token")
        return jsonify({"ok": False, "error": "invalid shutdown request"}), 403
    provided_token = (token_value or "").strip()

    if not provided_token:
        current_app.logger.warning("Shutdown request rejected: missing shutdown token")
        return jsonify({"ok": False, "error": "invalid shutdown request"}), 403

    if not app_state.consume_shutdown_token(provided_token):
        current_app.logger.warning(
            "Shutdown request rejected: invalid or already used shutdown token"
        )
        return jsonify({"ok": False, "error": "invalid shutdown request"}), 403

    logger = current_app.logger
    logger.info("Valid shutdown token accepted, initiating shutdown sequence")

    # The token was atomically consumed above before any shutdown work. Rotate
    # only after consumption so concurrent requests cannot enter this block
    # with the same one-time token.
    try:
        app_state.rotate_shutdown_token()
        logger.info("Shutdown token rotated for next session")
    except RuntimeError as exc:
        logger.warning("Failed to rotate shutdown token before shutdown: %s", exc)
        # Token is already consumed; rotation failure does not revert that.
        # The server shutdown proceeds regardless; the next startup will
        # generate a fresh token from scratch.

    def shutdown_server():
        logger.info("Shutdown thread started")

        # No sleep — shutdown should be as fast as possible
        try:
            app_state.shutdown_executors()
        except (RuntimeError, AttributeError, ValueError) as exc:
            logger.warning("Executor shutdown before process exit failed: %s", exc)

        # Remove PID file before exiting. R1: prefer the runtime-state copy
        # and also clean up a legacy project-root file written by older
        # versions so the source tree stops accumulating stale lock state.
        for pid_file in (APP_DATA_DIR / ".backend.pid", BASE_DIR / ".backend.pid"):
            try:
                if not pid_file.exists():
                    continue
                logger.info("Removing PID file %s", pid_file)
                removed = False
                for _ in range(2):
                    try:
                        pid_file.unlink()
                    except OSError:
                        time.sleep(0.1)
                    if not pid_file.exists():
                        removed = True
                        break
                if not removed:
                    logger.warning("PID file still exists after retry attempts: %s", pid_file)
                else:
                    logger.info("PID file removed successfully: %s", pid_file)
            except OSError as exc:
                logger.warning("Failed to remove pid file %s during shutdown: %s", pid_file, exc)

        try:
            from app_bg import _release_leader_lock

            _release_leader_lock()
        except Exception as exc:
            logger.debug("Failed to release leader lock during shutdown: %s", exc)

        # Brief delay to allow the HTTP 200 JSON response to be fully flushed over TCP
        time.sleep(0.3)
        _terminate_current_process(logger)

    shutdown_thread = threading.Thread(target=shutdown_server)
    shutdown_thread.daemon = True
    shutdown_thread.start()
    return jsonify({"ok": True, "message": "Shutting down..."})


@api_system_bp.route("/api/system/ai-usage", methods=["GET", "OPTIONS"])
@rate_limit(max_requests=30, window_seconds=60)
def get_ai_usage():
    """Return aggregated Mistral AI token usage statistics and cost estimates."""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    ok, denied = _require_admin_token_if_remote(request)
    if not ok:
        return denied
    allow_remote = os.environ.get("MNS_ALLOW_REMOTE_API", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if not allow_remote and not _is_local_request(request):
        return error_response(ErrorCode.FORBIDDEN, details={"reason": "forbidden"}, status_code=403)
    stats = app_state.ai.mistral_usage_stats()
    return jsonify({"ok": True, "usage": stats})
