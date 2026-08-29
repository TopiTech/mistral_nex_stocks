"""
constants.py - Application-wide constants (single source of truth)

All tunable parameters and magic numbers are defined here.
Other modules should import from this file instead of re-defining.
"""

import os
from pathlib import Path

from requests.exceptions import Timeout as RequestsTimeout

from utils.env_helpers import _env_float, _env_int

try:
    from curl_cffi.requests.exceptions import Timeout as CurlRequestsTimeout
except ImportError:
    CurlRequestsTimeout = RequestsTimeout  # type: ignore[misc,assignment,unused-ignore]

BASE_DIR = Path(__file__).resolve().parent

# ------------------------------
# Backend Server
# ------------------------------

BACKEND_PORT = _env_int("MNS_BACKEND_PORT", 5000, 1, 65535)

# ------------------------------
# Mistral API
# ------------------------------
MISTRAL_API_TIMEOUT_SEC = _env_float("MNS_MISTRAL_API_TIMEOUT", 60.0, 5.0, 180.0)
MISTRAL_MIN_INTERVAL_SEC = _env_float("MNS_MISTRAL_MIN_INTERVAL", 1.35, 0.0, 60.0)
MISTRAL_API_KEY_MIN_LENGTH = _env_int("MNS_MISTRAL_API_KEY_MIN_LENGTH", 32, 8, 256)


def _normalize_mistral_base_url(value: str) -> str:
    """Normalize the Mistral SDK base URL for the installed SDK version.

    The mistralai SDK v2.x builds request URLs as ``server_url + path`` where
    the operation paths already include the version prefix (e.g.
    ``/v1/chat/completions``). Its built-in default server is therefore
    ``https://api.mistral.ai`` *without* ``/v1``. Passing a URL that ends in
    ``/v1`` produces ``/v1/v1/chat/completions`` and a 404 ("no Route matched
    with those values"). This strips a trailing ``/v1`` segment so both the
    default and pre-existing ``MNS_MISTRAL_BASE_URL`` values ending in ``/v1``
    work with the SDK. Proxy / self-hosted URLs keep any custom path prefix.
    """
    base = (value or "").strip().rstrip("/")
    if base.lower().endswith("/v1"):
        base = base[: -len("/v1")].rstrip("/")
    return base or "https://api.mistral.ai"


# Mistral API base URL. Passed to the SDK client as ``server_url`` so a
# self-hosted / proxy Mistral-compatible endpoint can be used.
# NOTE: the SDK appends versioned paths (e.g. ``/v1/chat/completions``)
# itself, so the base URL must NOT include a trailing ``/v1``.
MISTRAL_BASE_URL = _normalize_mistral_base_url(
    os.environ.get("MNS_MISTRAL_BASE_URL", "https://api.mistral.ai")
)
# Per-request retries handled inside the official SDK (transient 5xx / 429).
MISTRAL_SDK_RETRIES = _env_int("MNS_MISTRAL_SDK_RETRIES", 2, 0, 10)
# Random jitter (+/- factor) applied to the global rate-limit wait so that
# multiple threads do not resume in a synchronized burst after a backoff.
MISTRAL_JITTER_FACTOR = _env_float("MNS_MISTRAL_JITTER_FACTOR", 0.1, 0.0, 0.5)
# Additional reasoning-capable model IDs beyond the built-in set
# (comma-separated, e.g. "mistral-large-2512,my-custom-reasoning-model").
MISTRAL_REASONING_MODELS_EXTRA = os.environ.get("MNS_MISTRAL_REASONING_MODELS_EXTRA", "")

# Default & Fallback Model (Small 4 is 100% compatible with both Free and Paid tiers)
DEFAULT_MISTRAL_MODEL = "mistral-small-2603"
MISTRAL_FALLBACK_MODEL = "mistral-small-2603"

# Free vs Paid Model Classifications
MISTRAL_FREE_TIER_MODELS = frozenset({
    "mistral-small-2603",
    "mistral-small-4",
    "mistral-small-latest",
    "ministral-8b-latest",
    "ministral-3b-latest",
    "ministral-14b-latest",
    "codestral-latest",
    "codestral-2508",
    "devstral-2512",
})

