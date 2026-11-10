"""tests/test_logging_config.py - Tests for logging_config and URLMaskingFilter."""

import logging
from unittest.mock import MagicMock

from logging_config import (
    URLMaskingFilter,
    WarningDeduplicationFilter,
    YFinanceNoFundamentalsFilter,
    init_logging,
)


def test_url_masking_filter_msg():
    """Verify that URLMaskingFilter masks sensitive tokens in record.msg."""
    log_filter = URLMaskingFilter()
    record = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="GET https://api.example.com/stream?token=secret123&admin_token=admin456&shutdown_token=shut789",
        args=(),
        exc_info=None,
    )
    assert log_filter.filter(record)
    assert "secret123" not in record.msg
    assert "admin456" not in record.msg
    assert "shut789" not in record.msg
    assert "token=[REDACTED]" in record.msg
    assert "admin_token=[REDACTED]" in record.msg
    assert "shutdown_token=[REDACTED]" in record.msg


def test_url_masking_filter_args_tuple_and_dict():
    """Verify that URLMaskingFilter masks sensitive tokens in record.args."""
    log_filter = URLMaskingFilter()

    # Args as tuple of strings
    record_tuple = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="Connecting to %s",
        args=("https://api.example.com/stream?token=topsecret",),
        exc_info=None,
    )
    assert log_filter.filter(record_tuple)
    assert "topsecret" not in record_tuple.args[0]  # type: ignore[index]
    assert "token=[REDACTED]" in record_tuple.args[0]  # type: ignore[index]

    # Args as dict (standard logging wraps mapping in a 1-tuple)
    record_dict = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="Details: %(endpoint)s",
        args=({"endpoint": "https://api.example.com/test?token=dictsecret"},),
        exc_info=None,
    )
    assert log_filter.filter(record_dict)
    assert isinstance(record_dict.args, dict)
    assert "dictsecret" not in record_dict.args["endpoint"]
    assert "token=[REDACTED]" in record_dict.args["endpoint"]


def test_init_logging_attaches_url_masking_filter_directly_to_handlers(monkeypatch, tmp_path):
    """ISSUE-BE-02: URLMaskingFilter must be directly attached to rotating_handler and error_handler."""
    monkeypatch.setenv("MNS_LOG_DIR", str(tmp_path))

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    root.handlers.clear()

    try:
        mock_app = MagicMock()
        mock_app.logger = logging.getLogger("test_app_logger")
        mock_app.logger.handlers.clear()

        init_logging(mock_app)

        # Verify handlers on root
        rotating_handler = None
        error_handler = None
        for h in root.handlers:
            if getattr(h, "_mns_logging_initialized", False):
                rotating_handler = h
            elif h.level == logging.ERROR:
                error_handler = h

        assert rotating_handler is not None, "rotating_handler must be registered on root logger"
        assert error_handler is not None, "error_handler must be registered on root logger"

        # Check URLMaskingFilter is directly attached to rotating_handler
        rotating_filter_types = [type(f) for f in rotating_handler.filters]
        assert URLMaskingFilter in rotating_filter_types, (
            "URLMaskingFilter must be directly attached to rotating_handler"
        )

        # Check URLMaskingFilter is directly attached to error_handler
        error_filter_types = [type(f) for f in error_handler.filters]
        assert URLMaskingFilter in error_filter_types, (
            "URLMaskingFilter must be directly attached to error_handler"
        )

        # Verify background/service logger record is masked by the handler filter
        bg_record = logging.LogRecord(
            name="service.background_worker",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Background fetch url: https://api.example.com/data?token=supersecret123",
            args=(),
            exc_info=None,
        )
        # Handler filters are evaluated before formatting/emitting
        for f in rotating_handler.filters:
            f.filter(bg_record)

        assert "supersecret123" not in bg_record.msg
        assert "token=[REDACTED]" in bg_record.msg
    finally:
        # Close any open file handlers created during test
        for h in root.handlers:
            h.close()
        root.handlers = saved_handlers


def test_warning_deduplication_filter():
    """Verify WarningDeduplicationFilter suppresses duplicate warnings in window."""
    w_filter = WarningDeduplicationFilter(dedup_window_sec=10.0, max_entries=50)
    record = logging.LogRecord(
        name="test_warn",
        level=logging.WARNING,
        pathname="",
        lineno=0,
        msg="Rate limit warning 429",
        args=(),
        exc_info=None,
    )
    # First time passes
    assert w_filter.filter(record) is True
    # Immediate duplicate within window is suppressed
    assert w_filter.filter(record) is False

    # Different message passes
    record2 = logging.LogRecord(
        name="test_warn",
        level=logging.WARNING,
        pathname="",
        lineno=0,
        msg="Different warning",
        args=(),
        exc_info=None,
    )
    assert w_filter.filter(record2) is True


def test_yfinance_no_fundamentals_filter():
    """Verify YFinanceNoFundamentalsFilter suppresses expected 404/fundamentals noise."""
    yf_filter = YFinanceNoFundamentalsFilter()

    # Suppressed patterns
    rec_404 = logging.LogRecord(
        name="yfinance",
        level=logging.ERROR,
        pathname="",
        lineno=0,
        msg="HTTP Error 404 for ticker",
        args=(),
        exc_info=None,
    )
    assert yf_filter.filter(rec_404) is False

    rec_no_fund = logging.LogRecord(
        name="yfinance",
        level=logging.ERROR,
        pathname="",
        lineno=0,
        msg="No fundamentals data found for ^N225",
        args=(),
        exc_info=None,
    )
    assert yf_filter.filter(rec_no_fund) is False

    # Real errors pass through
    rec_real_error = logging.LogRecord(
        name="yfinance",
        level=logging.ERROR,
        pathname="",
        lineno=0,
        msg="Fatal connection error connecting to server",
        args=(),
        exc_info=None,
    )
    assert yf_filter.filter(rec_real_error) is True
