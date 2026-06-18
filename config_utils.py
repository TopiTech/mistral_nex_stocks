"""
統一設定管理モジュール
app.py, switch_model.py の設定読み込み・保存の重複を排除
"""
# pylint: disable=missing-class-docstring,missing-function-docstring,too-many-branches,too-many-locals,too-many-statements,too-many-return-statements,too-many-arguments,too-many-positional-arguments

import base64
import binascii
import copy
import ctypes
import json
import logging
import os
import platform
import shutil
import threading
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import keyring
    from keyring.errors import KeyringError

    KEYRING_AVAILABLE = True
except ImportError:
    KEYRING_AVAILABLE = False

# --- 定数定義 ---
MISTRAL_MODELS = {
    "1": {"name": "mistral-small-latest", "badge": "mistral-small-v4"},
    "2": {"name": "mistral-medium-3.5", "badge": "mistral-medium-v3.5"},
    "3": {"name": "mistral-large-latest", "badge": "mistral-large-v3"},
    "4": {"name": "open-mistral-nemo", "badge": "nemo"},
    "5": {"name": "ministral-8b-latest", "badge": "ministral-8b"},
    "6": {"name": "ministral-3b-latest", "badge": "ministral-3b"},
    "7": {"name": "pixtral-large-latest", "badge": "pixtral-large"},
}

MISTRAL_SUPPORTED_MODELS = {
    "mistral-small-4",
    "mistral-medium-3.5",
    "mistral-medium-3.1",
    "mistral-large-3",
    "mistral-nemo-12b",
    "ministral-3-14b",
    "ministral-3-8b",
    "ministral-3-3b",
    "codestral",
    "devstral-2",
    "open-mistral-nemo",
}

MISTRAL_LEGACY_ALIASES = {
    "mistral-small-latest": "mistral-small-4",
    "mistral-medium-latest": "mistral-medium-3.5",
    "mistral-medium-3-5": "mistral-medium-3.5",
    "mistral-large-latest": "mistral-large-3",
    "open-mistral-nemo": "mistral-nemo-12b",
    "ministral-8b-latest": "ministral-3-8b",
    "ministral-3b-latest": "ministral-3-3b",
    "pixtral-large-latest": "mistral-large-3",
    "magistral-medium-1.2": "mistral-medium-3.5",
}


def _build_mistral_legacy_aliases():
    """Derive additional legacy aliases from MISTRAL_MODELS entries ending in '-latest'.

    For every model in MISTRAL_MODELS whose canonical name ends with '-latest'
    and is not in MISTRAL_SUPPORTED_MODELS, resolve it through
    MISTRAL_LEGACY_ALIASES. This keeps a single source of truth: when a new
    model is added to MISTRAL_MODELS, the derived alias is registered at
    import time without requiring a separate edit.
    """
    derived = {}
    for entry in MISTRAL_MODELS.values():
        name = entry.get("name", "")
        if not name or not name.endswith("-latest"):
            continue
        if name in MISTRAL_SUPPORTED_MODELS:
            continue
        canonical = MISTRAL_LEGACY_ALIASES.get(name)
        if canonical:
            derived[name] = canonical
    return derived


# Augment the legacy alias table at import time so MISTRAL_MODELS additions
# that need aliasing are picked up automatically.
for _alias, _target in _build_mistral_legacy_aliases().items():
    MISTRAL_LEGACY_ALIASES.setdefault(_alias, _target)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
KEYRING_SERVICE_NAME = os.environ.get("MNS_KEYRING_SERVICE", "mistral_nex_stocks")
logger = logging.getLogger(__name__)
_CONFIG_LOCK = threading.RLock()

DEFAULT_CONFIG = {
    "mistral_model": "mistral-medium-3.5",
    "model_badge": "mistral-medium-v3.5",
    "api_credentials": {},
    "allow_plaintext_secrets": False,
    "custom_ai_prompt": "",
}


class DataBlob(ctypes.Structure):  # pragma: no cover
    """DPAPI用のデータ構造体"""

    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _is_windows():
    return platform.system().lower() == "windows"