MISTRAL_PAID_TIER_MODELS = frozenset({
    "mistral-medium-2604",
    "mistral-medium-3.5",
    "mistral-medium-latest",
    "mistral-large-2512",
    "mistral-large-3",
    "mistral-large-latest",
    "pixtral-large-latest",
})

# ------------------------------
# LangSearch API
# ------------------------------
LANGSEARCH_API_KEY_MIN_LENGTH = _env_int("MNS_LANGSEARCH_API_KEY_MIN_LENGTH", 20, 1, 256)
LANGSEARCH_TIMEOUT = (5.0, 10.0)
# Maximum wall-clock budget for one LangSearch HTTP operation, including
# transient retries and backoff. The per-request timeout above does not bound
# the total duration of a retry sequence.
LANGSEARCH_TOTAL_TIMEOUT_SEC = _env_float("MNS_LANGSEARCH_TOTAL_TIMEOUT", 30.0, 5.0, 120.0)

# ------------------------------
# Tavily API
# ------------------------------
TAVILY_API_KEY_MIN_LENGTH = _env_int("MNS_TAVILY_API_KEY_MIN_LENGTH", 5, 1, 256)
TAVILY_TIMEOUT = (5.0, 10.0)

# ------------------------------
# Stock Disk Cache (survives restarts)
# ------------------------------
STOCK_HISTORY_DISK_CACHE_TTL = _env_int("MNS_STOCK_HISTORY_DISK_CACHE_TTL", 7200, 300, 86400)
STOCK_HISTORY_CACHE_MAXSIZE = _env_int("MNS_STOCK_HISTORY_CACHE_MAXSIZE", 512, 64, 4096)
STOCK_PAYLOAD_DISK_CACHE_TTL = _env_int("MNS_STOCK_PAYLOAD_DISK_CACHE_TTL", 3600, 300, 86400)

# ------------------------------
# yfinance
# ------------------------------
YFINANCE_TIMEOUT_BATCH = _env_int("MNS_YFINANCE_TIMEOUT_BATCH", 20, 1, 120)
YFINANCE_TIMEOUT_SINGLE = _env_int("MNS_YFINANCE_TIMEOUT_SINGLE", 6, 1, 60)
YFINANCE_MAX_RETRIES = _env_int("MNS_YFINANCE_MAX_RETRIES", 3, 0, 10)
YFINANCE_RETRY_WAIT = _env_int("MNS_YFINANCE_RETRY_WAIT", 1, 0, 30)
YFINANCE_RETRY_BACKOFF_BASE = _env_float("MNS_YFINANCE_RETRY_BACKOFF_BASE", 2.0, 1.0, 30.0)
# Short-cache TTL for yfinance data (e.g. fast_info, history)
# Increased from 180s to 300s so that data fetched during one sync cycle
# remains cached through ~6 cycles (30s fetch interval + margin).
# This dramatically reduces redundant fast_info/history calls during sustained operation.
YFINANCE_SHORT_CACHE_TTL = _env_int("MNS_YFINANCE_SHORT_CACHE_TTL", 300, 5, 600)

# yfinance rate-limit backoff and throttling
# Graduated backoff: 15s -> 30s -> 60s -> 120s -> 240s (capped at 600s)
# 2026-07: Reduced initial from 30 to 15, max from 900 to 600. The longer
# backoff values were conservative but kept the app blocked for too long after
# transient blocks (e.g. a single 439 that clears in 30s). The new graduated
# ramp-up is more responsive: short blocks clear fast, sustained blocks still
# escalate exponentially.
YFINANCE_BACKOFF_INITIAL = _env_int("MNS_YFINANCE_BACKOFF_INITIAL", 15, 5, 600)
YFINANCE_BACKOFF_MAX = _env_int("MNS_YFINANCE_BACKOFF_MAX", 600, 30, 3600)
YFINANCE_BACKOFF_MULTIPLIER = _env_float("MNS_YFINANCE_BACKOFF_MULTIPLIER", 2.0, 1.0, 10.0)

