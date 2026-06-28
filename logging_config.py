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
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app_helpers import _sanitize_error_message
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
    _use_json_format = os.environ.get("LOG_FORMAT", "json").lower() == "json"
except ImportError:
    try:
        from pythonjsonlogger import jsonlogger as _jsonlogger_compat
        _JsonFormatter = _jsonlogger_compat.JsonFormatter  # type: ignore[misc]
        _use_json_format = os.environ.get("LOG_FORMAT", "json").lower() == "json"
    except ImportError:
        _use_json_format = False
        _JsonFormatter = None  # type: ignore[assignment,misc]


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


def init_logging(app) -> None:
    """Configure structured logging with rotation, sanitisation, and filters.

    Must be called once after the Flask app instance is created.
    """
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
    rotating_handler.setFormatter(_log_formatter)
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

    app.logger.info(
        "Logging initialised: level=%s json=%s file=%s error_file=%s",
        _LOG_LEVEL_NAME,
        _use_json_format,
        log_file,
        error_log_file,
    )
