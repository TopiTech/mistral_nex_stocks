"""
app_helpers.py - Facade module for backward compatibility.

.. deprecated::
    This module is a backward-compatibility facade that re-exports functions
    from ``utils/`` submodules. New code should import directly from the
    specific ``utils/`` submodules instead:

        utils/text_utils.py      - Text sanitization, token formatting, JSON parsing
        utils/market_utils.py    - Market open/close detection, yfinance slot management
        utils/stock_payload.py   - Stock payload building, portfolio metrics, chart helpers
        utils/networking.py      - CORS, extension origin, local request validation
        utils/normalization.py   - Symbol/market normalization, formatting
        utils/caching.py         - Cache helpers
        utils/storage.py         - User stock I/O

    This facade will be removed in a future version.
"""

# NOTE: This module is a backward-compatibility facade that re-exports
# functions from utils/ submodules. New code should import directly from
# utils/ instead. The DeprecationWarning below is temporarily disabled to
# keep test output clean; re-enable once all callers have been migrated.
# import warnings
# warnings.warn(
#     "app_helpers is deprecated. Import directly from utils/ submodules "
#     "(utils/text_utils, utils/market_utils, utils/stock_payload, etc.) instead.",
#     DeprecationWarning,
#     stacklevel=2,
# )

# Constants originally defined in app_helpers.py (preserved for backward compatibility)
VALID_HISTORY_PERIODS: set = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "max"}
MAX_STOCK_NAME_LENGTH: int = 200


# Re-export from sub-modules for backward compatibility

# Text utilities
from utils.text_utils import (  # noqa: F401
    _short_text,
    _token_fingerprint,
    _token_mask,
    _is_valid_api_key,
    _parse_json_request,
    _sanitize_error_message,
    parse_non_negative_float,
)

# Market utilities
from utils.market_utils import (  # noqa: F401
    _is_market_session_open,
    _market_status_symbol,
    _market_state_from_metadata,
    _fetch_live_market_state,
    is_market_open,
    acquire_yfinance_slot,
    safe_get_ticker,
)

# Stock payload and portfolio helpers
from utils.stock_payload import (  # noqa: F401
    DEFAULT_US,
    DEFAULT_JP,
    DEFAULT_IDX,
    get_default_symbols,
    clear_yfinance_short_cache_prefix,
    _get_stock_container,
    _default_stock_names,
    _stock_is_default_or_user,
    choose_display_name,
    get_stock_info_cached,
    fetch_stock_info_async,
    _extract_portfolio_fields,
    _compute_price_metrics,
    _build_chart_ohlc_data,
    _build_portfolio_metrics,
    build_stock_payload,
    _resolve_stocks_for_response,
    _resolve_indices_for_response,
    _has_ready_indices_snapshot,
    _has_ready_stocks_snapshot,
    _wait_for_initial_market_snapshot,
    error_response,
)

# Third-party re-exports (unchanged)
from utils.http_utils import parse_retry_after  # noqa: F401
from utils.networking import (  # noqa: F401
    _normalize_extension_origin,
    _load_allowed_extension_origins,
    get_allowed_cors_origins,
    require_trusted_state_changing_request,
    require_trusted_or_admin,
    _is_allowed_shutdown_origin,
    _is_loopback_ip,
    _is_local_request,
)
from utils.normalization import (  # noqa: F401
    VALID_MARKETS,
    SYMBOL_PATTERN,
    normalize_market,
    normalize_symbol,
    normalize_text,
    normalize_symbol_for_market,
    is_valid_symbol,
    normalize_optional_number,
    _fmt,
    _fmt_vol,
    normalize_history_frame,
)
from utils.caching import (  # noqa: F401
    sanitize_cache_key,
    get_cached,
    clear_cache_prefix,
    _ensure_cache_bucket,
    _has_cached_key,
    _set_cached_value,
    _get_cached_value,
    get_cached_context_with_negative_cache,
)
from utils.storage import (  # noqa: F401
    load_user_stocks,
    save_user_stocks,
    USER_STOCKS_FILE,
)