# Pause between batch chunk submissions (seconds).
# Reduced from 2.0 to 1.0: chunks now run in parallel via ThreadPoolExecutor,
# so the pause is between chunk submission batches, not between individual
# chunk HTTP calls. The session manager's global pacing handles request spacing.
YFINANCE_BATCH_CHUNK_PAUSE = _env_float("MNS_YFINANCE_BATCH_CHUNK_PAUSE", 1.0, 0.0, 10.0)

# Minimum interval between yfinance requests (seconds)
# 1.0s: the adaptive interval kicks in immediately on any 429/401, so a
# slightly lower floor allows faster normal operation while still providing
# headroom. The session manager's adaptive interval grows on blocks and decays
# during quiet periods.
YFINANCE_MIN_INTERVAL = _env_float("MNS_YFINANCE_MIN_INTERVAL", 1.0, 0.3, 10.0)
# Random jitter factor applied to request intervals (+/- 10%)
YFINANCE_JITTER_FACTOR = _env_float("MNS_YFINANCE_JITTER_FACTOR", 0.1, 0.0, 0.5)
# How much to multiply the min interval when rate-limited
YFINANCE_ADAPTIVE_INTERVAL_FACTOR = _env_float(
    "MNS_YFINANCE_ADAPTIVE_INTERVAL_FACTOR", 3.0, 1.0, 10.0
)
# Short-cache TTL (seconds) used DURING rate-limiting to reduce request pressure
YFINANCE_SHORT_CACHE_TTL_RATE_LIMITED = _env_int(
    "MNS_YFINANCE_SHORT_CACHE_TTL_RATE_LIMITED", 300, 30, 600
)

# ------------------------------
# Web scraper global block detection
# ------------------------------
# Yahoo JP / Kabutan / SBI / Minkabu scrapers share the same IP as yfinance.
# When any of them returns a site-wide block (401/402/403/429/439), ALL scrapers
# pause for a graduated cooldown instead of hammering the upstream providers.
# Graduated backoff: 60s -> 120s -> 240s -> 480s (capped at 600s).
SCRAPER_BACKOFF_INITIAL = _env_int("MNS_SCRAPER_BACKOFF_INITIAL", 60, 10, 600)
SCRAPER_BACKOFF_MAX = _env_int("MNS_SCRAPER_BACKOFF_MAX", 600, 60, 3600)
SCRAPER_BACKOFF_MULTIPLIER = _env_float("MNS_SCRAPER_BACKOFF_MULTIPLIER", 2.0, 1.0, 10.0)

# --- yfinance HTTP request pacing & adaptive throttling (429/401 hardening) ---
# Base minimum spacing between ANY two yfinance HTTP requests. Higher headroom
# directly reduces 429/401 pressure from parallel/looping fetches.
# Bumped from 2.5s -> 3.0s: 401 Invalid Crumb連続ループ対策として
# ベース間隔を広げ、 adaptive interval の成長余裕を確保する。
YFINANCE_REQ_MIN_INTERVAL_BASE = _env_float("MNS_YFINANCE_REQ_MIN_INTERVAL_BASE", 0.5, 0.1, 10.0)
# Hard ceiling for the adaptive spacing interval during sustained rate-limiting.
# 12.0 -> 20.0: 持続的なブロック時にさらに間隔を広げられるようにする。
YFINANCE_REQ_MIN_INTERVAL_MAX = _env_float("MNS_YFINANCE_REQ_MIN_INTERVAL_MAX", 20.0, 2.0, 60.0)
# Multiplier applied to the spacing interval on each block (429/401/402/439).
# 1.6 -> 2.0: ブロック時の成長を加速して早く落ち着かせる (二倍ずつ増やす)。
YFINANCE_REQ_INTERVAL_GROWTH = _env_float("MNS_YFINANCE_REQ_INTERVAL_GROWTH", 2.0, 1.1, 5.0)
# Factor used to relax the interval back toward the base after a quiet period.
# Increased from 0.85 to 0.75: more aggressive decay so the interval recovers
# faster once Yahoo stops blocking. e.g. 20s -> 15s -> 11.25s -> 8.44s -> ...
YFINANCE_REQ_INTERVAL_DECAY = _env_float("MNS_YFINANCE_REQ_INTERVAL_DECAY", 0.75, 0.5, 0.99)
# Seconds of block-free traffic before the adaptive interval begins relaxing.
# Reduced from 30s to 15s: the interval starts decaying sooner after a block
# clears, allowing faster recovery to the base interval.
YFINANCE_REQ_INTERVAL_DECAY_AFTER = _env_float(
    "MNS_YFINANCE_REQ_INTERVAL_DECAY_AFTER", 15.0, 5.0, 300.0
)
# Maximum number of concurrent in-flight yfinance HTTP requests (thundering-herd guard).
# Increased from 2 -> 3: with parallel chunk downloads (max_workers=2), 2 concurrent
# slots could become a bottleneck. 3 slots allow the parallel chunks to overlap
# one request without serializing fully, while still preventing thundering-herd bursts.
YFINANCE_MAX_CONCURRENT_REQUESTS = _env_int("MNS_YFINANCE_MAX_CONCURRENT_REQUESTS", 3, 1, 32)