def _blob_from_bytes(data: bytes):  # pragma: no cover
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    return blob, buffer


def _dpapi_protect(data: bytes) -> bytes:  # pragma: no cover
    if not _is_windows():
        raise RuntimeError("DPAPI is only available on Windows")

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Avoid setting errcheck attribute which may raise TypeError on some Python builds
    in_blob, in_buffer = _blob_from_bytes(data)
    out_blob = DataBlob()
    flags = 0x01  # CRYPTPROTECT_UI_FORBIDDEN

    try:
        if not _crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            None,
            None,
            None,
            None,
            flags,
            ctypes.byref(out_blob),
        ):
            err = ctypes.get_last_error()
            raise ctypes.WinError(err)

        protected = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return protected
    except OSError as dpapi_exc:
        logger.error("DPAPI protection failed with OSError: %s.", dpapi_exc)
        raise
    finally:
        try:
            if out_blob.pbData:
                _kernel32.LocalFree(out_blob.pbData)
        except (AttributeError, TypeError):
            pass
        del in_buffer


def _dpapi_unprotect(data: bytes) -> bytes:  # pragma: no cover
    if not _is_windows():
        raise RuntimeError("DPAPI is only available on Windows")

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    in_blob, in_buffer = _blob_from_bytes(data)
    out_blob = DataBlob()

    try:
        if not _crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(out_blob),
        ):
            err = ctypes.get_last_error()
            raise ctypes.WinError(err)

        plain = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    except OSError:
        # CryptUnprotectData が失敗した場合（データ破損や別ユーザーでの暗号化など）
        logger.debug(
            "DPAPI unprotect failed; data may be corrupted or encrypted by another user"
        )
        return b""
    finally:
        # CryptUnprotectData が失敗した場合でも out_blob.pbData と in_buffer を確実に解放する
        try:
            if out_blob.pbData:
                _kernel32.LocalFree(out_blob.pbData)
        except (AttributeError, TypeError):
            pass
        del in_buffer
    return plain


def _encode_secret(value: str, key_name: str = "default"):
    text = str(value or "").strip()
    if not text:
        return ""

    raw = text.encode("utf-8")

    keyring_error = None
    if KEYRING_AVAILABLE:
        try:
            # key_nameを使用して各APIキーを個別に管理
            keyring.set_password(KEYRING_SERVICE_NAME, key_name, text)
            return {"scheme": "keyring", "value": ""}
        except KeyringError as exc:
            keyring_error = exc
            logger.warning(
                "Keyring protection failed, falling back to DPAPI if available: %s",
                exc,
            )

    if _is_windows():
        try:
            protected = _dpapi_protect(raw)
            return {
                "scheme": "dpapi",
                "value": base64.b64encode(protected).decode("ascii"),
            }
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.error(
                "DPAPI protection failed; unable to securely store secret: %s",
                exc,
                exc_info=True,
            )
            if not keyring_error:
                raise RuntimeError("Secure secret storage unavailable") from exc

    # Fallback to plaintext storage when secure storage is unavailable
    # For safety, plaintext fallback is disabled by default. To opt into insecure
    # storage set the environment variable MNS_ALLOW_PLAINTEXT_SECRETS=1.
    allow_plaintext_env = os.environ.get("MNS_ALLOW_PLAINTEXT_SECRETS", "").lower() in (
        "1",
        "true",
        "yes",
    )
    allow_plaintext_env = allow_plaintext_env or os.environ.get(
        "ALLOW_PLAINTEXT_SECRETS", ""
    ).lower() in ("1", "true", "yes")
    if allow_plaintext_env:
        logger.warning(
            "No secure storage available for '%s'; storing secret in plaintext because MNS_ALLOW_PLAINTEXT_SECRETS is set. "
            "On Windows, consider checking 'Credential Manager' in Control Panel.",
            key_name,
        )
        return {"scheme": "plaintext", "value": text}

    error_msg = (
        f"セキュアストレージ (keyring/DPAPI) が利用できません。対象: {key_name}。"
    )
    if keyring_error:
        error_msg += f" KeyringError: {keyring_error}."

    logger.error(
        "No secure storage (keyring/DPAPI) available for '%s' and plaintext fallback is disabled. "
        "On Windows, ensure Credential Manager is functional. "
        "On Linux, ensure dbus/gnome-keyring is installed. "
        "To allow insecure saving, set environment variable MNS_ALLOW_PLAINTEXT_SECRETS=1.",
        key_name,
    )
    raise RuntimeError(
        error_msg
        + " Windowsの場合はコントロールパネルの「資格情報マネージャー」が動作しているか確認してください。 "
        "プレーンテキストでの保存を許可する場合は環境変数 MNS_ALLOW_PLAINTEXT_SECRETS=1 を設定してください。"
    )


