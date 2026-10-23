"""指数バックオフのリトライデコレータ。`services.stock_provider` からの純粋な移動。

`yf_session_manager` と `_handle_yf_rate_limit` はテストで
`patch(\"services.stock_provider.X\")` (名前置換) されるため、呼び出し時に
`services.stock_provider` 属性として解決する。`time.sleep` への patch は共有
`time` モジュールに対するものなので直接 import でよい。
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

from requests.exceptions import ConnectionError as RequestsConnectionError

from constants import CurlRequestsTimeout, RequestsTimeout
from services.stock import _shim
from services.stock.errors import _is_yfinance_rate_limit_error

logger = logging.getLogger("services.stock_provider")


# Type variable for the retry decorator
F = TypeVar("F", bound=Callable[..., Any])

_cached_app_state: Any = None
_cached_backoff_base: Any = None


def _get_app_state_cached():
    global _cached_app_state
    if _cached_app_state is None:
        try:
            from app_state import app_state

            _cached_app_state = app_state
        except (ImportError, AttributeError):
            pass
    return _cached_app_state


def _get_backoff_base_cached():
    global _cached_backoff_base
    if _cached_backoff_base is None:
        try:
            from constants import YFINANCE_RETRY_BACKOFF_BASE

            _cached_backoff_base = YFINANCE_RETRY_BACKOFF_BASE
        except (ImportError, AttributeError):
            pass
    return _cached_backoff_base


def with_yfinance_retry(
    func: F | None = None,
    *,
    max_retries: int = 3,
    base_delay: float = 2.0,
    backoff_factor: float = 2.0,
) -> Callable[..., Any] | F:
    """Decorator: exponential-backoff retry for yfinance operations.

    Handles rate limiting (429), timeouts, and transient connection errors.
    Uses exponential backoff: delay = base_delay * backoff_factor^attempt
    plus jitter of ±25%.

    When the app-level yfinance rate limiter is active, retries use even longer
    delays (multiplied by 3x) to avoid hammering Yahoo servers.

    The default backoff_factor is overridden by MNS_YFINANCE_RETRY_BACKOFF_BASE
    when set, allowing runtime configuration without code changes.

    Args:
        max_retries: Maximum number of retry attempts
        base_delay: Initial delay in seconds
        backoff_factor: Multiplier for exponential backoff
    """

    def _rate_limit_multiplier(self_obj: Any = None) -> float:
        """Return 3x multiplier if app-level rate limiter is active."""
        if self_obj and hasattr(self_obj, "_get_market_state"):
            try:
                m_state = self_obj._get_market_state()
                if m_state and m_state.is_yf_rate_limited():
                    return 3.0
            except (AttributeError, RuntimeError):
                pass
            return 1.0

        state_ref = _get_app_state_cached()
        if state_ref is None:
            return 1.0
        try:
            if (
                hasattr(state_ref, "market")
                and state_ref.market
                and state_ref.market.is_yf_rate_limited()
            ):
                return 3.0
            if hasattr(state_ref, "is_yf_rate_limited") and state_ref.is_yf_rate_limited():
                return 3.0
        except (ImportError, AttributeError):
            pass
        return 1.0

    def decorator(f: F) -> F:
        @wraps(f)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # M-6: Evaluate the backoff_factor override at call time (not at
            # decoration/import time) so that environment variable changes in
            # tests and runtime configuration are reflected correctly.
            effective_backoff = backoff_factor
            env_backoff = _get_backoff_base_cached()
            if env_backoff is not None and env_backoff != effective_backoff:
                effective_backoff = env_backoff

            from utils.env_helpers import _is_testing

            is_testing = _is_testing()

            self_obj = args[0] if args else None

            # Capture whether rate-limiting was ALREADY active before starting retries.
            initial_rate_limited = False
            m_state = None
            if self_obj and hasattr(self_obj, "_get_market_state"):
                try:
                    m_state = self_obj._get_market_state()
                    if m_state and m_state.is_yf_rate_limited():
                        initial_rate_limited = True
                except (AttributeError, RuntimeError):
                    pass
            if m_state is None:
                app_state_ref = _get_app_state_cached()
                if app_state_ref:
                    m_state = getattr(app_state_ref, "market", None)
                    if m_state and m_state.is_yf_rate_limited():
                        initial_rate_limited = True

            last_exception: Exception | None = None
            for attempt in range(max_retries + 1):
                try:
                    already_rate_limited = initial_rate_limited
                    if not m_state and self_obj and hasattr(self_obj, "_get_market_state"):
                        try:
                            m_state = self_obj._get_market_state()
                        except (AttributeError, RuntimeError):
                            pass
                    if not m_state:
                        app_state_ref = _get_app_state_cached()
                        if app_state_ref:
                            m_state = getattr(app_state_ref, "market", None)

                    res = f(*args, **kwargs)
                    # Reset rate limit state on successful yfinance request only if not currently rate-limited
                    is_currently_rate_limited = False
                    if m_state:
                        is_currently_rate_limited = m_state.is_yf_rate_limited()
                    elif _shim().yf_session_manager.is_rate_limited("yfinance"):
                        is_currently_rate_limited = True

                    if not already_rate_limited and not is_currently_rate_limited:
                        if m_state:
                            with m_state.yfinance_lock:
                                m_state.is_yfinance_rate_limited = False
                                m_state.yfinance_429_streak = 0
                        try:
                            _shim().yf_session_manager.clear_rate_limit("yfinance")
                            _shim().yf_session_manager.reset_consecutive_401_count()
                        except Exception:  # nosec B110
                            pass
                    return res
                except (TimeoutError, RequestsTimeout, CurlRequestsTimeout) as exc:
                    if isinstance(exc, TimeoutError) and "server overloaded" in str(exc):
                        raise
                    last_exception = exc
                    if attempt < max_retries:
                        if is_testing:
                            time.sleep(0.0001)
                            continue
                        rl_mult = _rate_limit_multiplier(self_obj)
                        delay = base_delay * (effective_backoff**attempt) * rl_mult
                        jitter = delay * random.SystemRandom().uniform(-0.25, 0.25)
                        total_delay = delay + jitter
                        _target = getattr(args[0], "symbol", None) if args else None
                        logger.debug(
                            "yfinance retry %d/%d for %s after timeout, waiting %.2fs (rl_mult=%.1f)",
                            attempt + 1,
                            max_retries,
                            _target or str(args),
                            total_delay,
                            rl_mult,
                        )
                        time.sleep(total_delay)
                except (ConnectionError, OSError, RequestsConnectionError) as exc:
                    last_exception = exc
                    if attempt < max_retries:
                        if is_testing:
                            time.sleep(0.0001)
                            continue
                        rl_mult = _rate_limit_multiplier(self_obj)
                        delay = base_delay * (effective_backoff**attempt) * rl_mult
                        jitter = delay * random.SystemRandom().uniform(-0.25, 0.25)
                        time.sleep(delay + jitter)
                except Exception as exc:
                    # Non-retriable errors: re-raise immediately
                    # Check for yfinance rate limit errors
                    if _is_yfinance_rate_limit_error(exc):
                        last_exception = exc
                        if not m_state and self_obj and hasattr(self_obj, "_get_market_state"):
                            try:
                                m_state = self_obj._get_market_state()
                            except (AttributeError, RuntimeError):
                                pass
                        if not m_state:
                            app_state_ref = _get_app_state_cached()
                            if app_state_ref:
                                m_state = getattr(app_state_ref, "market", None)
                        if m_state:
                            try:
                                _shim()._handle_yf_rate_limit(
                                    exc, m_state, context=f"retry {attempt + 1}/{max_retries}"
                                )
                            except Exception as block_exc:
                                logger.debug("Failed to handle rate limit in retry: %s", block_exc)

                        if attempt < max_retries:
                            # If rate-limiting was ALREADY active before this request started, fast-fail
                            # without sleeping so callers can immediately return cached or fallback data.
                            if initial_rate_limited:
                                logger.info(
                                    "Fast-failing yfinance retry due to pre-existing rate limit (%s)",
                                    type(exc).__name__,
                                )
                                raise
                            if is_testing:
                                time.sleep(0.0001)
                                continue
                            rl_mult = _rate_limit_multiplier(self_obj)
                            # Full jitter (AWS-style): spread retries uniformly in
                            # [0, backoff] to avoid synchronized re-attacks after a 429/401.
                            backoff_delay = max(
                                base_delay * (effective_backoff**attempt) * rl_mult, 1.0
                            )
                            sleep_time = max(0.5, random.SystemRandom().uniform(0.0, backoff_delay))
                            logger.warning(
                                "yfinance rate limited (%s), retry %d/%d after %.1fs (rl_mult=%.1f)",
                                type(exc).__name__,
                                attempt + 1,
                                max_retries,
                                sleep_time,
                                rl_mult,
                            )
                            time.sleep(sleep_time)
                            continue
                    raise
            # All retries exhausted
            if last_exception:
                raise last_exception

        return wrapper  # type: ignore[return-value]

    if func:
        return decorator(func)
    return decorator
