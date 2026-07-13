"""
constants.py - Application-wide constants (single source of truth)

All tunable parameters and magic numbers are defined here.
Other modules should import from this file instead of re-defining.
"""

from pathlib import Path

from utils.env_helpers import _env_float, _env_int
from requests.exceptions import Timeout as RequestsTimeout

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
MISTRAL_API_TIMEOUT_SEC = _env_float("MNS_MISTRAL_API_TIMEOUT", 45.0, 5.0, 180.0)
MISTRAL_MIN_INTERVAL_SEC = _env_float("MNS_MISTRAL_MIN_INTERVAL", 1.35, 0.0, 60.0)
MISTRAL_API_KEY_MIN_LENGTH = _env_int("MNS_MISTRAL_API_KEY_MIN_LENGTH", 32, 8, 256)

# ------------------------------
# LangSearch API
# ------------------------------
LANGSEARCH_API_KEY_MIN_LENGTH = _env_int("MNS_LANGSEARCH_API_KEY_MIN_LENGTH", 20, 1, 256)
LANGSEARCH_TIMEOUT = (5.0, 10.0)

# ------------------------------
# Tavily API
# ------------------------------
TAVILY_API_KEY_MIN_LENGTH = _env_int("MNS_TAVILY_API_KEY_MIN_LENGTH", 5, 1, 256)
TAVILY_TIMEOUT = (5.0, 10.0)

# ------------------------------
# Stock Disk Cache (survives restarts)
# ------------------------------
STOCK_HISTORY_DISK_CACHE_TTL = _env_int(
    "MNS_STOCK_HISTORY_DISK_CACHE_TTL", 7200, 300, 86400
)
STOCK_HISTORY_CACHE_MAXSIZE = _env_int(
    "MNS_STOCK_HISTORY_CACHE_MAXSIZE", 512, 64, 4096
)
STOCK_PAYLOAD_DISK_CACHE_TTL = _env_int(
    "MNS_STOCK_PAYLOAD_DISK_CACHE_TTL", 3600, 300, 86400
)

# ------------------------------
# yfinance
# ------------------------------
YFINANCE_TIMEOUT_BATCH = _env_int("MNS_YFINANCE_TIMEOUT_BATCH", 20, 1, 120)
YFINANCE_TIMEOUT_SINGLE = _env_int("MNS_YFINANCE_TIMEOUT_SINGLE", 6, 1, 60)
YFINANCE_MAX_RETRIES = _env_int("MNS_YFINANCE_MAX_RETRIES", 3, 0, 10)
YFINANCE_RETRY_WAIT = _env_int("MNS_YFINANCE_RETRY_WAIT", 1, 0, 30)
YFINANCE_RETRY_BACKOFF_BASE = _env_float("MNS_YFINANCE_RETRY_BACKOFF_BASE", 2.0, 1.0, 30.0)
# Short-cache TTL for yfinance data (e.g. fast_info, history)
# Increased from 120s to 180s so that data fetched during one sync cycle
# remains cached through ~6 cycles (30s fetch interval + margin).
# This dramatically reduces redundant fast_info/history calls during sustained operation.
YFINANCE_SHORT_CACHE_TTL = _env_int("MNS_YFINANCE_SHORT_CACHE_TTL", 180, 5, 300)

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
YFINANCE_ADAPTIVE_INTERVAL_FACTOR = _env_float("MNS_YFINANCE_ADAPTIVE_INTERVAL_FACTOR", 3.0, 1.0, 10.0)
# Short-cache TTL (seconds) used DURING rate-limiting to reduce request pressure
YFINANCE_SHORT_CACHE_TTL_RATE_LIMITED = _env_int("MNS_YFINANCE_SHORT_CACHE_TTL_RATE_LIMITED", 300, 30, 600)

# --- yfinance HTTP request pacing & adaptive throttling (429/401 hardening) ---
# Base minimum spacing between ANY two yfinance HTTP requests. Higher headroom
# directly reduces 429/401 pressure from parallel/looping fetches.
# Bumped from 2.5s -> 3.0s: 401 Invalid Crumb連続ループ対策として
# ベース間隔を広げ、 adaptive interval の成長余裕を確保する。
YFINANCE_REQ_MIN_INTERVAL_BASE = _env_float("MNS_YFINANCE_REQ_MIN_INTERVAL_BASE", 3.0, 0.5, 10.0)
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
YFINANCE_REQ_INTERVAL_DECAY_AFTER = _env_float("MNS_YFINANCE_REQ_INTERVAL_DECAY_AFTER", 15.0, 5.0, 300.0)
# Maximum number of concurrent in-flight yfinance HTTP requests (thundering-herd guard).
# Increased from 2 -> 3: with parallel chunk downloads (max_workers=2), 2 concurrent
# slots could become a bottleneck. 3 slots allow the parallel chunks to overlap
# one request without serializing fully, while still preventing thundering-herd bursts.
YFINANCE_MAX_CONCURRENT_REQUESTS = _env_int("MNS_YFINANCE_MAX_CONCURRENT_REQUESTS", 3, 1, 32)

