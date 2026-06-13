---
name: security-review
description: Systematic multi-component security code review for Flask+Chrome extension+Native Host apps
---

# Security Review Skill

Perform a comprehensive security-focused code review of a multi-component application with Flask backend, Chrome/Edge extension, and Native Messaging host.

## When to use

- User requests a security review or code review with security focus
- Reviewing a Flask + Chrome extension + Native Host application
- Need to check for: API security, secret management, XSS, input validation, payload handling

## Workflow

### Phase 1: Parallel Component Exploration

Launch 2-3 explore subagents in parallel to understand each major component:

**Agent 1 - Flask Backend:**
- Main Flask app file and all API routes
- Secret/API key management (keyring, DPAPI, env vars)
- External API calls (Mistral, yfinance, DuckDuckGo, LangSearch)
- Error handling and input validation patterns
- SSE implementations, cache, rate limiting
- Shutdown and metrics endpoints

**Agent 2 - Chrome Extension:**
- manifest.json permissions and CSP
- Background scripts, content scripts, popup/options
- Extension ↔ Flask backend communication
- Extension ↔ Native Messaging host communication
- XSS protections and input sanitization
- Async processing patterns

**Agent 3 - Native Host (if separate):**
- Native Messaging host implementation
- Large payload handling
- JSON parsing and validation
- Error handling
- Dependencies and configuration

### Phase 2: Systematic File Reading

Read key files from each component to understand implementation details:

**Flask files to read:**
- `app.py` - main app, middleware, shutdown
- `routes/api_system.py` - system endpoints (shutdown, metrics)
- `routes/api_stocks.py` - stock data endpoints
- `routes/api_analysis.py` - AI analysis endpoints
- `services/ai_service.py` - Mistral API integration
- `services/search_service.py` - DuckDuckGo/LangSearch
- `config_utils.py` - configuration management
- `error_codes.py` - error handling
- `utils/validators.py` - input validation

**Chrome extension files:**
- `chrome_extension/manifest.json`
- `chrome_extension/background.js`
- `chrome_extension/popup.js`

**Native host files:**
- `native_host/native_host.py`
- `native_host/start_backend.py`

**Config and tests:**
- `requirements.txt`
- `config.json`
- `pytest.ini`
- `tests/` directory

### Phase 3: Security Analysis

Check each component against these security concerns:

**Flask API Security:**
- [ ] Input validation on all endpoints
- [ ] Exception handling (no stack traces to client)
- [ ] Origin/Referer header validation
- [ ] localhost restriction enforcement
- [ ] `/api/shutdown` abuse risk
- [ ] `/api/metrics` information leakage
- [ ] API keys not in logs/responses/config files

**Secret Management:**
- [ ] keyring / DPAPI usage (no plaintext fallback)
- [ ] No secrets in git history
- [ ] Secure credential storage

**Native Messaging:**
- [ ] Large payload handling (no memory exhaustion)
- [ ] JSON parsing validation
- [ ] Extension ID validation
- [ ] Input sanitization

**Chrome Extension:**
- [ ] XSS protections in popup/content scripts
- [ ] Minimal permissions (no over-privileged)
- [ ] Async processing error handling
- [ ] Content Security Policy compliance

**External API Integration:**
- [ ] Timeout configuration
- [ ] Retry logic with backoff
- [ ] 429/5xx error handling
- [ ] Response validation

**Cross-cutting Concerns:**
- [ ] Cache invalidation
- [ ] Rate limiting
- [ ] SSE connection management
- [ ] Concurrent request handling
- [ ] Windows/macOS/Linux compatibility

### Phase 4: Report Findings

Structure findings by severity:

**Critical** - Immediate security risks
**High** - Significant vulnerabilities
**Medium** - Security improvements needed
**Low** - Best practice recommendations

For each finding include:
- Component and file path
- Line numbers if applicable
- Description of the issue
- Recommended fix
- Risk assessment

## Example Usage

```
User: "以下のコード差分を、実運用前提で厳しめにレビューしてください。レビュー中はweb検索を用いて最新情報を参照してください。"
User: "Run a security review of the Flask backend and Chrome extension"
```

## Tips

- Use web search to verify current security best practices
- Check for known CVEs in dependencies
- Test error handling by reviewing exception paths
- Look for hardcoded secrets or credentials
- Verify HTTPS enforcement where applicable