# --- yfinance session pool bounding (long-running leak hardening) ---
# yfinance holds a keep-alive connection pool (sockets / FDs) per session. The
# session manager used to append every rotated session to a global list that
# was never reclaimed, so over a long run (many 401/429 UA rotations / many
# WSGI worker threads) the process leaked sessions -> FD/memory exhaustion ->
# "gets slow" and eventually "cannot fetch". These bounds cap the pool.
# Hard cap on the number of live yfinance sessions tracked by the manager.
# Oldest (LRU) idle sessions are closed once this is exceeded.
YFINANCE_SESSION_POOL_MAX = _env_int("MNS_YFINANCE_SESSION_POOL_MAX", 64, 8, 512)
# How often the background reaper thread closes idle sessions / enforces the cap.
YFINANCE_SESSION_RECLAIM_INTERVAL_SEC = _env_int(
    "MNS_YFINANCE_SESSION_RECLAIM_INTERVAL_SEC", 600, 30, 7200
)
# A session unused for longer than this is reclaimed by the reaper (seconds).
YFINANCE_SESSION_IDLE_TTL_SEC = _env_int("MNS_YFINANCE_SESSION_IDLE_TTL_SEC", 3600, 60, 86400)

# ------------------------------
# SSE interpolation simulation
# ------------------------------
# When enabled (default), the mode-1 complementary SSE interpolator adds tiny
# random fluctuations to open-market prices so the dashboard looks alive between
# background syncs. Set MNS_SIMULATE_FLUCTUATION=0 to disable the artificial
# noise and show pure linear interpolation toward the last real snapshot.
# Note: mode-2 (TradingView realtime) NEVER simulates — it only forwards real
# producer quotes, so this flag only affects the complementary mode.
SIMULATE_FLUCTUATION = _env_int("MNS_SIMULATE_FLUCTUATION", 1, 0, 1) == 1

# ------------------------------
# Realtime scraper concurrency (Yahoo JP worker loop)
# ------------------------------
# Max parallel scrape requests per polling cycle. Bounded to protect upstream
# providers from burst traffic while keeping a large watchlist responsive.
# Previously hardcoded to 3 in YahooJPRealtimeScraper._worker_loop.
SCRAPER_MAX_WORKERS = _env_int("MNS_SCRAPER_MAX_WORKERS", 3, 1, 8)
# Polite stagger delay (seconds) between per-symbol submissions in the
# concurrent scrape path; keeps the request rate to upstream providers flat
# regardless of the worker count.
SCRAPER_REQUEST_STAGGER_SEC = _env_float("MNS_SCRAPER_REQUEST_STAGGER_SEC", 0.1, 0.0, 1.0)

# ------------------------------
# Circuit Breaker
# ------------------------------
HISTORY_CIRCUIT_BREAKER_THRESHOLD = _env_int("MNS_CIRCUIT_BREAKER_THRESHOLD", 3, 1, 20)
HISTORY_CIRCUIT_BREAKER_OPEN_SEC = _env_int("MNS_CIRCUIT_BREAKER_OPEN_SEC", 20, 1, 600)