def _decode_secret(entry, key_name: str = "default") -> str:
    if not entry:
        return ""
    if isinstance(entry, str):
        logger.warning(
            "Ignoring legacy plaintext secret entry for '%s'; re-save the credential to migrate it to secure storage.",
            key_name,
        )
        return ""
    if not isinstance(entry, dict):
        return ""

    scheme = str(entry.get("scheme") or "").strip().lower()

    # keyring使用時はkeyringから直接取得
    if scheme == "keyring" and KEYRING_AVAILABLE:
        try:
            # key_nameを使用して各APIキーを個別に取得
            password = keyring.get_password(KEYRING_SERVICE_NAME, key_name)
            return password.strip() if password else ""
        except KeyringError as exc:
            logger.warning("Keyring decryption failed: %s", exc)
            return ""

    encoded = str(entry.get("value") or "").strip()
    if not encoded:
        return ""

    if scheme == "plaintext":
        allow_plaintext_env = os.environ.get(
            "MNS_ALLOW_PLAINTEXT_SECRETS", ""
        ).lower() in ("1", "true", "yes")
        allow_plaintext_env = allow_plaintext_env or os.environ.get(
            "ALLOW_PLAINTEXT_SECRETS", ""
        ).lower() in ("1", "true", "yes")
        if allow_plaintext_env:
            logger.warning(
                "Using plaintext secret entry for '%s' because plaintext fallback is explicitly enabled.",
                key_name,
            )
            return encoded.strip()
        logger.warning(
            "Ignoring plaintext secret entry for '%s' because plaintext fallback is disabled.",
            key_name,
        )
        return ""

    try:
        payload = base64.b64decode(encoded.encode("ascii"))
    except (ValueError, TypeError, binascii.Error):
        return ""

    if scheme == "dpapi" and _is_windows():
        try:
            payload = _dpapi_unprotect(payload)
        except (OSError, RuntimeError):
            return ""

    try:
        return payload.decode("utf-8").strip()
    except (UnicodeDecodeError, AttributeError):
        return ""


def _get_api_credentials_blob(cfg=None):
    source = cfg if isinstance(cfg, dict) else load_config()
    raw = source.get("api_credentials") if isinstance(source, dict) else {}
    return raw if isinstance(raw, dict) else {}


def load_config():
    """設定ファイルを読み込む。存在しない場合は初期化"""
    with _CONFIG_LOCK:
        if not CONFIG_FILE.exists():
            save_config(DEFAULT_CONFIG)
            return copy.deepcopy(DEFAULT_CONFIG)
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            cfg = data if isinstance(data, dict) else {}
            # Ensure default keys
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, copy.deepcopy(v))
            if not isinstance(cfg.get("api_credentials"), dict):
                cfg["api_credentials"] = {}
            return cfg
        except (json.JSONDecodeError, OSError, ValueError) as e:
            corrupt_backup = CONFIG_FILE.with_suffix(
                CONFIG_FILE.suffix + f".corrupt.{datetime.now():%Y%m%d%H%M%S}.bak"
            )
            try:
                shutil.copy2(CONFIG_FILE, corrupt_backup)
                logger.warning(
                    "Corrupted config backed up to %s",
                    corrupt_backup,
                )
            except Exception as backup_exc:  # pylint: disable=broad-exception-caught
                logger.warning(
                    "Failed to backup corrupted config %s: %s",
                    CONFIG_FILE,
                    backup_exc,
                )
            logger.warning(
                "Failed to load config from %s: %s. Using defaults.",
                CONFIG_FILE,
                e,
                exc_info=True,
            )
            return copy.deepcopy(DEFAULT_CONFIG)


