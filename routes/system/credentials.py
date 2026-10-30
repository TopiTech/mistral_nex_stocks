"""資格情報管理。`routes.api_system` からの純粋な移動。

`save_api_credentials` / `get_custom_ai_prompt` /
`get_api_credential_state` / `clear_api_credentials` はテストで
`patch(\"routes.api_system.X\")` されるため、呼び出し時に
`routes.api_system` 属性として解決する。
"""

from __future__ import annotations

import os
import time
from typing import Any

from flask import current_app, g, request

import routes.api_system
from app_state import app_state
from error_codes import ErrorCode
from route_helpers import _parse_credentials_save_request, rate_limit
from routes.system.blueprint import api_system_bp
from utils.env_helpers import _env_bool
from utils.networking import (
    check_admin_token,
    is_allowed_trusted_origin,
    require_trusted_or_admin,
)
from utils.stock_payload import error_response, ok_response
from utils.text_utils import _parse_json_request, _parse_optional_json_request, _token_fingerprint


def _build_safe_credentials_response() -> dict[str, Any]:
    """Build the public-safe credential state payload.

    R4 (ROUTE-3): explicitly enumerate allowed fields instead of spreading
    the full get_api_credential_state() dict, so that any future internal-only
    fields added to the state function are not automatically exposed in the
    API response across GET, POST, or DELETE.
    """
    _state = routes.api_system.get_api_credential_state()
    _allowed_keys: set[str] = {
        "has_mistral_api_key",
        "has_openai_api_key",
        "has_langsearch_api_key",
        "has_tavily_api_key",
        "has_alphavantage_api_key",
        "has_ai_access",
        "ai_provider",
        "is_beta_provider",
        "provider_notice",
        "openai_base_url",
        "openai_model",
        "openai_embeddings_model",
        "is_local_openai_base_url",
        "api_mode",
        "agent_id",
        "preview_mode",
        "model_selectable",
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
    state["custom_ai_prompt"] = routes.api_system.get_custom_ai_prompt()
    from config_utils import get_model_catalog, resolve_model_target
    from credential_manager import get_model_badge

    # Agents API: the model is fixed in Mistral Console -> no selectable models.
    state["available_models"] = [] if state.get("api_mode") == "agents" else get_model_catalog()
    state["model_badge"] = get_model_badge()
    configured_model_str = str(state.get("mistral_model") or "")
    resolved_info = resolve_model_target(configured_model_str)
    state["model_label"] = (
        str(resolved_info.get("label", configured_model_str))
        if resolved_info
        else configured_model_str
    )
    if state.get("ai_provider") == "openai_compatible":
        cur_openai_model = str(state.get("openai_model") or "gpt-4o-mini")
        state["model_label"] = f"OpenAI互換 (Beta): {cur_openai_model}"
        state["model_badge"] = f"[Beta] {cur_openai_model}"
    elif state.get("api_mode") == "agents":
        state["model_label"] = "Agents API (モデルはMistralコンソールで指定)"
    elif state.get("preview_mode"):
        state["model_label"] = "プレビューモード"
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
        return ok_response()

    admin_token = os.environ.get("MNS_ADMIN_TOKEN", "").strip()
    allow_remote = _env_bool("MNS_ALLOW_REMOTE_API")
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
    if admin_token and not check_admin_token(provided_token):
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
        and not is_allowed_trusted_origin(request)
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
        return ok_response(_build_safe_credentials_response())

    if request.method == "DELETE":
        failed_keys = routes.api_system.clear_api_credentials()
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
            return error_response(
                ErrorCode.SYSTEM_ERROR,
                details={
                    "reason": "設定ファイルから資格情報を削除しましたが、OSのセキュアストア（Keyring）からの削除に一部失敗しました。",
                    "failed_keys": failed_keys,
                    **safe_state,
                },
                status_code=500,
            )
        current_app.logger.info("Credentials cleared id=%s", getattr(g, "request_id", "-"))
        return ok_response(safe_state)

    data = _parse_json_request()
    if data is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )
    # Boundary validation lives in ``schemas.CredentialsSaveRequest`` (via
    # ``route_helpers._parse_credentials_save_request``); the parsed values
    # below are the normalized ones the old inline checks produced.
    # Server-state-dependent rules (Agents-mode gating, model catalog
    # resolution) stay here in the route.
    parsed_creds, creds_error = _parse_credentials_save_request(data)
    if creds_error:
        return creds_error
    if parsed_creds is None:
        return error_response(
            ErrorCode.MALFORMED_INPUT,
            details={"reason": "JSON形式が不正です"},
            status_code=400,
        )
    mistral_api_key = parsed_creds.mistral_api_key
    openai_api_key = parsed_creds.openai_api_key
    langsearch_api_key = parsed_creds.langsearch_api_key
    tavily_api_key = parsed_creds.tavily_api_key
    alphavantage_api_key = parsed_creds.alphavantage_api_key
    prompt_value = parsed_creds.custom_ai_prompt
    target_api_mode = parsed_creds.mistral_api_mode
    target_agent_id = parsed_creds.mistral_agent_id
    target_preview = parsed_creds.preview_mode
    has_prompt_update = "custom_ai_prompt" in data

    try:
        # --- API mode (chat / agents) / agent id / preview mode ---------------
        from credential_manager import get_agent_id, get_api_mode

        effective_api_mode = target_api_mode or get_api_mode()
        effective_agent_id = target_agent_id if target_agent_id is not None else get_agent_id()
        if (
            effective_api_mode == "agents"
            and (target_api_mode == "agents" or target_agent_id is not None)
            and not effective_agent_id
            and not target_preview
        ):
            return error_response(
                ErrorCode.INVALID_INPUT,
                details={
                    "fields": ["mistral_agent_id"],
                    "reason": "Agents APIを使用するにはAgent IDが必要です",
                },
                status_code=400,
            )

        # Agents API: the model is selected in Mistral Console, so the app must
        # not allow (or silently store) a model choice.
        if effective_api_mode == "agents" and data.get("mistral_model") is not None:
            return error_response(
                ErrorCode.INVALID_INPUT,
                details={
                    "fields": ["mistral_model"],
                    "reason": "Agents APIではモデルはMistral側のコンソールで指定するため、アプリ側では変更できません",
                },
                status_code=400,
            )

        # Validate the requested model before starting the single settings
        # transaction below.  Do not call set_model_name() here: doing so
        # would commit the model even if credential/prompt persistence fails.
        target_model_name: str | None = None
        model_str = parsed_creds.mistral_model
        if model_str is not None:
            from config_utils import (
                MISTRAL_LEGACY_ALIASES,
                MISTRAL_SUPPORTED_MODELS,
                resolve_model_target,
            )

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

        # --- AI provider / OpenAI compatible settings --------------------------
        # (shape validation lives in the schema; normalized targets below)
        target_provider = parsed_creds.ai_provider
        target_openai_url = parsed_creds.openai_base_url
        target_openai_model = parsed_creds.openai_model
        target_openai_emb_model = parsed_creds.openai_embeddings_model

        has_credentials_update = (
            mistral_api_key is not None
            or openai_api_key is not None
            or langsearch_api_key is not None
            or tavily_api_key is not None
            or alphavantage_api_key is not None
        )
        has_mode_update = (
            target_api_mode is not None
            or target_agent_id is not None
            or target_preview is not None
            or target_provider is not None
            or target_openai_url is not None
            or target_openai_model is not None
            or target_openai_emb_model is not None
        )
        if (
            has_credentials_update
            or has_prompt_update
            or target_model_name is not None
            or has_mode_update
        ):
            routes.api_system.save_api_credentials(
                mistral_api_key=mistral_api_key,
                openai_api_key=openai_api_key,
                langsearch_api_key=langsearch_api_key,
                tavily_api_key=tavily_api_key,
                alphavantage_api_key=alphavantage_api_key,
                custom_ai_prompt=prompt_value if has_prompt_update else None,
                update_custom_ai_prompt=has_prompt_update,
                mistral_model=target_model_name,
                api_mode=target_api_mode,
                agent_id=target_agent_id,
                preview_mode=target_preview,
                ai_provider=target_provider,
                openai_base_url=target_openai_url,
                openai_model=target_openai_model,
                openai_embeddings_model=target_openai_emb_model,
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
        "Credentials/Settings saved id=%s provider=%s mistral=%s openai=%s langsearch=%s tavily=%s alpha=%s custom_prompt_len=%d model=%s",
        getattr(g, "request_id", "-"),
        target_provider or "mistral",
        _token_fingerprint(mistral_api_key),
        _token_fingerprint(openai_api_key),
        _token_fingerprint(langsearch_api_key),
        _token_fingerprint(tavily_api_key),
        _token_fingerprint(alphavantage_api_key),
        len(str(data.get("custom_ai_prompt") or "")),
        data.get("mistral_model", "-"),
    )
    return ok_response(_build_safe_credentials_response())


