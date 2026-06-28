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
from datetime import datetime
from pathlib import Path
from typing import Optional
from utils.env_helpers import _env_float, _env_int  # noqa: F401 -- re-exported for other modules



if platform.system().lower() == "windows":
    from ctypes import wintypes
else:
    # Stub for non-Windows platforms
    class wintypes:  # type: ignore
        DWORD = ctypes.c_ulong

try:
    import keyring
    from keyring.errors import KeyringError

    KEYRING_AVAILABLE = True
except ImportError:
    KEYRING_AVAILABLE = False

# --- 定数定義 ---
MISTRAL_MODELS = {
    "1": {"name": "mistral-small-4", "badge": "mistral-small-v4"},
    "2": {"name": "mistral-medium-3.5", "badge": "mistral-medium-v3.5"},
    "3": {"name": "mistral-large-3", "badge": "mistral-large-v3"},
    "4": {"name": "open-mistral-nemo", "badge": "nemo"},
    "5": {"name": "ministral-3-8b", "badge": "ministral-8b"},
    "6": {"name": "ministral-3-3b", "badge": "ministral-3b"},
    "7": {"name": "mistral-large-3", "badge": "pixtral-large"},
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


def _dpapi_unprotect(data: bytes) -> Optional[bytes]:  # pragma: no cover
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
        return None  # None で「復号失敗」を「空データ」と区別する
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
    text = (value or "").strip()
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

    # プレーンテキストへのフォールバックはセキュリティリスクのため完全に削除しました。
    # keyring または DPAPI の利用を強制します。
    error_msg = (
        f"セキュアストレージ (keyring/DPAPI) が利用できません。対象: {key_name}。"
    )
    if keyring_error:
        error_msg += f" KeyringError: {keyring_error}."

    logger.error(
        "No secure storage (keyring/DPAPI) available for '%s'. "
        "Plaintext fallback is no longer supported for security reasons. "
        "On Windows, ensure Credential Manager is functional. "
        "On Linux, ensure dbus/gnome-keyring is installed.",
        key_name,
    )
    raise RuntimeError(
        error_msg
        + " Windowsの場合はコントロールパネルの「資格情報マネージャー」が動作しているか確認してください。 "
        "平文での保存機能はセキュリティ強化のため削除されました。"
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
        logger.warning(
            "Plaintext secret entry for '%s' is no longer supported for security reasons. "
            "Please re-enter and save your credentials securely.",
            key_name,
        )
        return ""

    try:
        payload = base64.b64decode(encoded.encode("ascii"))
    except (ValueError, TypeError, binascii.Error):
        return ""

    if scheme == "dpapi" and _is_windows():
        try:
            decrypted = _dpapi_unprotect(payload)
            if decrypted is None:
                return ""
            payload = decrypted
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


def enforce_secure_permissions(file_path):
    """Enforce owner-only read/write permissions (0o600) on non-Windows platforms."""
    if _is_windows():
        return
    p = Path(file_path)
    if p.exists():
        try:
            p.chmod(0o600)
        except Exception as exc:
            logger.warning("Failed to enforce 0o600 on %s: %s", file_path, exc)


def _rotate_corrupt_backups(directory: Path, limit: int = 5):
    """Keep only the latest N corrupted backup files and remove the older ones."""
    try:
        # Pattern: config.json.corrupt.*.bak
        backups = sorted(
            directory.glob("config.json.corrupt.*.bak"),
            key=lambda p: p.stat().st_mtime
        )
        if len(backups) > limit:
            to_remove = backups[:-limit]
            for p in to_remove:
                try:
                    p.unlink(missing_ok=True)
                    logger.info("Removed old corrupt config backup: %s", p.name)
                except OSError as exc:
                    logger.debug("Failed to remove old corrupt backup %s: %s", p.name, exc)
    except Exception as exc:
        logger.warning("Error during corrupt backups rotation: %s", exc)


def load_config():
    """設定ファイルを読み込む。存在しない場合は初期化"""
    with _CONFIG_LOCK:
        if CONFIG_FILE.exists():
            enforce_secure_permissions(CONFIG_FILE)
        else:
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
                _rotate_corrupt_backups(BASE_DIR)
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
                except PermissionError as perm_exc:
                    logger.warning(
                        "PermissionError during config replace (attempt %d/%d): %s. Retrying...",
                        attempt + 1,
                        max_retries,
                        perm_exc,
                    )
                    if attempt < max_retries - 1:
                        time.sleep(0.1 * (attempt + 1))
                        continue
                    raise
                break  # 成功
            except (OSError, TypeError) as exc:
                logger.warning(
                    "Error during config save (attempt %d/%d): %s. Retrying...",
                    attempt + 1,
                    max_retries,
                    exc,
                )
                if attempt < max_retries - 1:
                    time.sleep(0.1 * (attempt + 1))
                    continue
                if tmp_file.exists():
                    try:
                        tmp_file.unlink()
                    except OSError as unlink_exc:
                        logger.debug("Failed to remove temp config file: %s", unlink_exc)
                logger.error(
                    "Failed to save config to %s after %d attempts: %s",
                    CONFIG_FILE,
                    max_retries,
                    exc,
                    exc_info=True,
                )
                raise

        # Set restrictive file permissions for security on non-Windows systems
        if not _is_windows() and CONFIG_FILE.exists():
            try:
                os.chmod(CONFIG_FILE, 0o600)
            except Exception as exc:
                logger.warning("Failed to set config file permissions: %s", exc)


def get_mistral_api_key():
    """Mistral API鍵を取得"""
    env_key = os.environ.get("MISTRAL_API_KEY")
    if env_key and env_key.strip():
        return env_key.strip()
    return _decode_secret(
        _get_api_credentials_blob().get("mistral_api_key"), "mistral_api_key"
    )


def get_langsearch_api_key():
    """LangSearch API鍵を取得"""
    env_key = os.environ.get("LANGSEARCH_API_KEY")
    if env_key and env_key.strip():
        return env_key.strip()
    return _decode_secret(
        _get_api_credentials_blob().get("langsearch_api_key"), "langsearch_api_key"
    )


def get_tavily_api_key():
    """Tavily API鍵を取得"""
    env_key = os.environ.get("TAVILY_API_KEY")
    if env_key and env_key.strip():
        return env_key.strip()
    return _decode_secret(
        _get_api_credentials_blob().get("tavily_api_key"), "tavily_api_key"
    )


def has_mistral_api_key():
    """Mistral API鍵が設定されているか確認"""
    return bool(get_mistral_api_key())


def has_langsearch_api_key():
    """LangSearch API鍵が設定されているか確認"""
    return bool(get_langsearch_api_key())


def has_tavily_api_key():
    """Tavily API鍵が設定されているか確認"""
    return bool(get_tavily_api_key())


def save_api_credentials(mistral_api_key=None, langsearch_api_key=None, tavily_api_key=None):
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

    if tavily_api_key is not None:
        if str(tavily_api_key).strip():
            encoded = _encode_secret(tavily_api_key, "tavily_api_key")
            if not encoded:
                raise RuntimeError("Failed to securely encode tavily_api_key")
            credentials["tavily_api_key"] = encoded

    cfg["api_credentials"] = credentials
    save_config(cfg)


def clear_api_credentials():
    """全API認証情報を削除"""
    cfg = load_config()
    if KEYRING_AVAILABLE:
        for key_name in ("mistral_api_key", "langsearch_api_key", "tavily_api_key"):
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
        "has_tavily_api_key": has_tavily_api_key(),
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
    cfg["custom_ai_prompt"] = (prompt or "").strip()
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


def get_or_create_master_key() -> str:
    """Get or create the master key for Fernet symmetric encryption."""
    with _CONFIG_LOCK:
        cfg = {}
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                pass
        if not isinstance(cfg, dict):
            cfg = {}

        key_entry = cfg.get("mns_master_key")
        if key_entry and isinstance(key_entry, dict):
            key = _decode_secret(key_entry, "mns_master_key")
            if key:
                return key

        # Generate a new Fernet key
        from cryptography.fernet import Fernet
        new_key = Fernet.generate_key().decode("ascii")
        protected_entry = _encode_secret(new_key, "mns_master_key")
        
        full_cfg = {}
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    full_cfg = json.load(f)
            except Exception:
                pass
        if not isinstance(full_cfg, dict):
            full_cfg = {}
            
        full_cfg["mns_master_key"] = protected_entry
        
        tmp_file = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".tmp")
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(full_cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, CONFIG_FILE)
            enforce_secure_permissions(CONFIG_FILE)
        except Exception as exc:
            logger.error("Failed to save generated master key to config file: %s", exc)
            if tmp_file.exists():
                tmp_file.unlink(missing_ok=True)
                
        return new_key


def protect_data(text: str, key_name: str = "general_data") -> dict:
    """データを Fernet 対称暗号化で安全に保護（暗号化）する"""
    val = (text or "").strip()
    if not val:
        return {"scheme": "fernet", "value": ""}

    master_key = get_or_create_master_key()
    from cryptography.fernet import Fernet
    try:
        f = Fernet(master_key.encode("ascii"))
        encrypted = f.encrypt(val.encode("utf-8"))
        return {
            "scheme": "fernet",
            "value": encrypted.decode("ascii")
        }
    except Exception as exc:
        logger.error("Failed to protect data using Fernet for %s: %s", key_name, exc)
        return dict(_encode_secret(text, key_name))


def unprotect_data(entry: dict, key_name: str = "general_data") -> str:
    """保護されたデータを復号する"""
    if not entry or not isinstance(entry, dict):
        if isinstance(entry, str):
            return _decode_secret(entry, key_name)
        return ""

    scheme = str(entry.get("scheme") or "").strip().lower()

    if scheme == "fernet":
        master_key = get_or_create_master_key()
        from cryptography.fernet import Fernet
        try:
            f = Fernet(master_key.encode("ascii"))
            decrypted = f.decrypt(entry.get("value", "").encode("ascii"))
            return decrypted.decode("utf-8")
        except Exception as exc:
            logger.error("Failed to decrypt Fernet data for %s: %s", key_name, exc)
            return ""

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


