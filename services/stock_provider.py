# services/stock_provider.py
"""Stock Data Provider Abstraction Layer for Mistral NeX Stocks.

Provides uniform interface for retrieving stock ticker data, historical series,
batch downloads, and fast attributes.

このモジュールは後方互換性を維持するためのファサードです。
実装は `services/stock/` パッケージに関心事ごとに分割されています:
  - services.stock.sanitize: ファンダメンタル値のサニタイズ
  - services.stock.errors: yfinance エラー分類とレート制限記録
  - services.stock.retry: 指数バックオフのリトライデコレータ
  - services.stock.provider: `BaseStockProvider` / `YFinanceProvider` 本体

`yf_session_manager` と `_handle_yf_rate_limit` はテストで
`patch(\"services.stock_provider.X\")` (名前置換) されるため、このモジュールが
所有し、`services/stock/` 側は呼び出し時に属性解決します。
`yf` / `time` への patch は共有モジュールオブジェクトに対するものです。
"""

import logging
import time  # noqa: F401 -- patch 対象 (`services.stock_provider.time`)

import yfinance as yf  # noqa: F401 -- patch 対象 (`services.stock_provider.yf`)
from yfinance.exceptions import (  # noqa: F401 -- 下位互換の再エクスポート
    YFPricesMissingError,
    YFTickerMissingError,
)

from services.stock.errors import (  # noqa: F401
    YFRateLimitError,
    _handle_yf_rate_limit,
    _is_yfinance_invalid_symbol_error,
    _is_yfinance_rate_limit_error,
    is_yfinance_rate_limit_error,
)
from services.stock.provider import (  # noqa: F401
    BaseStockProvider,
    YFinanceProvider,
)
from services.stock.retry import (  # noqa: F401
    F,
    _cached_app_state,
    _cached_backoff_base,
    _get_app_state_cached,
    _get_backoff_base_cached,
    with_yfinance_retry,
)
from services.stock.sanitize import (  # noqa: F401
    _DROP_FUNDAMENTAL_VALUE,
    _finite_float,
    _sanitize_fundamental_value,
    sanitize_fundamental_dict,
)
from session_manager import yf_session_manager  # noqa: F401 -- patch 対象 (名前置換)

logger = logging.getLogger(__name__)


# Ticker-cache tunables (single owner): `services.stock.provider` resolves
# these via this facade at call time, so reads and writes through
# `services.stock_provider._TICKER_CACHE_MAX` / `_TICKER_CACHE_TTL_SEC`
# keep working exactly as before the split.
_TICKER_CACHE_MAX = 256
_TICKER_CACHE_TTL_SEC = 300.0
