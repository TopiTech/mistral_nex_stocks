import ipaddress
import logging
import os
import re
import secrets
import time
from pathlib import Path
import json

from app_state import app_state
from constants import _BASE_ALLOWED_CORS_ORIGINS

logger = logging.getLogger(__name__)

_cors_origins_cache = None
_cors_origins_cache_ts = 0.0
_CORS_ORIGINS_CACHE_TTL = 30.0


def _normalize_extension_origin(raw):
    if raw is None:
        return None
    value = str(raw).strip().rstrip("/")
    if not value:
        return None

    if value.startswith("chrome-extension://"):
        origin_id = value[len("chrome-extension://") :].lower()
        if re.fullmatch(r"[a-z0-9]{32}", origin_id):
            return f"chrome-extension://{origin_id}"
        return None

    normalized = value.lower()
    if re.fullmatch(r"[a-z0-9]{32}", normalized):
        return f"chrome-extension://{normalized}"
    return None


def _load_allowed_extension_origins():
    """Load extension origins from env and native host manifest (if available)."""
    now = time.time()
    with app_state._extension_origins_cache_lock:
        if (
            now - app_state._extension_origins_cache_ts
        ) < app_state._EXTENSION_ORIGINS_CACHE_TTL_SEC:
            return set(app_state._extension_origins_cache)

    origins = set()
    app_state._extension_manifest_status["ok"] = True
    app_state._extension_manifest_status["error"] = ""

    extension_origin = _normalize_extension_origin(
        os.environ.get("MNS_EXTENSION_ORIGIN", "")
    )
    if extension_origin:
        origins.add(extension_origin)

    env_origins = os.environ.get("MNS_ALLOWED_EXTENSION_ORIGINS", "")
    for raw in env_origins.split(","):
        origin = _normalize_extension_origin(raw)
        if origin:
            origins.add(origin)

    try:
        manifest_path = (
            Path(__file__).resolve().parents[1]
            / "native_host"
            / "com.mistral_nex_stocks.host.json"
        )
        if manifest_path.exists():
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest_data = json.load(f) or {}
            for raw in manifest_data.get("allowed_origins", []) or []:
                origin = _normalize_extension_origin(str(raw or "").strip())
                if origin:
                    origins.add(origin)
    except FileNotFoundError:
        logger.debug("Extension manifest not found, skipping")
    except Exception as exc:
        app_state._extension_manifest_status["ok"] = False
        app_state._extension_manifest_status["error"] = f"manifest_load_error: {exc}"

    with app_state._extension_origins_cache_lock:
        app_state._extension_origins_cache.clear()
        app_state._extension_origins_cache.update(origins)
        app_state._extension_origins_cache_ts = now

    return origins


def get_allowed_cors_origins():
    """Retrieve the set of allowed CORS origins from constants and dynamic sources."""
    global _cors_origins_cache, _cors_origins_cache_ts
    now = time.time()
    if _cors_origins_cache is not None and (now - _cors_origins_cache_ts) < _CORS_ORIGINS_CACHE_TTL:
        return _cors_origins_cache
    origins = {origin.rstrip("/") for origin in _BASE_ALLOWED_CORS_ORIGINS}
    origins.update(_load_allowed_extension_origins())
    _cors_origins_cache = origins
    _cors_origins_cache_ts = now
    return origins


def require_trusted_state_changing_request(req, require_origin=True):
    """Validate local state-changing API requests with a consistent origin policy."""
    if not _is_local_request(req):
        return False, "forbidden"
    if require_origin and not _is_allowed_shutdown_origin(req):
        return False, "untrusted origin"
    return True, ""


def require_trusted_or_admin(req, require_origin=True):
    """Gate for state-changing / costly endpoints in ALL deployment modes.

    Local-first (default): behaves exactly like
    ``require_trusted_state_changing_request`` (loopback + allowed origin).

    Remote / reverse-proxy mode (``MNS_ALLOW_REMOTE_API=1`` with
    ``MNS_PROXY_FIX=1``): ``_is_local_request`` returns True regardless of the
    caller's address, so the loopback/origin checks alone are no longer
    sufficient. When an ``MNS_ADMIN_TOKEN`` is configured, this function
    additionally requires a matching ``X-MNS-Admin-Token`` header
    (constant-time compare) — matching the policy already enforced on
    ``/api/credentials``. Callers that reach this with no admin token set are
    still gated by the loopback/origin policy (personal use leaves the token
    unset, exactly like credentials).

    Returns:
        (ok: bool, reason: str)
    """
    ok, reason = require_trusted_state_changing_request(req, require_origin=require_origin)
    if not ok:
        return ok, reason

    admin_token = os.environ.get("MNS_ADMIN_TOKEN", "").strip()
    if not admin_token:
        return True, ""

    provided = (req.headers.get("X-MNS-Admin-Token") or "").strip()
    if not provided or not secrets.compare_digest(provided, admin_token):
        return False, "invalid admin token"
    return True, ""


