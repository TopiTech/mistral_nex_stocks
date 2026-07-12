# utils/storage.py
"""Data persistence logic, managing encrypted saving and loading of user stock configurations."""

import copy
import json
import logging
import os
from pathlib import Path

from app_state import app_state
from config_utils import protect_data, unprotect_data, _is_windows
from constants import BASE_DIR

logger = logging.getLogger(__name__)

USER_STOCKS_FILE = str(BASE_DIR / "user_stocks.json")


def load_user_stocks(force=False):
    """ユーザーの銘柄設定をファイルから読み込む。"""
    if not os.path.exists(USER_STOCKS_FILE):
        return
    try:
        with app_state.market.user_stocks_lock:
            mtime_ns = os.stat(USER_STOCKS_FILE).st_mtime_ns
            if not force and mtime_ns <= app_state.market.last_modified_ns:
                return
            with open(USER_STOCKS_FILE, "r", encoding="utf-8") as f:
                raw_data = json.load(f)

            if (
                isinstance(raw_data, dict)
                and "scheme" in raw_data
                and "value" in raw_data
            ):
                unprotected = unprotect_data(raw_data, key_name="user_stocks")
                if unprotected:
                    data = json.loads(unprotected)
                else:
                    data = {}
                    # Decryption failed or empty, backup corrupted file
                    try:
                        import time
                        backup_file = f"{USER_STOCKS_FILE}.bak.{int(time.time())}"
                        os.replace(USER_STOCKS_FILE, backup_file)
                        logger.warning("Backed up corrupted user_stocks.json to %s due to decryption failure", backup_file)
                    except OSError:
                        pass
            else:
                data = raw_data

            if not isinstance(data, dict):
                data = {}
            app_state.market.user_us = data.get("us", {}) or {}
            app_state.market.user_jp = data.get("jp", {}) or {}
            app_state.market.user_idx = data.get("idx", {}) or {}
            app_state.market.last_usdjpy_rate = float(data.get("last_usdjpy_rate", 150.00))
            app_state.market.last_modified_ns = mtime_ns
    except (IOError, OSError, json.JSONDecodeError) as exc:
        logger.error("Failed to load user stocks: %s", exc)


def save_user_stocks():
    """ユーザーの銘柄設定をファイルに保存する。

    M-5: Uses threading.RLock for write-order safety. The lock is acquired
    externally (via app_state.market.user_stocks_lock), which callers must
    already hold for read operations. The mtime guard in load_user_stocks()
    ensures stale reads are detected.
    """
    try:
        with app_state.market.user_stocks_lock:
            data = {
                "us": copy.deepcopy(app_state.market.user_us),
                "jp": copy.deepcopy(app_state.market.user_jp),
                "idx": copy.deepcopy(app_state.market.user_idx),
                "last_usdjpy_rate": float(getattr(app_state.market, "last_usdjpy_rate", 150.00)),
            }
            encoded = json.dumps(data, ensure_ascii=False, indent=2)
            protected = protect_data(encoded, key_name="user_stocks")

            tmp_file = Path(USER_STOCKS_FILE).with_suffix(".tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(protected, f, ensure_ascii=False, indent=2)

            os.replace(tmp_file, USER_STOCKS_FILE)

            if not _is_windows():
                try:
                    os.chmod(USER_STOCKS_FILE, 0o600)
                except OSError:
                    logger.debug(
                        "Failed to set restrictive permissions on %s", USER_STOCKS_FILE
                    )

            app_state.market.last_modified_ns = os.stat(USER_STOCKS_FILE).st_mtime_ns
    except (IOError, OSError, TypeError) as exc:
        logger.error("Failed to save user stocks: %s", exc)
