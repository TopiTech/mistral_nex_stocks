"""起動時例外フックと終了時クリーンアップ。`app` からの純粋な移動。

`_is_windows_runtime` はテストで `patch(\"app._is_windows_runtime\")`
されるため、`_early_excepthook` は呼び出し時に `app` 属性として解決する。
"""

from __future__ import annotations

import atexit
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from app_state import app_state

logger = logging.getLogger("app")


def _is_windows_runtime() -> bool:
    return sys.platform.startswith("win") or os.name == "nt"


def _resolve_is_windows_runtime() -> bool:
    try:
        import app

        return app._is_windows_runtime()
    except (ImportError, AttributeError):
        return _is_windows_runtime()


def _resolve_app_data_log_dir() -> Path:
    """LIFECYCLE-14: prefer the resolved runtime APP_DATA_DIR over the source tree.

    With no MNS_LOG_DIR/MNS_DATA_DIR/MNS_APP_DATA_DIR override, startup crash
    logs must land in the same runtime directory the rest of the app uses
    (config_store.APP_DATA_DIR) instead of accumulating ``backend.log``/
    ``error.log`` inside the project root. The package root is kept only as a
    last resort when APP_DATA_DIR itself cannot be resolved (e.g. config_store
    fails to import during a broken startup), so a crash is still written
    somewhere inspectable.
    """
    try:
        import config_store

        return Path(config_store.APP_DATA_DIR)
    except Exception:
        # Last-resort fallback: the whole hook is best-effort, and a startup
        # crash may break config_store imports, so keep the legacy location
        # reachable rather than losing the traceback entirely.
        return Path(__file__).resolve().parent.parent


def _early_excepthook(exc_type, exc_value, exc_tb):
    """Ensure startup exceptions (e.g. missing dependencies) are written to log files."""
    if exc_type is None:
        sys.__excepthook__(exc_type, exc_value, exc_tb)  # type: ignore[arg-type]
        return
    if isinstance(exc_type, type) and issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    import traceback

    try:
        data_dir_override = (
            os.environ.get("MNS_LOG_DIR")
            or os.environ.get("MNS_DATA_DIR")
            or os.environ.get("MNS_APP_DATA_DIR")
        )
        log_dirs: list[Path] = []
        if data_dir_override:
            log_dirs.append(Path(data_dir_override).expanduser())
        elif _resolve_is_windows_runtime():
            root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
            if root:
                log_dirs.append(Path(root) / "MistralNeXStocks")
        else:
            xdg_data = os.environ.get("XDG_DATA_HOME")
            if xdg_data:
                log_dirs.append(Path(xdg_data).expanduser() / "mistral_nex_stocks")
            else:
                try:
                    log_dirs.append(Path.home() / ".local" / "share" / "mistral_nex_stocks")
                except Exception:
                    pass
        is_testing = bool(
            os.environ.get("PYTEST_CURRENT_TEST")
            or os.environ.get("MNS_TESTING")
            or os.environ.get("TESTING")
        )
        if not data_dir_override and not is_testing:
            # LIFECYCLE-14: crash logs go to the runtime data dir, not the
            # source tree (APP_DATA_DIR resolution, same as the rest of the app).
            fallback_dir = _resolve_app_data_log_dir()
            if fallback_dir not in log_dirs:
                log_dirs.append(fallback_dir)

        tb_lines = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        entry = f"[{timestamp}] CRITICAL startup exception:\n{tb_lines}\n"

        for l_dir in log_dirs:
            try:
                l_dir.mkdir(parents=True, exist_ok=True)
                for name in ("backend.log", "error.log"):
                    target = l_dir / name
                    with target.open("a", encoding="utf-8") as f:
                        f.write(entry)
            except Exception:
                pass
    except Exception:
        pass
    sys.__excepthook__(exc_type, exc_value, exc_tb)


sys.excepthook = _early_excepthook


# Ensure global HTTP sessions, SQLite connections, and managers are closed on process
# exit to avoid ResourceWarning / WAL corruption. This is the last-resort cleanup
# that fires after all teardown_appcontext hooks have run.
def _cleanup_on_exit():
    # R9 fix: consolidated single atexit handler. Previously two separate
    # handlers (_cleanup_on_exit + app_state.shutdown_executors) were registered
    # with no guaranteed ordering; now executors shut down first, then DB
    # connections are closed.
    try:
        app_state.shutdown_executors()
    except Exception as exc:
        logger.debug("Cleanup of executors: %s", exc)

    try:
        if hasattr(app_state, "ai") and hasattr(app_state.ai, "chat_history"):
            chat_store = app_state.ai.chat_history
            if hasattr(chat_store, "close_all"):
                chat_store.close_all()
            else:
                chat_store.close()
    except Exception as exc:
        logger.debug("Cleanup of chat database: %s", exc)


atexit.register(_cleanup_on_exit)

# Signal handlers are registered in _register_signal_handlers() inside
# create_app() so that they include executor cleanup. See #endregion below.
