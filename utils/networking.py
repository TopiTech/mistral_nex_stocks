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
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        # R5: narrowed from bare ``Exception``. A malformed/unreadable manifest
        # must be recorded as a degraded status rather than silently swallowed,
        # because it directly determines the CORS origin allow-list. Unexpected
        # error types now propagate instead of quietly widening/narrowing trust.
        manifest_status["ok"] = False
        manifest_status["error"] = f"manifest_load_error: {exc}"
        logger.warning("Failed to load extension manifest origins: %s", exc)

    with app_state._extension_origins_cache_lock:
        app_state._extension_manifest_status.clear()
        app_state._extension_manifest_status.update(manifest_status)
        app_state._extension_origins_cache.clear()
        app_state._extension_origins_cache.update(origins)
        app_state._extension_origins_cache_ts = now

    return origins

def _normalize_origin(origin: str) -> str:
    """Normalize CORS origin for consistent comparison.

    Handles trailing slashes and normalizes localhost variants
    (localhost, 127.0.0.1, [::1]) to a canonical form.
    """
    if not origin:
        return ""
    # Strip trailing slash
    origin = origin.rstrip("/")
    # Normalize localhost variants (including IPv6 bracket notation)
    replacements = [
        ("://127.0.0.1", "://localhost"),
        (":[::1]", ":localhost"),
        ("://[::1]", "://localhost"),
    ]
    for old, new in replacements:
        origin = origin.replace(old, new)
    return origin.lower()


def get_allowed_cors_origins():
    """Retrieve the set of allowed CORS origins from constants and dynamic sources."""
    global _cors_origins_cache, _cors_origins_cache_ts
    now = time.time()
    if _cors_origins_cache is not None and (now - _cors_origins_cache_ts) < _CORS_ORIGINS_CACHE_TTL:
        return _cors_origins_cache
    origins = {_normalize_origin(origin) for origin in _BASE_ALLOWED_CORS_ORIGINS}
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
# Entries MUST be lowercase: lookups normalise the incoming key with .lower() so
# an attacker-supplied ``?ADMIN_TOKEN=`` cannot slip past the mask.
# ``sse_ticket`` is the name actually used by the SSE stream endpoint
# (``/api/stocks/stream?sse_ticket=...``); ``ticket`` is the accepted alias.
_SENSITIVE_QUERY_PARAMS = (
    "admin_token",
    "api_key",
    "key",
    "last_event_id",
    "password",
    "secret",
    "shutdown_token",
    "sse_ticket",
    "ticket",
    "token",
)


