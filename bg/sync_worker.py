# bg/sync_worker.py
"""Stock data fetching, batch processing, background loop, and synchronization.

このモジュールは後方互換性を維持するためのファサードです。
実装は `bg/sync/` パッケージに関心事ごとに分割されています:
  - bg.sync.state: 共有状態・stale 回復・app_bg ミラー accessor
  - bg.sync.fetch: 単体/バッチ/指数取得とフォールバック
  - bg.sync.process: キャッシュ準備・マージ・指数更新・無効銘柄除去
  - bg.sync.control: 全体同期・スケジュール・ループ・watchdog・起動

`acquire_yfinance_slot` / `load_user_stocks` /
`ensure_stock_placeholder_in_caches` はテストで
`patch("bg.sync_worker.X")` (名前置換) されるため、このモジュールが
所有し、`bg/sync/` 側は呼び出し時に `_shim()` 経由で解決します。
`_sync_start_time` / `_sync_generation` はスカラーのため `__getattr__`
で実体 (`bg.sync.state`) から常に live 値を返します (書き換えは
setter 経由)。辞書・ロックは共有オブジェクトの再exportです。
"""

from __future__ import annotations

from typing import Any

from bg.sync.control import (  # noqa: F401
    _run_scheduled_sync_job,
    _start_background_threads,
    _watchdog_restart_dead_realtime_engine,
    bg_yahoo_fetch_loop,
    schedule_sync_all_stocks_now,
    start_background_worker,
    sync_all_stocks_now,
)
from bg.sync.fetch import (  # noqa: F401
    _handle_yfinance_error,
    _invalid_tuple_if_applicable,
    _is_batch_result_invalid,
    extract_batch_history,
    fetch_index_data,
    fetch_stock,
    fetch_stocks_batch,
)
from bg.sync.process import (  # noqa: F401
    _auto_remove_invalid_symbols,
    _prepare_sync_items,
    _process_fetched_stocks,
    _update_indices_data,
    _warm_payload_cache_from_disk,
)
from bg.sync.state import (  # noqa: F401
    _BATCH_INVALID_MARKER,
    SYNC_STALE_LOCK_WAIT_SEC,
    SYNC_STALE_TIMEOUT_SEC,
    _get_sync_execution_lock,
    _get_sync_start_time,
    _last_loaded_mtimes,
    _recover_stale_sync_state_if_needed,
    _set_sync_generation,
    _set_sync_start_time,
    _sync_execution_lock,
)
from route_helpers import ensure_stock_placeholder_in_caches  # noqa: F401 -- patch 対象
from utils.market_utils import acquire_yfinance_slot  # noqa: F401 -- patch 対象
from utils.storage import load_user_stocks  # noqa: F401 -- patch 対象

# Static types for the PEP 562 live scalars below (bare annotations bind
# nothing at runtime, so `__getattr__` still serves the live values).
_sync_start_time: float
_sync_generation: int


def __getattr__(name: str) -> Any:
    """Live scalar reads: `sw._sync_generation` must track `bg.sync.state`."""
    if name in ("_sync_start_time", "_sync_generation"):
        from bg.sync import state as _state

        return getattr(_state, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
