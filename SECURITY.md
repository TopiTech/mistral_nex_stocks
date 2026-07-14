# Security Policy

## Supported Versions

Currently, only the latest release version on the main/master branch is supported for security updates.

| Version | Supported |
| ------- | --------- |
| >=3.0   | :white_check_mark: |
| <3.0    | :x:       |

## Threat model (local-first)

This application is designed for **personal use on loopback** (`127.0.0.1`).

- Secrets (API keys, master key, extension token) are never stored as plaintext.
  Storage order: OS keyring → Windows DPAPI → Fernet under master key.
  Ephemeral fallback only with `MNS_EPHEMERAL_FALLBACK=1`.
- Credential endpoints are reachable from localhost + CSRF by default.
  Set `MNS_ADMIN_TOKEN` for an extra shared-secret gate (`X-MNS-Admin-Token`).
- `MNS_ALLOW_REMOTE_API=1` requires both `MNS_PROXY_FIX=1` and a non-empty
  `MNS_ADMIN_TOKEN`. Bootstrap refuses to start otherwise (fail-closed).
- Portfolio holdings (`shares`, `avg_price`, P/L) are stripped from unauthenticated
  `/api/stocks` and SSE responses. Mutations still require trusted origin + CSRF.
- Extension `/api/stocks/add_ext` requires loopback + Bearer extension token +
  a trusted `Origin` (missing Origin is rejected).

## Reporting a Vulnerability

We take the security of this project seriously. If you believe you have found a security vulnerability, please do NOT report it publicly via GitHub Issues. 

Instead, please report it privately through one of the following methods:
* Open a private vulnerability report via GitHub Security Advisories if available on the repository.
* Contact the maintainers directly via email or the contact address specified in the repository's main page.

Please include the following details in your report:
* Type of issue (e.g., buffer overflow, SQL injection, XSS)
* Detailed steps to reproduce the vulnerability (proof-of-concept code, payloads, or screenshots are highly appreciated)
* Potential impact of the vulnerability

Once a vulnerability report is received, we will:
1. Acknowledge receipt of the report within 48 hours.
2. Work on a fix or mitigation strategy.
3. Coordinate a public advisory and release a patched version.
