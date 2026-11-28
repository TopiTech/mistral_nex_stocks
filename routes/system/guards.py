"""管理トークンゲート。`routes.api_system` からの純粋な移動。"""

from __future__ import annotations

import os

import routes.api_system
from error_codes import ErrorCode
from utils.env_helpers import _env_bool
from utils.networking import check_admin_token, is_allowed_trusted_origin
from utils.stock_payload import error_response

PUBLIC_SAFE_CREDENTIAL_KEYS: frozenset[str] = frozenset(
    {
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
        "sse_heartbeat_interval",
    }
)


def _check_system_read_origin_and_locality(request_obj):
    """Enforce locality and local-Origin checks for system read endpoints.

    In reverse-proxy remote mode (``MNS_ALLOW_REMOTE_API=1`` AND
    ``MNS_PROXY_FIX=1``), remote callers authenticate with ``X-MNS-Admin-Token``
    and ``_is_local_request`` validates the forwarded Host; the localhost-only
    ``is_allowed_trusted_origin`` allow-list must not block remote origins.
    """
    allow_remote = _env_bool("MNS_ALLOW_REMOTE_API")
    remote_proxy_mode = allow_remote and _env_bool("MNS_PROXY_FIX")
    is_local = routes.api_system._is_local_request(request_obj)
    if (not allow_remote or remote_proxy_mode) and not is_local:
        return error_response(ErrorCode.FORBIDDEN, details={"reason": "forbidden"}, status_code=403)
    if (
        not remote_proxy_mode
        and (not allow_remote or is_local)
        and request_obj.headers.get("Origin")
        and not is_allowed_trusted_origin(request_obj)
    ):
        return error_response(
            ErrorCode.FORBIDDEN, details={"reason": "untrusted origin"}, status_code=403
        )
    return None


def _require_admin_token_if_remote(request_obj):
    """Require the admin token when the app is exposed beyond loopback or when configured."""
    allow_remote = _env_bool("MNS_ALLOW_REMOTE_API")
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
    if not check_admin_token(provided_token):
        return False, error_response(
            ErrorCode.FORBIDDEN,
            details={"reason": "invalid admin token"},
            status_code=403,
        )
    return True, None