def _is_allowed_shutdown_origin(req):
    """State-changing API 要求の送信元オリジンが許可されているか判定。

    Origin ヘッダのみを信頼する。Referer は Origin より改ざん・欠落が起きやすく、
    オリジン検証の厳格性を弱めるためフォールバックとして使わない。
    """
    allowed_origins = get_allowed_cors_origins()
    normalized_origins = {o.rstrip("/") for o in allowed_origins}

    origin = (req.headers.get("Origin") or "").strip().rstrip("/")
    return bool(origin) and origin in normalized_origins


def _is_loopback_ip(ip_str: str) -> bool:
    if not ip_str:
        return False
    ip_str = ip_str.strip().lower()
    if ip_str in ("localhost", "localhost:5000", "localhost:80", "localhost:443"):
        return True

    # Handle IPv6 with port, e.g., [::1]:5000
    if ip_str.startswith("[") and "]" in ip_str:
        bracket_end = ip_str.index("]")
        inner = ip_str[1:bracket_end]
        try:
            addr = ipaddress.ip_address(inner)
            return addr.is_loopback
        except ValueError:
            return False

    # Strip port if present (e.g. 127.0.0.1:5000)
    if ":" in ip_str:
        parts = ip_str.split(":")
        if len(parts) == 2:
            ip_str = parts[0]

    try:
        addr = ipaddress.ip_address(ip_str)
        return addr.is_loopback
    except ValueError:
        return False


def _is_local_request(req):
    """Check if the request originates from localhost with 2026 security standards.

    Authorization model (personal/local-first):
      * By default the API is reachable ONLY from loopback addresses, with no
        trusted proxy headers. This is safe against Host/X-Forwarded-For
        spoofing because a spoofed header is simply ignored on a direct listener.
      * `MNS_ALLOW_REMOTE_API=1` is a DENY-BY-DEFAULT escape hatch for running
        behind a trusted reverse proxy. It is only honored when `MNS_PROXY_FIX=1`
        is also set, so a bare `MNS_ALLOW_REMOTE_API=1` on a directly-listening
        server cannot accidentally expose the API to the network. Even when
        enabled, callers (require_trusted_state_changing_request / the
        api_analysis/shutdown gates) still enforce origin allow-lists and the
        loopback REMOTE_ADDR, so this only relaxes the address-family check.
    """
    is_prod = os.environ.get("MNS_PROD", "").strip().lower() in ("1", "true", "yes")
    proxied = os.environ.get("MNS_PROXY_FIX", "").strip().lower() in ("1", "true", "yes")
    allow_remote = (
        os.environ.get("MNS_ALLOW_REMOTE_API", "").strip().lower() in ("1", "true", "yes")
        and proxied
    )
    if allow_remote:
        # Reverse-proxy mode: the address check is delegated to the proxy, which
        # must set X-Forwarded-For correctly. We still refuse to trust a spoofed
        # loopback Host in production.
        _host = (req.headers.get("Host") or "").strip()
        if _host:
            try:
                from urllib.parse import urlsplit
                _parsed_host = (urlsplit(f"http://{_host}").hostname or "").lower()
            except Exception:
                return False
            if is_prod and (
                _parsed_host in ("localhost", "127.0.0.1", "::1")
                or _is_loopback_ip(_parsed_host)
            ):
                return False
        return True

    environ = getattr(req, "environ", None) or {}
    # Use RAW_REMOTE_ADDR (backed up by middleware) or raw environ REMOTE_ADDR (untouched by ProxyFix)
    remote = environ.get("RAW_REMOTE_ADDR") or environ.get("REMOTE_ADDR") or getattr(req, "remote_addr", "") or ""
    remote = str(remote).strip()
    if not _is_loopback_ip(remote):
        return False

    forwarded = req.headers.get("X-Forwarded-For", "")
    if forwarded:
        forwarded_ips = [x.strip() for x in forwarded.split(",")]
        # On a direct listener (no ProxyFix), X-Forwarded-For is attacker-
        # controllable and must NOT be trusted. Reject any request that
        # presents it unless we are explicitly running behind a trusted proxy.
        if not proxied:
            return False
        for ip in forwarded_ips:
            if ip and not _is_loopback_ip(ip):
                return False
        # In production, if X-Forwarded-For is present (even if all are loopback),
        # treat it as a proxy-forwarded external request to prevent loopback-bypass.
        if is_prod and any(ip for ip in forwarded_ips if ip):
            return False

    host = (req.headers.get("Host") or "").strip()
    if not host:
        return False

    try:
        from urllib.parse import urlsplit
        parsed = urlsplit(f"http://{host}")
        parsed_host = parsed.hostname
        if not parsed_host:
            return False
        parsed_host = parsed_host.lower()
    except Exception:
        return False

    # In production, do not trust loopback Host headers (e.g. 'localhost'),
    # as external attackers can spoof the Host header through reverse proxies.
    if is_prod:
        if parsed_host in ("localhost", "127.0.0.1", "::1") or _is_loopback_ip(parsed_host):
            return False

    if parsed_host not in ("localhost", "127.0.0.1", "::1"):
        if not _is_loopback_ip(parsed_host):
            return False
    return True
