"""ランタイムデータディレクトリとパス解決。`config_store` からの純粋な移動。"""

from __future__ import annotations

import os
from pathlib import Path

import config_store


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
    config_store.APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