# ------------------------------
# News / Research
# ------------------------------
NEWS_CONTEXT_WAIT_TIMEOUT = _env_int("MNS_NEWS_CONTEXT_WAIT_TIMEOUT", 45, 1, 180)
# Upper bound (seconds) a /api/news request thread will wait for a background
# news job to finish before returning fetching:True so the client can poll.
# Keeps the request thread responsive; only genuinely slow jobs fall back to polling.
NEWS_PREPARE_WAIT_SEC = _env_float("MNS_NEWS_PREPARE_WAIT_SEC", 8.0, 0.5, 45.0)
# Upper bound (seconds) a /api/chat or /api/analyze-v2 request thread will wait
# for the background Mistral job to finish before returning fetching:True so the
# client can poll. Keeps worker threads responsive and prevents worker starvation
# under concurrent AI calls (mirrors the /api/news pattern).
CHAT_PREPARE_WAIT_SEC = _env_float("MNS_CHAT_PREPARE_WAIT_SEC", 8.0, 0.5, 45.0)
ANALYZE_RESEARCH_CONTEXT_MAX_CHARS = _env_int(
    "MNS_ANALYZE_RESEARCH_CONTEXT_MAX_CHARS", 2200, 500, 12000
)

# ------------------------------
# Portfolio
# ------------------------------
PORTFOLIO_SHARES_MAX = 1_000_000_000
PORTFOLIO_AVG_PRICE_MAX = 1_000_000_000
PORTFOLIO_AVG_FX_RATE_MAX = 1_000_000.0
PORTFOLIO_TOTAL_VALUE_MAX = 1_000_000_000_000

# ------------------------------
# Request Limits
# ------------------------------
MAX_JSON_SIZE = 1024 * 1024  # 1MB - JSON request body limit
MAX_SSE_LISTENERS = _env_int("MNS_MAX_SSE_LISTENERS", 64, 1, 1000)
MAX_SSE_QUEUE_SIZE = _env_int("MNS_MAX_SSE_QUEUE_SIZE", 100, 10, 1000)


# ------------------------------
# Caching
# ------------------------------
CACHE_DURATION = _env_int("MNS_CACHE_DURATION", 150, 10, 86400)

# Endpoint-specific cache durations (seconds)
CACHE_DURATION_NEWS = _env_int("MNS_CACHE_DURATION_NEWS", 300, 30, 3600)
CACHE_DURATION_HEATMAP = _env_int("MNS_CACHE_DURATION_HEATMAP", 300, 30, 3600)
CACHE_DURATION_SEARCH = _env_int("MNS_CACHE_DURATION_SEARCH", 60, 10, 600)
CACHE_DURATION_TRENDING = _env_int("MNS_CACHE_DURATION_TRENDING", 300, 30, 3600)

# Negative cache (failure-avoidance) TTL
NEGATIVE_CACHE_TTL = _env_int("MNS_NEGATIVE_CACHE_TTL", 90, 10, 600)

# Static file cache-buster TTL
STATIC_MTIME_CACHE_TTL = _env_float("MNS_STATIC_MTIME_CACHE_TTL", 10.0, 1.0, 120.0)

# Stock history endpoint cache durations (market-open vs market-closed, seconds)
HISTORY_CACHE_DURATION_OPEN = _env_int("MNS_HISTORY_CACHE_DURATION_OPEN", 60, 10, 3600)
HISTORY_CACHE_DURATION_OPEN_LONG = _env_int("MNS_HISTORY_CACHE_DURATION_OPEN_LONG", 3600, 60, 86400)
HISTORY_CACHE_DURATION_CLOSED = _env_int("MNS_HISTORY_CACHE_DURATION_CLOSED", 3600, 60, 86400)
HISTORY_CACHE_DURATION_CLOSED_LONG = _env_int(
    "MNS_HISTORY_CACHE_DURATION_CLOSED_LONG", 43200, 3600, 172800
)

