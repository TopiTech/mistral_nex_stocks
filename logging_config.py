"""
logging_config.py - Centralized logging configuration for Mistral NeX Stocks.

All logging setup is consolidated here to reduce app.py complexity.
Exports:
    init_logging(app)  - Configure rotating file handlers, JSON/text formatters, filters
    LOG_LEVEL          - Resolved logging level (used by app.py for request tracing)
    DETAILED_API_LOG_PATHS - Paths that get detailed request/response logging
"""

import logging
import os
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from utils.text_utils import _sanitize_error_message
from app_state import BackendLogFilter, PollingFilter

BASE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Log level resolution
# ---------------------------------------------------------------------------
_LOG_LEVEL_NAME = (os.environ.get("BACKEND_LOG_LEVEL", "INFO") or "INFO").upper()
LOG_LEVEL: int = getattr(logging, _LOG_LEVEL_NAME, logging.INFO)

# ---------------------------------------------------------------------------
# Detailed API log path set (used in app.py for request tracing)
# ---------------------------------------------------------------------------
DETAILED_API_LOG_PATHS: set[str] = {
    "/api/chat",
    "/api/news",
    "/api/analyze-v2",
    "/api/credentials",
    "/api/shutdown",
}

# ---------------------------------------------------------------------------
# JSON formatter resolution (supports python-json-logger v2.x and v3.x)
# ---------------------------------------------------------------------------
try:
    from pythonjsonlogger.json import JsonFormatter as _JsonFormatter
    # JSON形式はCI/本番環境(非tty)ではデフォルト有効、tty環境ではtextをデフォルトにする
    import sys as _sys
    _is_tty = hasattr(_sys.stdout, "isatty") and _sys.stdout.isatty()
    _default_log_format = "text" if _is_tty else "json"
    _use_json_format = os.environ.get("LOG_FORMAT", _default_log_format).lower() == "json"
except ImportError:
    try:
        from pythonjsonlogger import jsonlogger as _jsonlogger_compat
        _JsonFormatter = _jsonlogger_compat.JsonFormatter  # type: ignore[misc]
        import sys as _sys
        _is_tty = hasattr(_sys.stdout, "isatty") and _sys.stdout.isatty()
        _default_log_format = "text" if _is_tty else "json"
        _use_json_format = os.environ.get("LOG_FORMAT", _default_log_format).lower() == "json"
    except ImportError:
        _use_json_format = False
        _JsonFormatter = None  # type: ignore[misc,assignment,unused-ignore]


# ---------------------------------------------------------------------------
# Custom formatters
# ---------------------------------------------------------------------------
class SanitizedFormatter(logging.Formatter):
    """Strip sensitive patterns from all log records."""

    def format(self, record):
        formatted = super().format(record)
        return _sanitize_error_message(formatted)


