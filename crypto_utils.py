"""
暗号化ユーティリティモジュール
config_utils.py から抽出した DPAPI/Fernet 暗号化関連の関数群
"""
# pylint: disable=missing-function-docstring,too-many-branches

import base64
import binascii
import ctypes
import logging
import os
import platform
from typing import Any, Optional

logger = logging.getLogger(__name__)

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

KEYRING_SERVICE_NAME = os.environ.get("MNS_KEYRING_SERVICE", "mistral_nex_stocks")

# In-memory ephemeral storage fallback for headless/Docker environments where secure storage is missing
_EPHEMERAL_CREDENTIALS: dict[str, str] = {}


def _is_windows():
    return platform.system().lower() == "windows"


class DataBlob(ctypes.Structure):  # pragma: no cover
    """DPAPI用のデータ構造体"""

    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob_from_bytes(data: bytes):  # pragma: no cover
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    return blob, buffer


def _dpapi_protect(data: bytes) -> bytes:  # pragma: no cover
    if not _is_windows():
        raise RuntimeError("DPAPI is only available on Windows")

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
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
            err = ctypes.get_last_error()  # type: ignore[attr-defined,unused-ignore]
            raise ctypes.WinError(err)  # type: ignore[attr-defined,unused-ignore]

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

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
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
            err = ctypes.get_last_error()  # type: ignore[attr-defined,unused-ignore]
            raise ctypes.WinError(err)  # type: ignore[attr-defined,unused-ignore]

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
    """API秘密情報を安全にエンコードする。

    Args:
        value: エンコードする秘密情報
        key_name: キーの識別子
    """
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
        except (OSError, RuntimeError) as exc:
            logger.error(
                "DPAPI protection failed; unable to securely store secret: %s",
                exc,
                exc_info=True,
            )
            if not keyring_error:
                raise RuntimeError("Secure secret storage unavailable") from exc

    # プレーンテキストへのフォールバックはセキュリティリスクのため完全に削除しましたが、
    # ヘッドレス/Docker環境での起動不能を防ぐため、MNS_EPHEMERAL_FALLBACK=1が指定されている場合は一時的インメモリ保存（ephemeral）にフォールバックします。
    if os.environ.get("MNS_EPHEMERAL_FALLBACK") == "1":
        logger.warning(
            "セキュアストレージ (keyring/DPAPI) が利用できません。対象: '%s'。 "
            "MNS_EPHEMERAL_FALLBACK=1 が指定されているため、本セッション中のみ有効な一時的インメモリ保存（ephemeral）にフォールバックします。 "
            "アプリケーションを再起動すると、保存された認証情報は失われます。",
            key_name,
        )
        _EPHEMERAL_CREDENTIALS[key_name] = text
        return {"scheme": "ephemeral", "value": ""}

    error_msg = (
        f"セキュアストレージ (keyring/DPAPI) が利用できません。対象: {key_name}。"
    )
    if keyring_error:
        error_msg += f" KeyringError: {keyring_error}."

    logger.error(
        "No secure storage (keyring/DPAPI) available for '%s'. "
        "Plaintext fallback is no longer supported for security reasons. "
        "On Windows, ensure Credential Manager is functional. "
        "On Linux, ensure dbus/gnome-keyring is installed. "
        "To allow in-memory ephemeral fallback in headless/Docker environments, set MNS_EPHEMERAL_FALLBACK=1.",
        key_name,
    )
    raise RuntimeError(
        error_msg
        + " Windowsの場合はコントロールパネルの「資格情報マネージャー」が動作しているか確認してください。 "
        "平文での保存機能はセキュリティ強化のため削除されました。 "
        "一時的インメモリ保存を有効にするには環境変数 MNS_EPHEMERAL_FALLBACK=1 を指定してください。"
    )


