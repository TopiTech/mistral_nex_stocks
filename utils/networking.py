import ipaddress
import logging
import os
import re
import time
from pathlib import Path
import json
from urllib.parse import urlparse

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


def _is_allowed_shutdown_origin(req):
    """シャットダウン要求の送信元オリジンが許可されているか判定"""
    allowed_origins = get_allowed_cors_origins()
    normalized_origins = {o.rstrip("/") for o in allowed_origins}

    origin = (req.headers.get("Origin") or "").strip().rstrip("/")
    if origin:
        return origin in normalized_origins

    referer = (req.headers.get("Referer") or "").strip()
    if referer:
        parsed = urlparse(referer)
        ref_origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        return ref_origin in normalized_origins
    return False


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
    """Check if the request originates from localhost with 2026 security standards."""
    environ = getattr(req, "environ", None) or {}
    remote = environ.get("RAW_REMOTE_ADDR") or getattr(req, "remote_addr", "") or ""
    remote = str(remote).strip()
    if not _is_loopback_ip(remote):
        return False

    forwarded = req.headers.get("X-Forwarded-For", "")
    if forwarded:
        forwarded_ips = [x.strip() for x in forwarded.split(",")]
        for ip in forwarded_ips:
            if ip and not _is_loopback_ip(ip):
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

    if parsed_host not in ("localhost", "127.0.0.1", "::1"):
        if not _is_loopback_ip(parsed_host):
            return False
    return True
