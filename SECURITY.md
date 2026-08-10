# Security Policy

## Supported Versions

Currently, only the latest release version on the main/master branch is supported for security updates.

| Version | Supported          |
| ------- | ------------------ |
| >=3.0   | :white_check_mark: |
| <3.0    | :x:                |

## Threat model (local-first)

This application is designed for **personal use on loopback** (`127.0.0.1`).

- Secrets (API keys, master key, extension token) are never stored as plaintext.
  Storage order: OS keyring → Windows DPAPI → Fernet under master key.
  In a headless non-Windows environment without an available keyring, set a
  persistent `MNS_MASTER_KEY`. `MNS_EPHEMERAL_FALLBACK=1` alone does not make
  an automatically generated key durable. `MNS_ALLOW_EPHEMERAL_MASTER_KEY=1`
  is an explicit development/test-only opt-in; encrypted settings and history
  created with it cannot be recovered after restart.
- Credential endpoints are reachable from localhost + CSRF by default when
  `MNS_ADMIN_TOKEN` is unset (the normal personal/local setup).
- If `MNS_ADMIN_TOKEN` is set, **every** credentials / gated API request must
  present `X-MNS-Admin-Token` (constant-time compare). The first-party browser
  UI does **not** send this header, so leave the token unset for local UI use.
  Configure it only for reverse-proxy / remote clients that can supply the header.
- `MNS_ALLOW_REMOTE_API=1` requires both `MNS_PROXY_FIX=1` and a non-empty
  `MNS_ADMIN_TOKEN` of at least 32 characters. Bootstrap refuses to start
  otherwise (fail-closed).
- Chat history is encrypted at rest (Fernet). Encryption failure is fail-closed:
  messages are not written as plaintext.
- Bootstrap core initialization is fail-closed: on failure the process does not
  mark itself ready and may retry after environment correction.
- Portfolio holdings (`shares`, `avg_price`, P/L) are stripped from unauthenticated
  `/api/stocks` and SSE responses. Mutations still require trusted origin + CSRF.
- Extension `/api/stocks/add_ext` requires loopback + Bearer extension token +
  a trusted `Origin` (missing Origin is rejected).
- The Chrome extension content script uses `<all_urls>` host access to detect
  ticker symbols on any page. It reads page text only in response to an explicit
  `detectTickers` message from the extension popup — there is no automatic
  background collection or exfiltration — and its `host_permissions` for backend
  calls are restricted to loopback (`127.0.0.1`/`localhost`).
- **Native messaging host is not an authentication boundary.** Chrome passes the
  extension origin as an argv argument and the host checks it against the
  message's self-declared `extensionId`; any local (same-user) process can
  reproduce both. The host therefore treats itself as a same-user helper, not a
  privileged bridge: the shutdown token and extension API token are returned to
  any caller passing that check, bounded by the per-action rate limit
  (`NATIVE_HOST_TOKEN_ACTION_MAX`, default 3 per
  `NATIVE_HOST_TOKEN_ACTION_WINDOW`, default 30 s) added as a mitigation. If
  your threat model includes malicious local processes, treat the tokens the
  host returns as exposed and rely on the backend's loopback/Origin gates and
  one-time shutdown semantics (the token can be used once) rather than on the
  host as a secret boundary.

## SSE token-in-URL risk (remote / reverse-proxy mode)

In **remote / reverse-proxy mode** (`MNS_ALLOW_REMOTE_API=1` + `MNS_PROXY_FIX=1`),
the SSE stream endpoint `/api/stocks/stream` avoids accepting the long-lived
admin token directly in the URL. Because `EventSource` cannot set request
headers, the application issues a **short-lived, session-scoped SSE ticket** via
`POST /api/stocks/stream/ticket` and uses that ticket on the subsequent GET
stream request. Long-lived bearer tokens should no longer be passed in the URL.

In this mode short-lived SSE tickets may still appear in:

- Reverse-proxy and backend **access logs** (full URL is typically logged).
- **Browser history** and the `Referer` header sent to any downstream resource.
- Any intermediate hop that records the `Forwarded` / `X-Forwarded-*` chain.

Operational guidance for remote mode:

- Prefer header-based admin authentication for all non-SSE endpoints.
- If SSE tickets are used, **exclude `/api/stocks/stream` (and any request
  carrying `sse_ticket`/`ticket`) from access logging** at the proxy
  (e.g. conditional logging / log_format that drops the query string for
  this path), or keep proxy retention very short.
  _Example (Nginx Conditional Logging):_
  ```nginx
  # Define a map to disable logging for the stream URL
  map $request_uri $loggable {
      ~*/api/stocks/stream  0;
      default               1;
  }
  access_log /var/log/nginx/access.log combined if=$loggable;
  ```
  _Example (Nginx Query Parameter Masking in custom format):_
  ```nginx
  # Alternatively, log the path ($uri) without the query string ($request_uri)
  log_format masked '$remote_addr - $remote_user [$time_local] '
                    '"$request_method $uri $server_protocol" '
                    '$status $body_bytes_sent "$http_referer" '
                    '"$http_user_agent"';
  access_log /var/log/nginx/access.log masked;
  ```
- **Use short-lived or frequently rotated admin tokens.** Never reuse the same
  `MNS_ADMIN_TOKEN` indefinitely; rotate on a schedule and after any suspected
  exposure.
- Prefer `X-MNS-Admin-Token` header auth on every other endpoint — those
  endpoints already reject query-param tokens, so only SSE is exposed to this
  risk.
- On loopback-only (default, personal) deployments this risk is minimal because
  there is no proxy logging the URL and the token is normally unset.

## Legacy workspace config (config.json)

The repository root may contain a `config.json` (legacy/workspace config). On
startup `load_config()` performs a **one-time, process-lifetime** migration of
non-secret preferences (`mistral_model`, `custom_ai_prompt`) from the legacy
config into the per-user runtime config
(`%LOCALAPPDATA%/MistralNeXStocks/config.json` on Windows, or the `MNS_DATA_DIR`
override).

Legacy config merge behaviour:

- **First process start**: If no runtime config exists yet, legacy preferences
  are seeded into the newly created runtime config (one-time migration).
- **Process lifetime**: The legacy config is read at most once per process. To
  apply workspace config changes, restart the backend process so the new
  legacy values are evaluated.
- **Protected keys**: Secrets and generated tokens
  (`api_credentials`, `flask_secret_key`, `mns_master_key`,
  `extension_api_token`) are **never** read from the legacy config. These
  are runtime-authoritative and exist only in the per-user runtime storage.

Do not rely on editing the repo-root `config.json` to change runtime behavior
— use the in-app Settings page or the runtime config file directly.
Committed/checked-in `config.json` files may contain machine-specific secrets
and should be treated as untrusted; the runtime config always takes precedence
over the legacy copy.

## Reporting a Vulnerability

We take the security of this project seriously. If you believe you have found a security vulnerability, please do NOT report it publicly via GitHub Issues.

Instead, please report it privately through one of the following methods:

- Open a private vulnerability report via GitHub Security Advisories if available on the repository.
- Contact the maintainers directly via email or the contact address specified in the repository's main page.

Please include the following details in your report:

- Type of issue (e.g., buffer overflow, SQL injection, XSS)
- Detailed steps to reproduce the vulnerability (proof-of-concept code, payloads, or screenshots are highly appreciated)
- Potential impact of the vulnerability

Once a vulnerability report is received, we will:

1. Acknowledge receipt of the report within 48 hours.
2. Work on a fix or mitigation strategy.
3. Coordinate a public advisory and release a patched version.