def _decode_secret(entry, key_name: str = "default") -> str:
    """エンコードされたAPI秘密情報をデコードする。

    Args:
        entry: デコードするエントリ
        key_name: キーの識別子
    """
    if not entry:
        return ""

    # 平文エントリはセキュリティ上一切受け付けない（オプトイン廃止）。
    if isinstance(entry, str):
        logger.warning(
            "Ignoring legacy plaintext secret entry for '%s'; re-save the credential to migrate it to secure storage.",
            key_name,
        )
        return ""
    if not isinstance(entry, dict):
        return ""

    scheme = str(entry.get("scheme") or "").strip().lower()

    if scheme == "ephemeral":
        return _EPHEMERAL_CREDENTIALS.get(key_name, "")

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
        # 平文エントリは一切利用しない（オプトイン廃止）。
        logger.warning(
            "Plaintext secret entry for '%s' is no longer supported for security reasons.",
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


def get_or_create_master_key(config_store_module=None) -> str:
    """Get or create the master key for Fernet symmetric encryption.

    DEPRECATED: Use config_store.get_or_create_master_key() instead.
    This wrapper is kept for backward compatibility and will be removed.

    Args:
        config_store_module: The config_store module, or a callable that returns it.
                             If None, imports config_store at call time.
    """
    if config_store_module is not None:
        if callable(config_store_module):
            cs = config_store_module()
        else:
            cs = config_store_module  # module or similar object
    else:
        import config_store as _cs  # type: ignore[import-not-found]
        cs = _cs
    return cs.get_or_create_master_key()


def enforce_secure_permissions(file_path):
    """Enforce owner-only read/write permissions (0o600) on non-Windows platforms."""
    if _is_windows():
        return
    from pathlib import Path
    p = Path(file_path)
    if p.exists():
        try:
            p.chmod(0o600)
        except Exception as exc:
            logger.warning("Failed to enforce 0o600 on %s: %s", file_path, exc)


def protect_data(text: str, key_name: str = "general_data", master_key: Optional[str] = None) -> dict:
    """データを Fernet 対称暗号化で安全に保護（暗号化）する

    Args:
        text: 保護するプレーンテキスト
        key_name: キーの識別子（ログ用）
        master_key: Fernet マスターキー。None の場合は自動取得を試みる。
    """
    val = (text or "").strip()
    if not val:
        return {"scheme": "fernet", "value": ""}

    if master_key is None:
        # Fallback: import config_store lazily to avoid circular imports
        import config_store as _cs  # type: ignore[import-not-found]
        master_key = _cs.get_or_create_master_key()

    from cryptography.fernet import Fernet, InvalidToken
    try:
        f = Fernet(master_key.encode("ascii"))
        encrypted = f.encrypt(val.encode("utf-8"))
        return {
            "scheme": "fernet",
            "value": encrypted.decode("ascii")
        }
    except (InvalidToken, ValueError, TypeError) as exc:
        logger.error(
            "Failed to protect data using Fernet for %s: %s. Falling back to platform crypto.",
            key_name,
            exc,
        )
        fallback = _encode_secret(text, key_name)
        if fallback and isinstance(fallback, dict) and fallback.get("value", "") != "":
            return fallback
        raise RuntimeError(f"Failed to protect data for {key_name}: {exc}") from exc


def unprotect_data(entry: Any, key_name: str = "general_data", master_key: Optional[str] = None) -> str:
    """保護されたデータを復号する

    Args:
        entry: 暗号化されたエントリ（dict）
        key_name: キーの識別子（ログ用）
        master_key: Fernet マスターキー。None の場合は自動取得を試みる。
    """
    if not entry or not isinstance(entry, dict):
        if isinstance(entry, str):
            return _decode_secret(entry, key_name)
        return ""

    scheme = str(entry.get("scheme") or "").strip().lower()

    if scheme == "fernet":
        if master_key is None:
            import config_store as _cs  # type: ignore[import-not-found]
            master_key = _cs.get_or_create_master_key()
        from cryptography.fernet import Fernet, InvalidToken
        try:
            f = Fernet(master_key.encode("ascii"))
            decrypted = f.decrypt(entry.get("value", "").encode("ascii"))
            return decrypted.decode("utf-8")
        except (InvalidToken, ValueError, TypeError) as exc:
            logger.error("Failed to decrypt Fernet data for %s: %s", key_name, exc)
            return ""

    return _decode_secret(entry, key_name)
