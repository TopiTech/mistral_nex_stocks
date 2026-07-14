# Architecture Overview

## System Architecture

```mermaid
graph TB
    subgraph Browser["Browser"]
        FE[Frontend<br/>HTML/CSS/JS]
        CE[Chrome Extension]
    end

    subgraph Backend["Flask Backend (app.py)"]
        direction TB
        MW[Middleware<br/>CSP/CORS/CSRF/RateLimit]
        BP1[pages_bp<br/>/ /main /setup /settings /heatmap]
        BP2[api_stocks_bp<br/>/api/stocks /api/indices /api/heatmap]
        BP3[api_analysis_bp<br/>/api/analyze-v2 /api/news /api/chat]
        BP4[api_system_bp<br/>/api/health /api/credentials /api/shutdown]
    end

    subgraph Services["Service Layer"]
        AIS[ai_service<br/>Mistral LLM Integration]
        SSS[search_service<br/>DDGS + LangSearch]
        SP[stock_provider<br/>yfinance Abstraction]
    end

    subgraph State["Application State"]
        AS[AppState<br/>app_state.py]
        BG[Background Threads<br/>app_bg.py]
        SSE[SSE Stream<br/>stocks/stream]
    end

    subgraph External["External APIs"]
        MISTRAL[Mistral AI API]
        YF[Yahoo Finance<br/>yfinance]
        DDGS[DuckDuckGo Search]
        LS[LangSearch API]
    end

    FE -->|HTTP| MW
    CE -->|NativeHost| MW
    MW --> BP1 & BP2 & BP3 & BP4
    BP3 --> AIS
    BP3 --> SSS
    BP2 --> SP
    AIS --> MISTRAL
    SP --> YF
    SSS --> DDGS
    SSS --> LS
    BG --> SP
    BG --> SSE
    AS --> BG
```

## Data Flow

```mermaid
sequenceDiagram
    participant U as User Browser
    participant F as Flask Backend
    participant Y as Yahoo Finance
    participant M as Mistral AI
    participant S as Search (DDGS/LS)

    Note over F: Background Thread (every 30-300s)
    F->>Y: fetch_stocks_batch (all symbols)
    Y-->>F: Historical price data
    F->>F: Update target_stocks_cache

    loop Every 0.5s (market open)
        F->>F: Interpolate current→target values
        F->>U: SSE "data:" event
    end

    Note over U: User requests AI Analysis
    U->>F: POST /api/analyze-v2
    F->>S: collect_symbol_research_context
    S-->>F: Research context
    F->>M: call_mistral_chat (structured output)
    M-->>F: StockAnalysis (Pydantic model)
    F->>U: JSON analysis result
```

## Module Structure

| Module              | Responsibility                                                         |
| ------------------- | ---------------------------------------------------------------------- |
| `app.py`            | Flask app init, middleware, error handlers, blueprint registration     |
| `app_state.py`      | Centralized state: AppState, AIState, MarketDataState, CacheState, SSE |
| `app_helpers.py`    | Validation, caching, stock payload building, market hours              |
| `app_bg.py`         | Background threads: yfinance fetch loop, SSE interpolation loop        |
| `config_utils.py`   | Config file I/O, API key encryption (keyring/DPAPI)                    |
| `constants.py`      | Single source of truth for all tunable parameters                      |
| `route_helpers.py`  | Rate limiting, API key extraction, cache helpers                       |
| `error_codes.py`    | ErrorCode enum with ja/en messages                                     |
| `routes/`           | Blueprint route handlers (pages, stocks, analysis, system)             |
| `services/`         | External service integrations (AI, search, stock provider)             |
| `utils/`            | Validators, formatters, env helpers                                    |
| `static/js/`        | Frontend JavaScript (SSE, charts, UI, API client)                      |
| `templates/`        | Jinja2 HTML templates                                                  |
| `chrome_extension/` | Chrome/Edge extension (MV3)                                            |
| `native_host/`      | Windows native messaging host                                          |

## Security Model

```mermaid
graph LR
    subgraph Incoming["Incoming Request"]
        R[Request]
    end

    R -->|1| CSRF[CSRF Check<br/>Sec-Fetch-Site]
    R -->|2| CORS[CORS Validation<br/>Origin Allowlist]
    R -->|3| RL[Rate Limiting<br/>Per-IP + Endpoint]
    R -->|4| LOCAL[Local-Only Check<br/>127.0.0.1 / localhost]
    R -->|5| TOKEN[Token Auth<br/>Shutdown Token]

    CSRF --> OK[Allowed]
    CORS --> OK
    RL --> OK
    LOCAL --> OK
    TOKEN --> OK
```

## Key Design Decisions

1. **Personal Use First**: Designed for local/localhost use, not multi-tenant SaaS
2. **Graceful Degradation**: LangSearch → DDGS fallback, cached data when rate-limited
3. **Structured Outputs**: Mistral Pydantic models for reliable JSON generation
4. **SSE for Real-time**: Server-Sent Events with heartbeat and automatic reconnection
5. **Encrypted Credentials**: keyring > DPAPI (plaintext fallback removed)
