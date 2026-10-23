# pylint: disable=missing-class-docstring,missing-function-docstring,too-many-branches,too-many-locals,too-many-statements,too-many-arguments,too-many-positional-arguments
"""Backend application for Mistral NeX Stocks.

このモジュールは後方互換性を維持するためのファサードです。
実装は `app_factory/` パッケージに関心事ごとに分割されています:
  - app_factory.early: 起動時例外フックと終了時クリーンアップ
  - app_factory.wiring: ProxyFix/secret/static/signals の配線
  - app_factory.hooks: Sec-Fetch-Site/CORS/ログのフック
  - app_factory.warmup: ニュースウォームアップ
  - app_factory.factory: create_app とフック登録
  - app_factory.boot: ランタイムブートストラップ

共有状態 (`_app_bootstrap_done` / `_app_bootstrap_lock`) はこのモジュールが
所有します。`app_factory/` 側はこれらを `app` 属性として参照するため、
テスト隔離と `patch(\"app.X\")` 系モックは従来どおり機能します。
"""

import logging
import os
import threading

from app_bg import (  # noqa: F401 -- 再エクスポート互換 + patch 対象
    _start_background_threads,
    schedule_sync_all_stocks_now,
)
from app_factory.boot import bootstrap
from app_factory.early import (  # noqa: F401
    _cleanup_on_exit,
    _early_excepthook,
    _is_windows_runtime,
)
from app_factory.factory import (  # noqa: F401
    _close_current_thread_chat_db,
    _ensure_bootstrap_called,
    add_request_hooks,
    create_app,
)
from app_factory.hooks import (  # noqa: F401
    _CROSS_SITE_COSTLY_GET_PATHS,
    _enforce_sec_fetch_site_check,
    _log_request_start,
    add_extension_cors_headers,
)
from app_factory.warmup import schedule_news_warmup
from app_factory.wiring import (  # noqa: F401
    RawRemoteAddressMiddleware,
    _apply_proxy_fix,
    _configure_secret_key,
    _configure_static_cache_buster,
    _register_signal_handlers,
)
from app_state import app_state  # noqa: F401 -- `from app import app_state` 互換
from constants import BACKEND_PORT
from credential_manager import (  # noqa: F401 -- 再エクスポート互換 + patch 対象
    get_langsearch_api_key,
    get_tavily_api_key,
)
from utils.env_helpers import _env_bool, _env_int

logger = logging.getLogger(__name__)


# H-1/H-2 improvement: runtime bootstrap for threads and config-less startup.
_app_bootstrap_done = False
_app_bootstrap_lock = threading.Lock()


_schedule_news_warmup_impl = schedule_news_warmup


# #region Global Flask Instance (backward compatibility)


# NOTE: Do NOT call bootstrap() at import time. WSGI servers (gunicorn wsgi:app)
# import this module to obtain `app`, so running bootstrap here would start
# background threads *and* a second bootstrap would run in wsgi.py, producing
# duplicate apps / threads sharing the single app_state singleton. Bootstrap is
# performed exactly once by the entry point (wsgi.py for gunicorn, or the
# __main__ block below for `python app.py`). Tests opt out via MNS_SKIP_BOOTSTRAP.
app = create_app()


# #endregion

# #region Startup Configuration

LANGSEARCH_BASE_URL = os.environ.get("LANGSEARCH_BASE_URL", "https://api.langsearch.com")
LANGSEARCH_WEB_SEARCH_ENDPOINT = f"{LANGSEARCH_BASE_URL}/v1/web-search"

NEWS_PARSE_LOG_SNIPPET_CHARS = _env_int("MNS_NEWS_PARSE_LOG_SNIPPET_CHARS", 1200, 0, 10000)


if __name__ == "__main__":
    # Use wsgi.py as the canonical entry point instead of running this file directly.
    # This is kept for backward compatibility.
    if not _env_bool("MNS_SKIP_BOOTSTRAP"):
        bootstrap(app)
    app.run(debug=False, threaded=True, host="127.0.0.1", port=BACKEND_PORT)

# #endregion