if _use_json_format and _JsonFormatter is not None:

    class CustomJsonFormatter(_JsonFormatter):
        def add_fields(self, log_record, record, message_dict):
            super().add_fields(log_record, record, message_dict)
            log_record["level"] = record.levelname
            log_record["logger"] = record.name
            log_record["timestamp"] = self.formatTime(record, self.datefmt)

    class SanitizedJsonFormatter(CustomJsonFormatter):
        def format(self, record):
            formatted = super().format(record)
            return _sanitize_error_message(formatted)

    _log_formatter: logging.Formatter = SanitizedJsonFormatter(
        "%(timestamp)s %(level)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
else:
    # Plain text format (optimised for development / personal use)
    _log_formatter = SanitizedFormatter(
        "%(asctime)s.%(msecs)03d | %(levelname)-7s | %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


class WarningDeduplicationFilter(logging.Filter):
    """Suppresses duplicate WARNING+ messages within a configurable time window.

    When the same message is logged repeatedly (e.g., rate-limit warnings every
    30s), only the first occurrence in each window is emitted. This prevents
    repetitive warnings from flooding the log while still preserving the first
    occurrence for diagnostics. After the window expires, the next occurrence
    is allowed through as a periodic reminder.

    The internal message cache is capped at *max_entries* (default 500) to
    prevent unbounded memory growth over long-running processes.

    Disabled when *dedup_window_sec* ≤ 0.

    Args:
        dedup_window_sec: Time window in seconds. Default 60.
            Override via ``MNS_LOG_WARNING_DEDUP_SEC`` env var.
        max_entries: Maximum unique messages tracked. Default 500.
    """

    def __init__(self, dedup_window_sec: Optional[float] = None, max_entries: int = 500):
        super().__init__()
        # Allow override via env var for runtime tuning without code changes
        env_value = os.environ.get("MNS_LOG_WARNING_DEDUP_SEC", "")
        if env_value.strip():
            try:
                dedup_window_sec = float(env_value.strip())
            except (ValueError, TypeError):
                pass
        self.dedup_window_sec = max(dedup_window_sec or 60.0, 0.0)
        self.max_entries = max(max_entries or 500, 100)
        self._recent_messages: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.WARNING or self.dedup_window_sec <= 0:
            return True

        # Normalize: strip whitespace, truncate to 200 chars for comparison
        msg = record.getMessage().strip()
        if len(msg) > 200:
            msg = msg[:200]

        now = time.time()

        with self._lock:
            entry = self._recent_messages.get(msg)
            if entry is not None:
                first_time, count = entry
                if now - first_time < self.dedup_window_sec:
                    # Suppress duplicate, increment counter
                    self._recent_messages[msg] = (first_time, count + 1)
                    return False
                # Window expired: reset count and let this one through
                self._recent_messages[msg] = (now, 0)
            else:
                # Cap the dict to prevent unbounded growth
                if len(self._recent_messages) >= self.max_entries:
                    # Evict oldest entry (simple FIFO)
                    try:
                        oldest_key = next(iter(self._recent_messages))
                        del self._recent_messages[oldest_key]
                    except StopIteration:
                        pass
                self._recent_messages[msg] = (now, 0)

        return True


class YFinanceNoFundamentalsFilter(logging.Filter):
    """Filters out noisy yfinance ERROR logs about missing fundamentals data,
    404 (symbol not found), and "possibly delisted" messages.

    yfinance emits ERROR-level log messages for:
      * Index tickers (^N225, ^DJI, etc.) that lack fundamentals data —
        expected behaviour, not an actual error.
      * Symbols that no longer exist on Yahoo Finance (HTTP 404) — e.g. test
        symbols, delisted stocks, or mistyped tickers.
      * Symbols with no price data ("possibly delisted", "No data found").

    All of these are application-level noise that clutter the error log without
    actionable diagnostics.
    """

    # Multiple message variants across yfinance versions
    _SUPPRESSED_PATTERNS = (
        # Fundamentals-data missing (index tickers)
        "No fundamentals data found",
        "no fundamentals data found",
        "fundamentals not available",
        "no fundamentals",
        "could not parse fundamentals",
        # 404 — Symbol not found on Yahoo Finance
        "HTTP Error 404",
        "Quote not found for symbol",
        "symbol may be delisted",
        # No price data (delisted / invalid symbol)
        "possibly delisted",
        "No data found, symbol may be delisted",
        "no price data found",
    )

    def filter(self, record):
        msg = record.getMessage()
        if any(pattern in msg for pattern in self._SUPPRESSED_PATTERNS):
            return False
        return True


def init_logging(app) -> None:
    """Configure structured logging with rotation, sanitisation, and filters.

    Must be called once after the Flask app instance is created.
    Guarded so re-imports / repeated calls (tests, app factory) don't stack
    duplicate root handlers (which would multiply log lines).
    """
    root = logging.getLogger()
    for h in root.handlers:
        if getattr(h, "_mns_logging_initialized", False):
            app.logger.info("Logging already initialised; skipping duplicate setup")
            return

    log_file = BASE_DIR / "backend.log"
    error_log_file = BASE_DIR / "error.log"

    # --- Main rotating handler (all levels) ---
    rotating_handler = RotatingFileHandler(
        str(log_file),
        maxBytes=5 * 1024 * 1024,  # 5MB
        backupCount=3,
        encoding="utf-8",
    )
    rotating_handler.setLevel(LOG_LEVEL)
    rotating_handler.addFilter(BackendLogFilter())
    rotating_handler.addFilter(WarningDeduplicationFilter())
    rotating_handler.setFormatter(_log_formatter)
    rotating_handler._mns_logging_initialized = True  # type: ignore[attr-defined]
    logging.getLogger().addHandler(rotating_handler)

    # --- Dedicated error log (ERROR and above) ---
    error_handler = RotatingFileHandler(
        str(error_log_file),
        maxBytes=2 * 1024 * 1024,  # 2MB
        backupCount=5,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(_log_formatter)
    logging.getLogger().addHandler(error_handler)

    logging.getLogger().setLevel(LOG_LEVEL)
    app.logger.addHandler(rotating_handler)
    app.logger.addHandler(error_handler)
    app.logger.setLevel(LOG_LEVEL)
    app.logger.propagate = False

    # Suppress noisy werkzeug polling logs
    logging.getLogger("werkzeug").addFilter(PollingFilter())

    # Suppress noisy yfinance ERROR logs for index tickers
    # yfinance calls the quoteSummary endpoint for index tickers (^N225, ^DJI, etc.)
    # which returns 404 "No fundamentals data found". This is expected behaviour
    # for indices — suppress these ERROR logs to reduce noise.
    yf_filter = YFinanceNoFundamentalsFilter()
    yf_logger = logging.getLogger("yfinance")
    yf_logger.addFilter(yf_filter)
    yf_logger.setLevel(max(yf_logger.level or 0, logging.WARNING))
    # Apply the same suppression for all yfinance sub-loggers
    for name in list(logging.Logger.manager.loggerDict.keys()):
        if name.startswith("yfinance"):
            sub_logger = logging.getLogger(name)
            sub_logger.addFilter(yf_filter)
            sub_logger.setLevel(max(sub_logger.level or 0, logging.WARNING))

    app.logger.info(
        "Logging initialised: level=%s json=%s file=%s error_file=%s",
        _LOG_LEVEL_NAME,
        _use_json_format,
        log_file,
        error_log_file,
    )