def save_config(cfg, create_backup=True):
    """設定ファイルに保存。デフォルト値との統合を保証"""
    with _CONFIG_LOCK:
        data = cfg.copy() if isinstance(cfg, dict) else {}
        for k, v in DEFAULT_CONFIG.items():
            data.setdefault(k, copy.deepcopy(v))
        if not isinstance(data.get("api_credentials"), dict):
            data["api_credentials"] = {}

        # 既存の設定があれば、秘密情報を除いたバックアップを作成 (.bak)
        if create_backup and CONFIG_FILE.exists():
            try:
                backup_data = copy.deepcopy(data)
                if isinstance(backup_data.get("api_credentials"), dict):
                    backup_data["api_credentials"] = {}
                # Strip flask_secret_key from backups to avoid leaking secrets
                if "flask_secret_key" in backup_data:
                    del backup_data["flask_secret_key"]
                backup_file = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".bak")
                with open(backup_file, "w", encoding="utf-8") as f:
                    json.dump(backup_data, f, ensure_ascii=False, indent=2)
                if not _is_windows():
                    try:
                        os.chmod(backup_file, 0o600)
                    except Exception as exc:
                        logger.warning(
                            "Failed to set config backup permissions: %s", exc
                        )
            except (OSError, TypeError) as e:
                logger.warning("Failed to create config backup: %s", e)

        tmp_file = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".tmp")

        # Windowsでのファイルアクセス競合対策（リトライロジック）
        max_retries = 5
        for attempt in range(max_retries):
            try:
                with open(tmp_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)

                # os.replace はアトミックだが、Windowsではファイルが開かれていると失敗する
                try:
                    os.replace(tmp_file, CONFIG_FILE)
                except PermissionError:
                    if attempt < max_retries - 1:
                        time.sleep(0.1 * (attempt + 1))
                        continue
                    raise
                break  # 成功
            except (OSError, TypeError):
                if attempt < max_retries - 1:
                    time.sleep(0.1 * (attempt + 1))
                    continue
                if tmp_file.exists():
                    try:
                        tmp_file.unlink()
                    except OSError:
                        pass
                raise

        # Set restrictive file permissions for security on non-Windows systems
        if not _is_windows() and CONFIG_FILE.exists():
            try:
                os.chmod(CONFIG_FILE, 0o600)
            except Exception as exc:
                logger.warning("Failed to set config file permissions: %s", exc)


def get_mistral_api_key():
    """Mistral API鍵を取得"""
    return _decode_secret(
        _get_api_credentials_blob().get("mistral_api_key"), "mistral_api_key"
    )


def get_langsearch_api_key():
    """LangSearch API鍵を取得"""
    return _decode_secret(
        _get_api_credentials_blob().get("langsearch_api_key"), "langsearch_api_key"
    )


def has_mistral_api_key():
    """Mistral API鍵が設定されているか確認"""
    return bool(get_mistral_api_key())


def has_langsearch_api_key():
    """LangSearch API鍵が設定されているか確認"""
    return bool(get_langsearch_api_key())


def save_api_credentials(mistral_api_key=None, langsearch_api_key=None):
    """API認証情報を安全に保存"""
    cfg = load_config()
    credentials = {
        key: value
        for key, value in _get_api_credentials_blob(cfg).items()
        if isinstance(value, dict)
    }

    if mistral_api_key is not None:
        if str(mistral_api_key).strip():
            encoded = _encode_secret(mistral_api_key, "mistral_api_key")
            if not encoded:
                raise RuntimeError("Failed to securely encode mistral_api_key")
            credentials["mistral_api_key"] = encoded

    if langsearch_api_key is not None:
        if str(langsearch_api_key).strip():
            encoded = _encode_secret(langsearch_api_key, "langsearch_api_key")
            if not encoded:
                raise RuntimeError("Failed to securely encode langsearch_api_key")
            credentials["langsearch_api_key"] = encoded

    cfg["api_credentials"] = credentials
    save_config(cfg)