# ------------------------------
# Circuit Breaker
# ------------------------------
HISTORY_CIRCUIT_BREAKER_THRESHOLD = _env_int(
    "MNS_CIRCUIT_BREAKER_THRESHOLD", 3, 1, 20
)
HISTORY_CIRCUIT_BREAKER_OPEN_SEC = _env_int(
    "MNS_CIRCUIT_BREAKER_OPEN_SEC", 20, 1, 600
)

# ------------------------------
# News / Research
# ------------------------------
NEWS_CONTEXT_WAIT_TIMEOUT = _env_int(
    "MNS_NEWS_CONTEXT_WAIT_TIMEOUT", 45, 1, 180
)
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
PORTFOLIO_TOTAL_VALUE_MAX = 1_000_000_000_000

# ------------------------------
# Request Limits
# ------------------------------
MAX_JSON_SIZE = 1024 * 1024  # 1MB - JSON request body limit
MAX_SSE_LISTENERS = _env_int("MNS_MAX_SSE_LISTENERS", 8, 1, 100)

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
HISTORY_CACHE_DURATION_CLOSED_LONG = _env_int("MNS_HISTORY_CACHE_DURATION_CLOSED_LONG", 43200, 3600, 172800)

# History fetch semaphore timeout
HISTORY_SEMAPHORE_TIMEOUT = _env_int("MNS_HISTORY_SEMAPHORE_TIMEOUT", 15, 1, 30)

# ------------------------------
# AI Analysis / Chat
# ------------------------------
ANALYSIS_MAX_TOKENS = _env_int("MNS_ANALYSIS_MAX_TOKENS", 2500, 256, 8000)
ANALYSIS_MAX_TOKENS_FALLBACK = _env_int("MNS_ANALYSIS_MAX_TOKENS_FALLBACK", 700, 128, 4000)
CHAT_MAX_TOKENS = _env_int("MNS_CHAT_MAX_TOKENS", 1500, 128, 4000)
CHAT_MAX_MSG_LENGTH = _env_int("MNS_CHAT_MAX_MSG_LENGTH", 2000, 100, 10000)
CHAT_HISTORY_MAX_KEYS = _env_int("MNS_CHAT_HISTORY_MAX_KEYS", 50, 10, 200)
CHAT_HISTORY_MAX_MSGS = _env_int("MNS_CHAT_HISTORY_MAX_MSGS", 30, 3, 50)
NEWS_SUMMARY_MAX_TOKENS = _env_int("MNS_NEWS_SUMMARY_MAX_TOKENS", 1500, 256, 4000)

# Max tokens for LLM news repair (lower than summary because it's a simpler task)
REPAIR_NEWS_MAX_TOKENS = _env_int("MNS_REPAIR_NEWS_MAX_TOKENS", 1000, 128, 4000)

# ------------------------------
# Popular Stock Lists
# ------------------------------
POPULAR_US = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "TSLA", "META", "NFLX", "AVGO",
    "ADBE", "COST", "PEP", "CSCO", "INTC", "TMUS", "CMCSA", "AMD", "TXN",
    "HON", "QCOM", "BRK-B", "V", "JNJ", "WMT", "JPM", "PG", "MA", "UNH",
    "HD", "XOM",
]
POPULAR_JP = [
    "7203.T", "6758.T", "9984.T", "8306.T", "6861.T", "6098.T", "9432.T",
    "8035.T", "4502.T", "7974.T", "6501.T", "6954.T", "8001.T", "8058.T",
    "8316.T", "4063.T", "6702.T", "6902.T", "6367.T", "4568.T", "6503.T",
    "8766.T", "6273.T", "6178.T", "9022.T", "7267.T", "8591.T", "6301.T",
    "4519.T", "6701.T",
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
# SSE
# ------------------------------
SSE_HEARTBEAT_INTERVAL = 15
SSE_MARKET_CLOSED_SLEEP = 10.0
SSE_MARKET_OPEN_SLEEP = 0.5
SSE_YAHOO_FETCH_MARKET_CLOSED_SLEEP = 300.0
SSE_YAHOO_FETCH_MARKET_OPEN_SLEEP = 90.0
SSE_YAHOO_FETCH_NO_LISTENER_SLEEP = 60.0

# ------------------------------
# CORS
# ------------------------------
_BASE_ALLOWED_CORS_ORIGINS = {
    f"http://localhost:{BACKEND_PORT}",
    f"http://127.0.0.1:{BACKEND_PORT}",
}
