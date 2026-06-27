"""
constants.py - Application-wide constants (single source of truth)

All tunable parameters and magic numbers are defined here.
Other modules should import from this file instead of re-defining.
"""

import os
from pathlib import Path

from utils.env_helpers import _env_float, _env_int

BASE_DIR = Path(__file__).resolve().parent

# ------------------------------
# Backend Server
# ------------------------------


def _get_backend_port(default=5000):
    port_text = os.environ.get("MNS_BACKEND_PORT", "").strip()
    if not port_text:
        return default
    try:
        port = int(port_text)
        if 1 <= port <= 65535:
            return port
    except ValueError:
        pass
    return default


BACKEND_PORT = _get_backend_port()

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
# yfinance
# ------------------------------
YFINANCE_TIMEOUT_BATCH = _env_int("MNS_YFINANCE_TIMEOUT_BATCH", 20, 1, 120)
YFINANCE_TIMEOUT_SINGLE = _env_int("MNS_YFINANCE_TIMEOUT_SINGLE", 6, 1, 60)
YFINANCE_MAX_RETRIES = _env_int("MNS_YFINANCE_MAX_RETRIES", 2, 0, 10)
YFINANCE_RETRY_WAIT = _env_int("MNS_YFINANCE_RETRY_WAIT", 1, 0, 30)

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
# CORS
# ------------------------------
_BASE_ALLOWED_CORS_ORIGINS = {
    f"http://localhost:{BACKEND_PORT}",
    f"http://127.0.0.1:{BACKEND_PORT}",
}