def clear_api_credentials():
    """全API認証情報を削除"""
    cfg = load_config()
    if KEYRING_AVAILABLE:
        for key_name in ("mistral_api_key", "langsearch_api_key"):
            try:
                keyring.delete_password(KEYRING_SERVICE_NAME, key_name)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.warning(
                    "Keyring credential deletion failed for %s: %s",
                    key_name,
                    exc,
                )
    cfg["api_credentials"] = {}
    save_config(cfg, create_backup=False)


def get_api_credential_state():
    """API認証情報の設定状況を取得"""
    return {
        "has_mistral_api_key": has_mistral_api_key(),
        "has_langsearch_api_key": has_langsearch_api_key(),
    }


def get_model_name():
    """現在のMistralモデル名を取得"""
    return load_config().get("mistral_model", DEFAULT_CONFIG["mistral_model"])


def get_model_badge():
    """現在のモデルバッジ（UI表示用）を取得"""
    return load_config().get("model_badge", DEFAULT_CONFIG["model_badge"])


def get_custom_ai_prompt():
    """カスタムAI分析プロンプトを取得"""
    return load_config().get("custom_ai_prompt", "")


def set_custom_ai_prompt(prompt: str):
    """カスタムAI分析プロンプトを保存"""
    cfg = load_config()
    cfg["custom_ai_prompt"] = str(prompt or "").strip()
    save_config(cfg)


def resolve_model_target(arg: str):
    """
    ユーザー入力からモデル情報を解決

    Args:
        arg: "1", "2", "3" または "mistral-small-latest" など

    Returns:
        {"name": "...", "badge": "..."} または None
    """
    if arg in MISTRAL_MODELS:
        return MISTRAL_MODELS[arg]
    return next((v for v in MISTRAL_MODELS.values() if v["name"] == arg), None)


def get_all_models():
    """利用可能なすべてのモデルを取得"""
    return MISTRAL_MODELS


def protect_data(text: str, key_name: str = "general_data") -> dict:
    """データを安全に保護（暗号化）する"""
    return _encode_secret(text, key_name)


def unprotect_data(entry: dict, key_name: str = "general_data") -> str:
    """保護されたデータを復号する"""
    return _decode_secret(entry, key_name)


def get_or_create_flask_secret_key() -> str:
    """
    Flaskのシークレットキーを取得、または生成して安全に保存する。
    再起動後もセッションを維持するために使用する。
    """
    cfg = load_config()
    secret_entry = cfg.get("flask_secret_key")
    if secret_entry:
        secret = unprotect_data(secret_entry, "flask_secret_key")
        if secret and len(secret) >= 32:
            return secret

    # Generate a new 32-byte hex string (64 characters) if not available or invalid
    import secrets

    new_secret = secrets.token_hex(32)

    # Store it securely
    protected_entry = protect_data(new_secret, "flask_secret_key")
    cfg["flask_secret_key"] = protected_entry
    save_config(cfg)
    return new_secret


def _env_int(
    name: str,
    default: int,
    min_value: Optional[int] = None,
    max_value: Optional[int] = None,
) -> int:
    """Read an integer environment variable with bounds and safe fallback."""
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        logging.getLogger(__name__).warning(
            "Invalid integer env %s=%r; using default %s", name, raw, default
        )
        return default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _env_float(
    name: str,
    default: float,
    min_value: Optional[float] = None,
    max_value: Optional[float] = None,
) -> float:
    """Read a float environment variable with bounds and safe fallback."""
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        logging.getLogger(__name__).warning(
            "Invalid float env %s=%r; using default %s", name, raw, default
        )
        return default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value