def mask_sensitive_url(url: str) -> str:
    """Return *url* with any secret-bearing query params replaced by a mask.

    Used so request URLs logged via Flask's request.path / request.full_path do
    not leak the admin or shutdown token into log files, browser history
    proxies, or crash reports. Non-sensitive query params are preserved.

    Param-name matching is case-insensitive: this filter also runs over the
    werkzeug access log, which carries arbitrary attacker-supplied URLs, so a
    differently-cased spelling must not bypass the mask.
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
        if key.lower() in _SENSITIVE_QUERY_PARAMS:
            pairs.append(f"{key}=[REDACTED]")
        else:
            pairs.append(pair if sep else key)
    return f"{path}?{'&'.join(pairs)}"


def require_trusted_or_admin(req, require_origin=True):
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

    R7: the helper no longer accepts a query-string admin token. The SSE
    stream endpoint uses its own ``require_sse_auth`` helper for
    ticket / header authentication, which keeps the policy for URL-borne
    secrets in one place.

    Args:
        req: The Flask request object.
        require_origin: Whether to require a trusted ``Origin`` (loopback mode).

    Returns:
        (ok: bool, reason: str)
    """
    allow_remote = os.environ.get("MNS_ALLOW_REMOTE_API", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    admin_token = os.environ.get("MNS_ADMIN_TOKEN", "").strip()

    if allow_remote and len(admin_token) < 32:
        # Fail closed in remote mode even if startup validation was skipped.
        return False, "admin token must contain at least 32 characters"

    # In remote/proxy mode, the reverse proxy is responsible for admitting the
    # connection and the mandatory admin token is the application-level
    # credential.  The local browser Origin allow-list must not be applied here:
    # it only contains loopback origins and would otherwise make every public
    # same-origin POST fail before its valid token can be checked.
    if allow_remote:
        if not _is_local_request(req):
            return False, "forbidden"
    else:
        ok, reason = require_trusted_state_changing_request(req, require_origin=require_origin)
        if not ok:
            return ok, reason

    if not admin_token:
        return True, ""

    provided = req.headers.get("X-MNS-Admin-Token", "").strip()
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


class SseTicketSessionUnavailable(RuntimeError):
    """Raised when an SSE ticket is requested without a usable browser session.

    Tickets exist purely so that ``EventSource`` (which cannot set headers) can
    authenticate. Any client able to reach this code path without a session
    cookie is by definition not an ``EventSource``, and can authenticate with
    the ``X-MNS-Admin-Token`` header instead, so failing closed here costs no
    legitimate functionality.
    """


def _session_id_for_sse(req) -> tuple[str, bool]:
    """Return ``(identity, session_backed)`` used to bind an SSE ticket.

    The id is persisted in the Flask session (``_sse_sid``) on first use so
    that the browser cookie reliably carries it: ``session.sid`` does not
    exist in the default secure-cookie session. A session-bound ticket issued
    by one browser is therefore useless from another browser (different
    cookie), which is the R4 guarantee.

    R1: the peer address is NOT an acceptable substitute. ``REMOTE_ADDR`` is
    identical for every client on the same host, so on a shared workstation
    (RDP/terminal server, multi-user container) an address-bound ticket could
    be redeemed by a different local user within the TTL. The fallback
    identity is therefore returned with ``session_backed=False`` and is
    rejected by both ``create_sse_ticket`` and ``consume_sse_ticket``; it is
    kept only so callers can log/diagnose the failure. The value is never
    exposed to clients.
    """
    try:
        from flask import session

        sid = session.get("_sse_sid") or session.get("_id")
        if not sid:
            sid = secrets.token_hex(16)
            session["_sse_sid"] = sid
        return f"sid:{sid}", True
    except RuntimeError as exc:
        # Raised by Flask outside a request context, or when no secret key is
        # configured. Narrow on purpose (R5): any other error on this
        # authorization path must surface rather than silently downgrade the
        # ticket binding to a shared-host identity.
        logger.debug("No Flask session available for SSE ticket binding: %s", exc)
    fallback = str(
        req.environ.get("RAW_REMOTE_ADDR")
        or req.environ.get("REMOTE_ADDR")
        or getattr(req, "remote_addr", "")
        or "unknown"
    )
    return f"addr:{fallback}", False


def create_sse_ticket(req, ttl_sec: float | None = None) -> str:
    """Create and store a session-bound, single-use SSE ticket.

    Raises:
        SseTicketSessionUnavailable: if no Flask session backs the request, so
            a ticket that could be redeemed by any client sharing the peer
            address is never issued (R1).
    """
    if ttl_sec is None:
        ttl_sec = SSE_TICKET_TTL_SEC
    identity, session_backed = _session_id_for_sse(req)
    if not session_backed:
        raise SseTicketSessionUnavailable(
            "SSE tickets require a browser session cookie. Non-browser clients "
            "must authenticate with the X-MNS-Admin-Token header instead."
        )
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
        _SSE_TICKETS[ticket] = (identity, expires_at)
    return ticket


def consume_sse_ticket(req, ticket: str) -> bool:
    """Consume a session-bound SSE ticket if it is valid, unexpired, and unused.

    The ticket is removed on any attempt so it cannot be replayed.

    R1: a request without a Flask session can never redeem a ticket. Tickets
    are only ever issued against a session-backed identity, so an
    address-based identity must not be allowed to match one.
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
    identity, session_backed = _session_id_for_sse(req)
    if not session_backed:
        return False
    return secrets.compare_digest(bound_session, identity)


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
    allow_remote = os.environ.get("MNS_ALLOW_REMOTE_API", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    admin_token = os.environ.get("MNS_ADMIN_TOKEN", "").strip()

    if allow_remote and len(admin_token) < 32:
        return False, "admin token must contain at least 32 characters"

    # Match the regular protected-API policy: a remote deployment is admitted
    # by its trusted proxy and must authenticate with the configured admin
    # token, while local deployments retain the loopback/Origin gate.
    if allow_remote:
        if not _is_local_request(req):
            return False, "forbidden"
    else:
        ok, reason = require_trusted_state_changing_request(req, require_origin=require_origin)
        if not ok:
            return ok, reason

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
    if not provided_ticket:
        provided_ticket = (req.cookies.get("sse_ticket") or "").strip()
    if provided_ticket and consume_sse_ticket(req, provided_ticket):
        return True, ""

    return False, "invalid SSE ticket or admin token"


def _is_allowed_shutdown_origin(req):
    """State-changing API 要求の送信元オリジンが許可されているか判定。

    Origin ヘッダのみを信頼する。Referer は Origin より改ざん・欠落が起きやすく、
    オリジン検証の厳格性を弱めるためフォールバックとして使わない。
    """
    allowed_origins = get_allowed_cors_origins()
    normalized_origins = {_normalize_origin(o) for o in allowed_origins}

    origin = _normalize_origin(req.headers.get("Origin") or "")
    return bool(origin) and origin in normalized_origins


def _is_loopback_ip(ip_str: str) -> bool:
    if not ip_str:
        return False
    ip_str = ip_str.strip().lower()

    # Handle IPv6 with port, e.g., [::1]:5000
    if ip_str.startswith("[") and "]" in ip_str:
        bracket_end = ip_str.index("]")
        inner = ip_str[1:bracket_end]
        try:
            addr = ipaddress.ip_address(inner)
            return addr.is_loopback
        except ValueError:
            return False

    # R3: strip a trailing ":port" suffix using a single split, but accept
    # only inputs where exactly one trailing port component is present.
    # IPv4-mapped IPv6 (e.g. ``::ffff:127.0.0.1``) is left intact and resolved
    # by ``ipaddress.ip_address`` below, which recognises it as loopback.
    if ip_str.count(":") == 1:
        ip_str = ip_str.split(":", 1)[0]

    # ``localhost`` is checked AFTER the port is stripped so any backend port
    # works (MNS_BACKEND_PORT is configurable), not just a hardcoded few.
    # The comparison stays an exact match so lookalike names such as
    # ``localhost.attacker.com`` or ``evil-localhost`` are still rejected.
    if ip_str == "localhost":
        return True

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