@api_system_bp.route("/api/credentials/verify", methods=["POST", "OPTIONS"])
@rate_limit(max_requests=20, window_seconds=60)
def api_credentials_verify():
    """Verify Mistral API key and query available models and tier status."""
    if request.method == "OPTIONS":
        return ok_response()

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
    target_provider = data.get("ai_provider") or data.get("target_provider")
    if not target_provider:
        from credential_manager import get_ai_provider

        target_provider = get_ai_provider()

    if target_provider == "openai_compatible":
        from credential_manager import get_openai_api_key, get_openai_base_url
        from services.openai_service import verify_openai_connection

        openai_key = data.get("openai_api_key")
        if openai_key is None:
            openai_key = data.get("api_key")
        if not openai_key or not str(openai_key).strip():
            openai_key = get_openai_api_key() or ""
        else:
            openai_key = str(openai_key).strip()

        base_url = data.get("openai_base_url") or data.get("base_url") or get_openai_base_url()
        res_dict = verify_openai_connection(base_url=base_url, api_key=openai_key)
        if not res_dict.get("ok"):
            return error_response(
                ErrorCode.INVALID_API_KEY,
                400,
                message=str(
                    res_dict.get("error")
                    or "OpenAI互換APIキーの検証に失敗しました。設定と接続を確認してください。"
                ),
                extra_fields={
                    "code": "INVALID_API_KEY",
                    "valid": False,
                    "latency_ms": res_dict.get("latency_ms", 0),
                    "details": res_dict,
                },
            )
        return ok_response(res_dict)

    api_key = data.get("mistral_api_key")
    if not api_key:
        from credential_manager import get_mistral_api_key

        api_key = get_mistral_api_key()

    if not api_key or not isinstance(api_key, str) or not api_key.strip():
        return error_response(
            ErrorCode.INVALID_API_KEY,
            400,
            message="Mistral APIキーが指定されていません。",
            extra_fields={"code": "INVALID_API_KEY", "valid": False},
        )

    api_key = api_key.strip()
    from credential_manager import (
        get_agent_id,
        get_api_mode,
        is_preview_api_key,
    )

    if is_preview_api_key(api_key):
        return ok_response(
            {
                "valid": True,
                "preview": True,
                "message": "プレビューモードです（APIキー未使用・サンプル出力）。",
            }
        )

    start_ts = time.time()
    try:
        client = app_state.ai.get_or_create_mistral_client(api_key)
        if client is None:
            raise RuntimeError("Mistral client could not be initialized")

        requested_mode = data.get("mistral_api_mode")
        verify_mode = requested_mode if requested_mode in ("chat", "agents") else get_api_mode()
        if verify_mode == "agents":
            agent_id = data.get("mistral_agent_id")
            if not isinstance(agent_id, str) or not agent_id.strip():
                agent_id = get_agent_id()
            if not agent_id:
                return error_response(
                    ErrorCode.MISSING_REQUIRED_FIELD,
                    400,
                    message="Agent IDが指定されていません。",
                    extra_fields={"code": "MISSING_REQUIRED_FIELD", "valid": False},
                )
            beta_api = getattr(client, "beta", None)
            if not beta_api or not hasattr(beta_api, "agents"):
                return error_response(
                    ErrorCode.INVALID_API_KEY,
                    400,
                    message="Agents APIが現在のSDKでサポートされていません。",
                    extra_fields={"code": "INVALID_API_KEY", "valid": False},
                )
            agent = beta_api.agents.get(agent_id=agent_id.strip())
            latency_ms = int((time.time() - start_ts) * 1000)
            agent_model = getattr(agent, "model", None)
            agent_name = getattr(agent, "name", None)
            return ok_response(
                {
                    "valid": True,
                    "mode": "agents",
                    "agent_name": agent_name if isinstance(agent_name, str) else "",
                    "agent_model": agent_model if isinstance(agent_model, str) else "",
                    "tier_name": "Agents API",
                    "model_count": 1 if agent_model else 0,
                    "latency_ms": latency_ms,
                    "message": f"Agent接続成功 (応答: {latency_ms}ms)",
                }
            )

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

        return ok_response(
            {
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
        current_app.logger.warning(
            "Mistral API key verification failed error_type=%s", type(exc).__name__
        )
        return error_response(
            ErrorCode.INVALID_API_KEY,
            400,
            # Provider exceptions can contain request URLs, account
            # metadata, or credentials. Return a stable,
            # user-actionable message without recording that text.
            message="APIキーの検証に失敗しました。設定と接続を確認してください。",
            extra_fields={"code": "INVALID_API_KEY", "valid": False, "latency_ms": latency_ms},
        )