# History fetch semaphore capacity and timeout
HISTORY_SEMAPHORE_CAPACITY = _env_int("MNS_HISTORY_SEMAPHORE_CAPACITY", 4, 1, 32)
HISTORY_SEMAPHORE_TIMEOUT = _env_int("MNS_HISTORY_SEMAPHORE_TIMEOUT", 15, 1, 60)

# ------------------------------
# AI Analysis / Chat
# ------------------------------
ANALYSIS_MAX_TOKENS = _env_int("MNS_ANALYSIS_MAX_TOKENS", 2500, 256, 8000)
ANALYSIS_MAX_TOKENS_FALLBACK = _env_int("MNS_ANALYSIS_MAX_TOKENS_FALLBACK", 700, 128, 4000)
CHAT_MAX_TOKENS = _env_int("MNS_CHAT_MAX_TOKENS", 1500, 128, 4000)
CHAT_MAX_MSG_LENGTH = _env_int("MNS_CHAT_MAX_MSG_LENGTH", 2000, 100, 10000)
CHAT_HISTORY_MAX_KEYS = _env_int("MNS_CHAT_HISTORY_MAX_KEYS", 50, 10, 200)
CHAT_HISTORY_MAX_MSGS = _env_int("MNS_CHAT_HISTORY_MAX_MSGS", 30, 3, 50)
# Upper bound on the total characters of chat history (including the system
# message) sent to the LLM on each turn. Older turns are dropped first so the
# request stays within the model context window and cost stays predictable.
CHAT_CONTEXT_MAX_CHARS = _env_int("MNS_CHAT_CONTEXT_MAX_CHARS", 6000, 1000, 40000)
# Max concurrent SSE streaming chat responses. R4: streams use dedicated
# mistral_stream_semaphore (2) so they do not occupy the 3 slots shared by
# non-stream calls (analyze/news); HTTP cap still bounds request threads.
STREAM_CHAT_MAX_CONCURRENT = _env_int("MNS_STREAM_CHAT_MAX_CONCURRENT", 2, 1, 8)
NEWS_SUMMARY_MAX_TOKENS = _env_int("MNS_NEWS_SUMMARY_MAX_TOKENS", 1500, 256, 4000)

# Max tokens for LLM news repair (lower than summary because it's a simpler task)
REPAIR_NEWS_MAX_TOKENS = _env_int("MNS_REPAIR_NEWS_MAX_TOKENS", 1000, 128, 4000)

# Hard ceiling for the max_tokens value actually sent to the Mistral API.
# Must be at least as high as the largest configurable analysis budget
# (ANALYSIS_MAX_TOKENS allows up to 8000) so that configured values are honored
# instead of being silently clamped to a hardcoded 2000. (M-2)
MISTRAL_MAX_TOKENS_CEIL = _env_int("MNS_MISTRAL_MAX_TOKENS_CEIL", 8000, 256, 32000)

# ------------------------------
# Popular Stock Lists
# ------------------------------
POPULAR_US = [
    "AAPL",
    "MSFT",
    "GOOGL",
    "AMZN",
    "NVDA",
    "TSLA",
    "META",
    "NFLX",
    "AVGO",
    "ADBE",
    "COST",
    "PEP",
    "CSCO",
    "INTC",
    "TMUS",
    "CMCSA",
    "AMD",
    "TXN",
    "HON",
    "QCOM",
    "BRK-B",
    "V",
    "JNJ",
    "WMT",
    "JPM",
    "PG",
    "MA",
    "UNH",
    "HD",
    "XOM",
]
# History
VALID_HISTORY_PERIODS: set = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "max"}
VALID_HISTORY_INTERVALS: set = {
    "auto",
    "1m",
    "2m",
    "5m",
    "15m",
    "30m",
    "60m",
    "1h",
    "1d",
    "5d",
    "1wk",
    "1mo",
}

# Stock Input
MAX_STOCK_NAME_LENGTH: int = 200

# Max user-added watchlist entries per market (us / jp / idx). Default display
# stocks (DEFAULT_US/JP/IDX) live outside the user containers and are not
# counted. Every watchlist mutator must enforce this inside user_stocks_lock
# before changing state so a single request cannot register an unbounded
# number of symbols with the realtime engine and cache layers.
MAX_USER_WATCHLIST_ITEMS: int = 100

