"""ランタイムデータディレクトリとパス解決。`config_store` からの純粋な移動。"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import config_store

logger = logging.getLogger("config_store")


def _get_runtime_data_dir() -> Path:
    """Return the per-user runtime data directory.

    Runtime state should live outside the source tree so that config and stock
    data are not accidentally copied, committed, or bundled with the repo.
    """
    override = os.environ.get("MNS_DATA_DIR") or os.environ.get("MNS_APP_DATA_DIR")
    if override:
        return Path(override).expanduser()

    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if root:
            return Path(root) / "MistralNeXStocks"
        return Path(__file__).resolve().parent.parent / ".data"

    xdg_data = os.environ.get("XDG_DATA_HOME")
    if xdg_data:
        return Path(xdg_data).expanduser() / "mistral_nex_stocks"
    return Path.home() / ".local" / "share" / "mistral_nex_stocks"


def _ensure_runtime_dir() -> None:
    # The directory holds the credential store and its rotation history. Every
    # file inside is written 0o600, but a world-traversable directory still
    # lets other local users enumerate those filenames, and any future file
    # written without an explicit mode inherits the directory default (CFG-10).
    #
    # ``mkdir(mode=...)`` applies the mode only when the directory is created.
    # Several early startup paths (shutdown token, user-stock migration) create
    # APP_DATA_DIR before this helper runs, so an existing directory must be
    # tightened explicitly; otherwise a fresh install keeps the process umask
    # (typically 0o755).
    config_store.APP_DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(config_store.APP_DATA_DIR, 0o700)
        except OSError as exc:
            logger.warning(
                "Could not restrict runtime data directory %s to 0o700: %s",
                config_store.APP_DATA_DIR,
                exc,
            )
