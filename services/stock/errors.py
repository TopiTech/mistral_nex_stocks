"""yfinance エラー分類とレート制限記録。`services.stock_provider` からの純粋な移動。"""

from __future__ import annotations

import logging
from typing import Any

try:
    from yfinance.exceptions import YFRateLimitError
except ImportError:
    # yfinance < 1.5 does not define YFRateLimitError; define a sentinel
    # so isinstance checks never match and fall back to text/status-code
    # heuristics in _is_yfinance_rate_limit_error.
    class YFRateLimitError(Exception):  # type: ignore[no-redef]
        pass


from yfinance.exceptions import (
    YFPricesMissingError,
    YFTickerMissingError,
)

from utils.http_utils import parse_retry_after

logger = logging.getLogger("services.stock_provider")


def _is_yfinance_rate_limit_error(exc: Exception) -> bool:
    """Detect yfinance 401/402/429/439-style blocking failures from exception objects.

    Yahoo Finance / yfinance can surface blocking responses through several HTTP
    status codes and error envelopes:

      * 429 - Too Many Requests (classic rate limit)
      * 401 - Unauthorized / Invalid Crumb (session needs rotation)
      * 402 - Payment Required (data now behind the Yahoo paywall)
      * 439 - Yahoo's "your request was denied / temporarily unavailable" block

    In yfinance 1.5.1 a 429 is raised as ``YFRateLimitError`` (a subclass of
    ``YFException``). That exception carries no ``response``/``status_code``
    attribute — only the message "Too Many Requests. Rate limited. Try after a
    while." So we must check the exception *type* first, before relying on the
    response/status_code heuristics below.

    All of these are treated as retriable-with-backoff conditions so the caller
    rotates the session (UA + crumb) and applies graduated backoff instead of
    hammering the endpoint.
    """
    # Type-based check first: YFRateLimitError is prioritized.
    if isinstance(exc, YFRateLimitError):
        return True

    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    exc_name = type(exc).__name__.lower()
    exc_text = str(exc).lower()

    if status_code in (401, 402, 403, 429, 439):
        return True

    text_markers = (
        "ratelimit",
        "too many requests",
        "rate limit",
        "payment required",
        "unauthorized",
        "invalid crumb",
        "access forbidden",
        "your request was denied",
        "temporarily unavailable",
        "thank you for your patience",
    )
    if any(marker in exc_text for marker in text_markers):
        return True

    # Some yfinance endpoints return the numeric block code inside a JSON body
    # (e.g. {"finance": {"error": {"code": "439", ...}}}). Detect those too.
    try:
        body_callable = getattr(response, "json", None)
        if callable(body_callable):
            try:
                payload = body_callable()
            except (ValueError, TypeError):
                payload = None
            if isinstance(payload, dict):
                code = payload.get("code") or (payload.get("finance") or {}).get("error", {}).get(
                    "code"
                )
                if code in (401, 402, 403, 429, 439, "401", "402", "403", "429", "439"):
                    return True
    except (AttributeError, ValueError, TypeError):
        pass

    return "ratelimit" in exc_name


# Public alias so external modules (e.g. stock_service) can use this utility
# without importing a private (underscore-prefixed) symbol.
is_yfinance_rate_limit_error = _is_yfinance_rate_limit_error


def _is_yfinance_invalid_symbol_error(exc: Exception) -> bool:
    """Detect yfinance errors that mean the *symbol itself is invalid*.

    Yahoo Finance / yfinance raise specific exceptions when a ticker does not
    exist (rather than when the service is temporarily unavailable). These are
    the only failures that should count toward automatic removal of a
    user-added symbol. Transient conditions (rate-limit, timeout, network,
    server error) must NOT be treated as invalid so a temporary outage cannot
    silently delete user stocks.

    Detection strategy (ordered by reliability):
      1. Type-based: ``YFTickerMissingError`` / ``YFPricesMissingError``
      2. HTTP 404 status code on the underlying response object
      3. Text-based heuristics on the exception message (defense-in-depth)
    """
    # Type-based check first: YFTickerMissingError is a strict indicator.
    if isinstance(exc, YFTickerMissingError):
        return True

    # HTTP 404 status code: a 404 from Yahoo means the symbol/ticker does not
    # exist (as opposed to 401/402/429/439 which are transient blocks).
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None) if response is not None else None
    if status_code == 404:
        return True

    # Text-based heuristics on exception message
    exc_text = str(exc).lower()
    text_markers = (
        "no data found",
        "ticker does not exist",
        "symbol may be delisted",
        "delisted",
        "unknown symbol",
        "invalid symbol",
        "http error 404",
        "not found",
        "could not find",
    )
    is_text_matched = any(marker in exc_text for marker in text_markers)

    # YFPricesMissingError can occur on market holidays or transient data gaps.
    # Treat it as invalid ONLY if accompanied by an explicit text marker or 404.
    if isinstance(exc, YFPricesMissingError):
        return is_text_matched

    return is_text_matched


def _handle_yf_rate_limit(exc: Exception, m_state: Any, context: str = "") -> float:
    """yfinance の 401/402/429/439 エラーを検知してバックオフを記録し、その秒数を返す。

    各フェッチ経路で繰り返されていた ``if _is_yfinance_rate_limit_error(...)`` +
    ``m_state.mark_yf_429(...)`` のブロックを一箇所に集約するためのヘルパ。
    呼び出し側は戻り値（バックオフ秒）を参照して return/break 等の制御を行う。
    """
    backoff = m_state.mark_yf_429(retry_after=parse_retry_after(exc))
    logger.warning(
        "yfinance rate limit detected%s; backing off %.0fs",
        f" ({context})" if context else "",
        backoff,
    )
    return backoff
