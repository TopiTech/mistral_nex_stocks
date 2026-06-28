# utils/storage.py
"""Data persistence logic, managing encrypted saving and loading of user stock configurations."""

import copy
import json
import logging
import os
import platform
from pathlib import Path

from app_state import app_state
from config_utils import protect_data, unprotect_data
from constants import BASE_DIR

logger = logging.getLogger(__name__)

USER_STOCKS_FILE = str(BASE_DIR / "user_stocks.json")


def load_user_stocks(force=False):
    """ユーザーの銘柄設定をファイルから読み込む。"""
    if not os.path.exists(USER_STOCKS_FILE):
        return
    try:
        with app_state.user_stocks_lock:
            mtime_ns = os.stat(USER_STOCKS_FILE).st_mtime_ns
            if not force and mtime_ns <= app_state.last_modified_ns:
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
            else:
                data = raw_data

            if not isinstance(data, dict):
                data = {}
            app_state.user_us = data.get("us", {}) or {}
            app_state.user_jp = data.get("jp", {}) or {}
            app_state.user_idx = data.get("idx", {}) or {}
            app_state.last_modified_ns = mtime_ns
    except (IOError, OSError, json.JSONDecodeError) as exc:
        logger.error("Failed to load user stocks: %s", exc)


def save_user_stocks():
    """ユーザーの銘柄設定をファイルに保存する。"""
    try:
        with app_state.user_stocks_lock:
            data = {
                "us": copy.deepcopy(app_state.user_us),
                "jp": copy.deepcopy(app_state.user_jp),
                "idx": copy.deepcopy(app_state.user_idx),
            }
            encoded = json.dumps(data, ensure_ascii=False, indent=2)
            protected = protect_data(encoded, key_name="user_stocks")

            tmp_file = Path(USER_STOCKS_FILE).with_suffix(".tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(protected, f, ensure_ascii=False, indent=2)

            os.replace(tmp_file, USER_STOCKS_FILE)

            if platform.system().lower() != "windows":
                try:
                    os.chmod(USER_STOCKS_FILE, 0o600)
                except OSError:
                    logger.debug(
                        "Failed to set restrictive permissions on %s", USER_STOCKS_FILE
                    )

            app_state.last_modified_ns = os.stat(USER_STOCKS_FILE).st_mtime_ns
    except (IOError, OSError, TypeError) as exc:
        logger.error("Failed to save user stocks: %s", exc)