POPULAR_JP = [
    "7203.T",
    "6758.T",
    "9984.T",
    "8306.T",
    "6861.T",
    "6098.T",
    "9432.T",
    "8035.T",
    "4502.T",
    "7974.T",
    "6501.T",
    "6954.T",
    "8001.T",
    "8058.T",
    "8316.T",
    "4063.T",
    "6702.T",
    "6902.T",
    "6367.T",
    "4568.T",
    "6503.T",
    "8766.T",
    "6273.T",
    "6178.T",
    "9022.T",
    "7267.T",
    "8591.T",
    "6301.T",
    "4519.T",
    "6701.T",
]

# ------------------------------
# Trend / News Search Timeouts
# ------------------------------
TREND_REQUEST_TIMEOUT = (3.0, 5.0)
TREND_SOURCE_RESULT_TIMEOUT_SEC = 12
TREND_SYMBOL_QUERY_LIMIT = 3
TREND_REDDIT_SEARCH_QUERY_LIMIT = 2
TREND_REDDIT_SEARCH_SUBREDDIT_LIMIT = 2

# ------------------------------
# SSE Modes
# ------------------------------
SSE_MODE_DISABLED = 0
SSE_MODE_COMPLEMENTARY = 1
SSE_MODE_TRADINGVIEW = 2

# ------------------------------
# SSE
# ------------------------------
SSE_HEARTBEAT_INTERVAL = _env_int("MNS_SSE_HEARTBEAT_INTERVAL", 15, 3, 120)
SSE_GET_TIMEOUT = _env_float("MNS_SSE_GET_TIMEOUT", 0.5, 0.01, 10.0)
SSE_MARKET_CLOSED_SLEEP = _env_float("MNS_SSE_MARKET_CLOSED_SLEEP", 5.0, 1.0, 120.0)
SSE_MARKET_OPEN_SLEEP = _env_float("MNS_SSE_MARKET_OPEN_SLEEP", 1.0, 0.05, 10.0)
SSE_YAHOO_FETCH_MARKET_CLOSED_SLEEP = _env_float(
    "MNS_SSE_YAHOO_FETCH_MARKET_CLOSED_SLEEP", 300.0, 30.0, 3600.0
)
SSE_YAHOO_FETCH_MARKET_OPEN_SLEEP = _env_float(
    "MNS_SSE_YAHOO_FETCH_MARKET_OPEN_SLEEP", 90.0, 10.0, 600.0
)
SSE_YAHOO_FETCH_NO_LISTENER_SLEEP = _env_float(
    "MNS_SSE_YAHOO_FETCH_NO_LISTENER_SLEEP", 60.0, 10.0, 600.0
)

# Sliding-window size for the SSE event replay log (Last-Event-ID resume).
# Every meaningful event across all connections is recorded; when the buffer no
# longer covers a reconnect gap the stream falls back to a full snapshot.
SSE_EVENT_LOG_MAX = _env_int("MNS_SSE_EVENT_LOG_MAX", 500, 50, 5000)

# Mode 2 (TradingView realtime) periodic full-engine snapshot interval (seconds).
# With per-connection cursors seeded at connect and incremental deltas after, a
# silently dropped frame could otherwise go unnoticed for a whole sync cycle;
# re-emitting the full engine snapshot bounds mode-2 recovery latency.
SSE_MODE2_FULL_SNAPSHOT_INTERVAL_SEC = _env_float(
    "MNS_SSE_MODE2_FULL_SNAPSHOT_INTERVAL_SEC", 5.0, 1.0, 120.0
)

# ------------------------------
# CORS
# ------------------------------
_BASE_ALLOWED_CORS_ORIGINS = {
    f"http://localhost:{BACKEND_PORT}",
    f"http://127.0.0.1:{BACKEND_PORT}",
}
# AI portfolios intentionally model equities only; index watchlist entries
# remain supported by the general stock APIs but are not portfolio holdings.
AI_PORTFOLIO_MARKETS = frozenset(("us", "jp"))
