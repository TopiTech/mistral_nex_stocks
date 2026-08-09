import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path

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

    # Chrome uses chrome-extension://; Edge uses extension://. Both carry
    # a 32-char lowercase hex origin-id. Normalise everything to the
    # chrome-extension:// canonical form so internal checks are uniform.
    # Firefox uses moz-extension:// with an RFC4122 UUID.
    for prefix in ("chrome-extension://", "extension://"):
        if value.startswith(prefix):
            origin_id = value[len(prefix) :].lower()
            if re.fullmatch(r"[a-z0-9]{32}", origin_id):
                return f"chrome-extension://{origin_id}"
            return None

    if value.startswith("moz-extension://"):
        origin_id = value[len("moz-extension://") :].lower()
        if re.fullmatch(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", origin_id) or re.fullmatch(r"[a-z0-9]{32}", origin_id):
            return f"moz-extension://{origin_id}"
        return None

    normalized = value.lower()
    if re.fullmatch(r"[a-z0-9]{32}", normalized):
        return f"chrome-extension://{normalized}"
    if re.fullmatch(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", normalized):
        return f"moz-extension://{normalized}"
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
    manifest_status = {"ok": True, "error": ""}

    extension_origin = _normalize_extension_origin(os.environ.get("MNS_EXTENSION_ORIGIN", ""))
    if extension_origin:
        origins.add(extension_origin)

    env_origins = os.environ.get("MNS_ALLOWED_EXTENSION_ORIGINS", "")
    for raw in env_origins.split(","):
        origin = _normalize_extension_origin(raw)
        if origin:
            origins.add(origin)

    try:
        manifest_path = (
            Path(__file__).resolve().parents[1] / "native_host" / "com.mistral_nex_stocks.host.json"
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
        manifest_status["ok"] = False
        manifest_status["error"] = f"manifest_load_error: {exc}"

    with app_state._extension_origins_cache_lock:
        app_state._extension_manifest_status.clear()
        app_state._extension_manifest_status.update(manifest_status)
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


# Query-param names that carry secret bearer tokens. These must NEVER be
# logged verbatim; they are masked before any request path/URL is written to logs.
_SENSITIVE_QUERY_PARAMS = ("admin_token", "api_key", "key", "password", "secret", "shutdown_token", "ticket", "token")


def mask_sensitive_url(url: str) -> str:
    """Return *url* with any secret-bearing query params replaced by a mask.

    Used so request URLs logged via Flask's request.path / request.full_path do
    not leak the admin or shutdown token into log files, browser history
    proxies, or crash reports. Non-sensitive query params are preserved.
    """
    if not url or "?" not in url:
        return url
    path, _, query = url.partition("?")
    if not query:
        return url
    pairs = []
    for pair in query.split("&"):
        if not pair:
            continue
        key, sep, _value = pair.partition("=")
        if key in _SENSITIVE_QUERY_PARAMS:
            pairs.append(f"{key}=[REDACTED]")
        else:
            pairs.append(pair if sep else key)
    return f"{path}?{'&'.join(pairs)}"


def require_trusted_or_admin(req, require_origin=True, allow_query_token=False):
    """Gate for state-changing / costly endpoints in ALL deployment modes.

    Local-first (default): behaves exactly like
    ``require_trusted_state_changing_request`` (loopback + allowed origin).

    Remote / reverse-proxy mode (``MNS_ALLOW_REMOTE_API=1`` with
    ``MNS_PROXY_FIX=1``): ``_is_local_request`` returns True regardless of the
    caller's address, so the loopback/origin checks alone are no longer
    sufficient. When an ``MNS_ADMIN_TOKEN`` is configured (even in local mode),
    this function additionally requires a matching ``X-MNS-Admin-Token`` header
    (constant-time compare) — matching the policy already enforced on
    ``/api/credentials``. The first-party browser UI does not send this header,
    so leave the token unset for personal localhost use. Callers that reach this
    with no admin token set are still gated by the loopback/origin policy.

    **Query-param token acceptance is restricted to SSE.** The
    ``X-MNS-Admin-Token`` header is the primary authenticator for every gated
    endpoint. Query-string tokens (``?admin_token=`` / ``?token=``) are only
    accepted when *allow_query_token* is True, which is set exclusively by the
    SSE stream endpoint: ``EventSource`` cannot set request headers, so the
    stream has no alternative. Accepting the token in the URL on any other
    endpoint would expose it to access logs, proxies, and browser history.

    Args:
        req: The Flask request object.
        require_origin: Whether to require a trusted ``Origin`` (loopback mode).
        allow_query_token: If True, also accept the admin token via the
            ``admin_token`` / ``token`` query params. **Only the SSE stream
            should set this.**

    Returns:
        (ok: bool, reason: str)
    """
    ok, reason = require_trusted_state_changing_request(req, require_origin=require_origin)
    if not ok:
        return ok, reason

    allow_remote = os.environ.get("MNS_ALLOW_REMOTE_API", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    admin_token = os.environ.get("MNS_ADMIN_TOKEN", "").strip()

    if allow_remote and len(admin_token) < 32:
        # Fail closed in remote mode even if startup validation was skipped.
        return False, "admin token must contain at least 32 characters"
    if not admin_token:
        return True, ""

    provided = req.headers.get("X-MNS-Admin-Token", "").strip()
    if not provided and allow_query_token:
        if allow_remote:
            # Query-string tokens are local-only (SSE). In remote/proxy mode the
            # URL — including query params — can be logged by the proxy and
            # stored in browser history, so fail closed instead of accepting a
            # URL-borne admin token.
            return False, "query token not allowed in remote mode"
        # SSE-only fallback: EventSource cannot send custom headers, so the
        # token travels in the query string for this single endpoint. Every
        # other gated endpoint must use the header (see module docstring above).
        provided = (req.args.get("admin_token") or req.args.get("token") or "").strip()
    if not provided or not secrets.compare_digest(provided, admin_token):
        return False, "invalid admin token"
    return True, ""


# ---------------------------------------------------------------------------
# Short-lived SSE connection tickets
# ---------------------------------------------------------------------------
# ``EventSource`` cannot attach request headers, so the SSE stream endpoint
# accepts a short-lived, single-use ticket that is bound to the issuing
# session. The ticket is generated by a CSRF-protected POST and consumed on
# the GET stream request, so a long-lived admin token never travels in the
# URL (see SECURITY.md "SSE token-in-URL risk").
SSE_TICKET_TTL_SEC = 120.0
_SSE_TICKETS: dict[str, tuple[str, float]] = {}
_SSE_TICKETS_LOCK = threading.Lock()


def _session_id_for_sse(req) -> str:
    """Return the session identifier used to bind an SSE ticket.

    The id is persisted in the Flask session (``_sse_sid``) on first use so
    that the browser cookie reliably carries it: ``session.sid`` does not
    exist in the default secure-cookie session and ``REMOTE_ADDR`` is shared
    by every client on the same host. A session-bound ticket issued by one
    browser is therefore useless from another browser (different cookie),
    which is the R4 guarantee. Falls back to the raw peer address only when
    no session is available (e.g. non-browser clients); the value is never
    exposed to clients.
    """
    try:
        from flask import session

        sid = session.get("_sse_sid") or session.get("_id")
        if not sid:
            sid = secrets.token_hex(16)
            session["_sse_sid"] = sid
        return str(sid)
    except Exception as exc:
        logger.debug("Failed to get session id for SSE: %s", exc)
    return str(
        req.environ.get("RAW_REMOTE_ADDR")
        or req.environ.get("REMOTE_ADDR")
        or getattr(req, "remote_addr", "")
        or "unknown"
    )


def create_sse_ticket(req, ttl_sec: float | None = None) -> str:
    """Create and store a session-bound, single-use SSE ticket."""
    if ttl_sec is None:
        ttl_sec = SSE_TICKET_TTL_SEC
    ticket = secrets.token_urlsafe(24)
    now = time.time()
    expires_at = now + float(ttl_sec)
    with _SSE_TICKETS_LOCK:
        # Periodic cleanup of expired tickets to prevent memory leaks (M-7)
        expired_keys = [k for k, (_, exp) in _SSE_TICKETS.items() if now > exp]
        for k in expired_keys:
            _SSE_TICKETS.pop(k, None)
        if len(_SSE_TICKETS) >= 500:
            oldest_key = min(_SSE_TICKETS.keys(), key=lambda k: _SSE_TICKETS[k][1])
            _SSE_TICKETS.pop(oldest_key, None)
        _SSE_TICKETS[ticket] = (_session_id_for_sse(req), expires_at)
    return ticket


def consume_sse_ticket(req, ticket: str) -> bool:
    """Consume a session-bound SSE ticket if it is valid, unexpired, and unused.

    The ticket is removed on any attempt so it cannot be replayed.
    """
    if not ticket:
        return False
    with _SSE_TICKETS_LOCK:
        entry = _SSE_TICKETS.pop(ticket, None)
    if entry is None:
        return False
    bound_session, expires_at = entry
    if time.time() > expires_at:
        return False
    return secrets.compare_digest(bound_session, _session_id_for_sse(req))


def require_sse_auth(req, require_origin: bool = False):
    """Authenticate the SSE stream endpoint.

    Accepts either:
      * a matching ``X-MNS-Admin-Token`` header (constant-time compare), or
      * a session-bound short-lived ticket in the query string
        (``sse_ticket`` / ``ticket``), consumed on success.

    When ``MNS_ADMIN_TOKEN`` is unset (the normal personal/local setup) the
    trusted/local gate alone is sufficient. Remote/proxy mode without a
    configured admin token fails closed.

    Returns:
        (ok: bool, reason: str)
    """
    ok, reason = require_trusted_state_changing_request(req, require_origin=require_origin)
    if not ok:
        return ok, reason

    allow_remote = os.environ.get("MNS_ALLOW_REMOTE_API", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    admin_token = os.environ.get("MNS_ADMIN_TOKEN", "").strip()

    if allow_remote and len(admin_token) < 32:
        return False, "admin token must contain at least 32 characters"

    # No admin token configured → the trusted/local gate alone is the whole
    # policy (same as require_trusted_or_admin). Any stray header is ignored.
    if not admin_token:
        return True, ""

    provided_header = (req.headers.get("X-MNS-Admin-Token") or "").strip()
    if provided_header:
        if not secrets.compare_digest(provided_header, admin_token):
            return False, "invalid admin token"
        return True, ""

    provided_ticket = (
        req.args.get("sse_ticket") or req.args.get("ticket") or ""
    ).strip()
    if provided_ticket and consume_sse_ticket(req, provided_ticket):
        return True, ""

    return False, "invalid SSE ticket or admin token"


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
                _parsed_host in ("localhost", "127.0.0.1", "::1") or _is_loopback_ip(_parsed_host)
            ):
                return False
        return True

    environ = getattr(req, "environ", None) or {}
    # The raw socket peer address captured before any proxy rewriting. This is
    # the ONLY authoritative source for the direct-listener check: REMOTE_ADDR
    # alone may have been rewritten by ProxyFix (which trusts X-Forwarded-For)
    # even when MNS_ALLOW_REMOTE_API is off, so a misconfigured proxy must not
    # be able to turn an external peer into a "local" request.
    raw_remote = (
        environ.get("RAW_REMOTE_ADDR")
        or environ.get("REMOTE_ADDR")
        or getattr(req, "remote_addr", "")
        or ""
    )
    raw_remote = str(raw_remote).strip()
    if not _is_loopback_ip(raw_remote):
        return False

    # X-Forwarded-For is attacker-controllable on a direct listener and must
    # never be trusted outside the explicit remote/proxy mode handled above.
    # Reject any request that presents it, even if all entries claim loopback.
    forwarded = req.headers.get("X-Forwarded-For", "")
    if forwarded and not allow_remote:
        logger.warning(
            "Rejected request presenting X-Forwarded-For header on direct listener: remote=%s",
            raw_remote,
        )
        return False

    host = (req.headers.get("Host") or "").strip()
    if not host:
        return not proxied

    try:
        from urllib.parse import urlsplit

        parsed = urlsplit(f"http://{host}")
        parsed_host = parsed.hostname
        if not parsed_host:
            return False
        parsed_host = parsed_host.lower()
    except Exception:
        return False

    # In production, do not trust loopback Host headers (e.g. 'localhost')
    # WHEN running behind a proxy, as external attackers can spoof the Host
    # header through reverse proxies to bypass local-request gates.
    # When running directly (no proxy), loopback REMOTE_ADDR is sufficient and
    # Host: localhost is normal browser behavior.
    if (
        is_prod
        and proxied
        and (parsed_host in ("localhost", "127.0.0.1", "::1") or _is_loopback_ip(parsed_host))
    ):
        return False

    return parsed_host in ("localhost", "127.0.0.1", "::1") or _is_loopback_ip(parsed_host)
