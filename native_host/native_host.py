#!/usr/bin/env python3
"""Native host wrapper for Chrome native messaging and backend startup."""

import io
import json
import logging
import math
import ntpath
import os
import re
import struct
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, BinaryIO, cast

# --- I/O Protection & Binary Mode Setup ---
# Protocol streams (must be captured before stdout is redirected)
RAW_STDIN = cast(BinaryIO, getattr(sys.stdin, "buffer", sys.stdin))
RAW_STDOUT = cast(BinaryIO, getattr(sys.stdout, "buffer", sys.stdout))

if os.name == "nt":  # pragma: no cover
    import msvcrt  # pylint: disable=import-error
    msvcrt_mod = cast(Any, msvcrt)

    # Ensure binary mode for raw streams on Windows. Pytest may provide pseudo
    # streams without fileno(), so skip this during import-time tests.
    try:
        msvcrt_mod.setmode(RAW_STDIN.fileno(), 0x8000)  # _O_BINARY
        msvcrt_mod.setmode(RAW_STDOUT.fileno(), 0x8000)  # _O_BINARY
    except (OSError, ValueError, AttributeError, io.UnsupportedOperation):
        pass


# Redirect stdout to stderr so that stray print calls don't break the protocol
class StdoutRedirectionGuard:
    """stdoutをstderrへリダイレクトするガード"""

    @property
    def encoding(self):
        """stderrのエンコーディングを返す"""
        return getattr(sys.stderr, "encoding", "utf-8")

    @property
    def errors(self):
        """stderrのエラーハンドリングを返す"""
        return getattr(sys.stderr, "errors", "strict")

    def isatty(self):
        """擬似端末ではない"""
        return False

    def fileno(self):
        """stderrのファイル記述子を返す"""
        return sys.stderr.fileno()

    def write(self, data):
        """データをstderrに書き込む"""
        sys.stderr.write(data)

    def flush(self):
        """stderrをフラッシュする"""
        sys.stderr.flush()


sys.stdout = StdoutRedirectionGuard()


# --- Security Utilities ---
def _sanitize_log_message(msg):
    """ログメッセージから機密情報を削除"""
    if not msg:
        return ""
    # 値部分は [^\s]+ で引用符・区切り文字（' " 等）も含めて完全にマスクする。
    # これにより token=abc"def のような引用符を含む値も全体が隠される（NH-1 / R8）。
    # authorization はスキーム（Bearer / Basic / Digest 等）を消費してから
    # トークン本体全体をマスクするため、Bearer トークンの漏洩を防ぐ。
    sensitive_patterns = [
        r"api[_-]?key['\"]?\s*[:=]\s*['\"]?[^\s]+",
        r"token['\"]?\s*[:=]\s*['\"]?[^\s]+",
        r"password['\"]?\s*[:=]\s*['\"]?[^\s]+",
        r"authorization['\"]?\s*[:=]\s*(?:\w+\s+)?[^\s]+",
    ]
    sanitized = str(msg)
    for pattern in sensitive_patterns:
        sanitized = re.sub(pattern, "[REDACTED]", sanitized, flags=re.IGNORECASE)
    return sanitized


class SanitizedFormatter(logging.Formatter):
    def format(self, record):
        formatted = super().format(record)
        return _sanitize_log_message(formatted)


# --- Logging Configuration ---
# Since stdout is now redirected to stderr, we must be careful with logging levels
_log_format = "[%(asctime)s] %(levelname)s: %(message)s"
_log_dir = Path(
    os.environ.get("MNS_DATA_DIR") or os.environ.get("MNS_APP_DATA_DIR") or Path(__file__).parent
)
_log_dir.mkdir(parents=True, exist_ok=True)
_file_handler = RotatingFileHandler(
    _log_dir / "native_host.log",
    maxBytes=1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
_file_handler.setFormatter(SanitizedFormatter(_log_format))

_stream_handler = logging.StreamHandler(sys.stderr)
_stream_handler.setFormatter(SanitizedFormatter(_log_format))

logging.basicConfig(
    level=logging.DEBUG,
    handlers=[_file_handler, _stream_handler],
)
logger = logging.getLogger(__name__)

# Suppress debug/info logs from stderr to avoid cluttering Chrome's stderr capture
for _handler in logging.getLogger().handlers:
    if (
        isinstance(_handler, logging.StreamHandler)
        and getattr(_handler, "stream", None) is sys.stderr
    ):
        _handler.setLevel(logging.WARNING)

# --- Imports and Constants ---
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "native_host"))
try:
    try:
        from native_host.start_backend import get_backend_port, is_backend_healthy_once, start
    except ImportError:
        from start_backend import get_backend_port, is_backend_healthy_once, start  # type: ignore
except ImportError:
    logger.exception("Failed to import start_backend")
    start = None  # type: ignore
    get_backend_port = None  # type: ignore
    is_backend_healthy_once = None  # type: ignore

try:
    from crypto_utils import unprotect_data
except ImportError:
    try:
        from config_utils import unprotect_data
    except ImportError as imp_exc:
        logger.critical(
            "Critical import failure for crypto_utils/config_utils: %s. Native Host cannot function without key utilities.",
            imp_exc,
            exc_info=True,
        )
        sys.exit(1)


def _safe_int_env(key: str, default: int, min_value: int | None = None) -> int:
    val = os.environ.get(key, "").strip()
    if not val:
        return default
    try:
        parsed = int(val)
    except (TypeError, ValueError):
        logger.warning("Invalid integer env %s=%r; using default %d", key, val, default)
        return default
    if min_value is not None and parsed < min_value:
        logger.warning(
            "Env %s=%r below minimum %d; clamping to %d", key, val, min_value, min_value
        )
        return min_value
    return parsed


def _safe_float_env(key: str, default: float, min_value: float | None = None) -> float:
    val = os.environ.get(key, "").strip()
    if not val:
        return default
    try:
        parsed = float(val)
    except (TypeError, ValueError):
        logger.warning("Invalid float env %s=%r; using default %f", key, val, default)
        return default
    if not math.isfinite(parsed):
        logger.warning("Invalid finite float env %s=%r; using default %f", key, val, default)
        return default
    if min_value is not None and parsed < min_value:
        logger.warning(
            "Env %s=%r below minimum %s; clamping to %s", key, val, min_value, min_value
        )
        return min_value
    return parsed


# A tiny/mis-set limit would reject legitimate frames (or, for the drain
# limit, defeat the bounded-drain defense), so both are floored.
MAX_MESSAGE_BYTES = _safe_int_env("NATIVE_HOST_MAX_MESSAGE_BYTES", 1024 * 1024, min_value=4096)
MAX_DRAIN_BYTES = _safe_int_env(
    "NATIVE_HOST_MAX_DRAIN_BYTES", MAX_MESSAGE_BYTES * 2, min_value=4096
)

# A fully consumed frame with invalid contents is safe to skip. A truncated or
# undrainable frame loses stream alignment and must terminate the connection.
SKIP_FRAME = object()
FATAL_FRAME = object()


# --- Rate Limiting for IPC ---
_NATIVE_RATE_LIMIT_MAX = _safe_int_env("NATIVE_HOST_RATE_LIMIT_MAX", 10, min_value=1)
_NATIVE_RATE_LIMIT_WINDOW = _safe_float_env(
    "NATIVE_HOST_RATE_LIMIT_WINDOW", 1.0, min_value=0.001
)
_rate_limit_timestamps: list = []
_rate_limit_lock = threading.Lock()


def _check_rate_limit():
    """IPCメッセージのレート制限をチェック（スライディングウィンドウ）"""
    now = time.time()
    with _rate_limit_lock:
        cutoff = now - _NATIVE_RATE_LIMIT_WINDOW
        _rate_limit_timestamps[:] = [t for t in _rate_limit_timestamps if t > cutoff]
        if len(_rate_limit_timestamps) >= _NATIVE_RATE_LIMIT_MAX:
            return False
        _rate_limit_timestamps.append(now)
        return True


# Stricter budget for actions that return secret material (shutdown token,
# extension API token). The extension-ID check only proves the caller knows the
# public ID (any local process can pass it), so the general IPC limit alone
# would let a local attacker harvest tokens at 10 msg/sec. This bounds token
# exposure to a few reads per window (R30).
_NATIVE_TOKEN_ACTION_MAX = _safe_int_env("NATIVE_HOST_TOKEN_ACTION_MAX", 3, min_value=1)
_NATIVE_TOKEN_ACTION_WINDOW = _safe_float_env(
    "NATIVE_HOST_TOKEN_ACTION_WINDOW", 30.0, min_value=0.001
)
_token_action_timestamps: list = []


def _check_token_action_rate_limit():
    """Rate limit for actions that disclose secrets (sliding window)."""
    now = time.time()
    with _rate_limit_lock:
        cutoff = now - _NATIVE_TOKEN_ACTION_WINDOW
        _token_action_timestamps[:] = [t for t in _token_action_timestamps if t > cutoff]
        if len(_token_action_timestamps) >= _NATIVE_TOKEN_ACTION_MAX:
            return False
        _token_action_timestamps.append(now)
        return True


def _token_action_allowed():
    """Gate helper: enforce the secret-action budget and log denials once."""
    ok = _check_token_action_rate_limit()
    if not ok:
        logger.warning("Token action rate limit exceeded")
    return ok


# --- Security Constants ---
# 許可されたアクションのホワイトリスト
ALLOWED_ACTIONS = frozenset(
    {"start_backend", "get_shutdown_token", "get_backend_port", "get_extension_api_token", "ping"}
)
_MAX_ACTION_LENGTH = 64

# extensionId のフォーマット検証（Chrome 拡張IDは32文字の小文字英数字）
_EXTENSION_ID_PATTERN = re.compile(r"^[a-z0-9]{32}$")


# Cache the parsed allowed-origins set, reloading only when the manifest file
# changes on disk. Avoids re-reading + JSON-parsing the manifest on every IPC
# message (which previously happened up to the IPC rate limit per second).
_allowed_origins_cache: dict = {"origins": None, "mtime": None}
_allowed_origins_lock = threading.Lock()


def _load_allowed_manifest_origins():
    """ホストマニフェストから許可された拡張機能IDのセットを取得（mtime キャッシュ付き）"""
    manifest_path = ROOT / "native_host" / "com.mistral_nex_stocks.host.json"
    try:
        mtime = manifest_path.stat().st_mtime if manifest_path.exists() else None
    except OSError:
        mtime = None
    with _allowed_origins_lock:
        if (
            _allowed_origins_cache["origins"] is not None
            and _allowed_origins_cache["mtime"] == mtime
        ):
            return _allowed_origins_cache["origins"]
        origins = _parse_allowed_manifest_origins(manifest_path)
        _allowed_origins_cache["origins"] = origins
        _allowed_origins_cache["mtime"] = mtime
        return origins


def _parse_allowed_manifest_origins(manifest_path):
    origins = set()
    try:
        if manifest_path.exists():
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest_data = json.load(f) or {}
            for raw in manifest_data.get("allowed_origins", []) or []:
                raw_str = str(raw or "").strip().lower()
                if raw_str.startswith("chrome-extension://"):
                    origin_id = raw_str[len("chrome-extension://") :].rstrip("/")
                    if _EXTENSION_ID_PATTERN.match(origin_id):
                        origins.add(origin_id)
                elif _EXTENSION_ID_PATTERN.match(raw_str):
                    origins.add(raw_str)
    except Exception as exc:
        logger.error("Failed to load allowed origins from manifest: %s", exc)
    return origins


def _validate_extension_id(extension_id):
    """Chrome 拡張機能のIDフォーマットおよび許可リストを検証"""
    if extension_id is None:
        return None
    extension_id = str(extension_id).strip()
    if not _EXTENSION_ID_PATTERN.match(extension_id):
        logger.warning(
            "Invalid extension ID format: %s",
            extension_id[:20] if extension_id else "None",
        )
        return None

    # マニフェストに記載された許可済みオリジンと照合
    allowed_ids = _load_allowed_manifest_origins()
    if not allowed_ids:
        logger.error(
            "No allowed extension IDs found in manifest; rejecting connection as a security precaution"
        )
        return None
    if extension_id not in allowed_ids:
        logger.warning("Unauthorised extension ID rejected: %s", extension_id)
        return None
    return extension_id


def _is_invalid_windows_handle(handle: Any, ctypes_module: Any) -> bool:
    """Return whether a Win32 handle is null or INVALID_HANDLE_VALUE."""
    value = getattr(handle, "value", handle)
    invalid_value = ctypes_module.c_void_p(-1).value
    return value in (None, 0, -1, invalid_value)


def _get_proc_creation_time(pid: int) -> int | None:
    """Return process creation time as an integer timestamp on Windows, or None on failure."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        k32: Any = getattr(ctypes, "windll", ctypes.cdll).kernel32

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

        class FILETIME(ctypes.Structure):
            _fields_ = [
                ("dwLowDateTime", wintypes.DWORD),
                ("dwHighDateTime", wintypes.DWORD),
            ]

        # ctypes defaults to a 32-bit integer return value.  Explicit
        # signatures are required here because process handles and pointers
        # are 64-bit on normal Windows installations.
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
        ]
        k32.GetProcessTimes.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL

        h_proc = k32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if _is_invalid_windows_handle(h_proc, ctypes):
            get_last_error = getattr(ctypes, "GetLastError", getattr(ctypes, "get_last_error", lambda: -1))
            err = get_last_error()
            logger.debug("OpenProcess failed for pid %d (err=%d)", pid, err)
            return None
        try:
            c_time = FILETIME()
            e_time = FILETIME()
            k_time = FILETIME()
            u_time = FILETIME()
            if k32.GetProcessTimes(
                h_proc,
                ctypes.byref(c_time),
                ctypes.byref(e_time),
                ctypes.byref(k_time),
                ctypes.byref(u_time),
            ):
                return (c_time.dwHighDateTime << 32) | c_time.dwLowDateTime
            return None
        finally:
            if not _is_invalid_windows_handle(h_proc, ctypes):
                k32.CloseHandle(h_proc)
    except Exception as exc:
        logger.debug("GetProcessTimes failed on Windows for pid %d: %s", pid, exc)
        return None


def _get_posix_ancestor_process_names(
    max_depth: int = 5,
    proc_dir: Path | str = "/proc",
    start_pid: int | None = None,
) -> list[str]:
    """Return lower-case executable basenames of ancestor processes on POSIX systems."""
    ancestors: list[str] = []
    try:
        proc_path = Path(proc_dir)
        curr_pid = os.getpid() if start_pid is None else start_pid
        for _ in range(max_depth):
            status_file = proc_path / str(curr_pid) / "status"
            if not status_file.exists():
                break
            ppid = None
            for line in status_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("PPid:"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        ppid = int(parts[1])
                    break
            if ppid is None or ppid <= 1 or ppid == curr_pid:
                break
            cmdline_file = proc_path / str(ppid) / "cmdline"
            if cmdline_file.exists():
                try:
                    raw_bytes = cmdline_file.read_bytes()
                    raw = raw_bytes.split(b"\x00")[0] if raw_bytes else b""
                    name = Path(raw.decode("utf-8", errors="ignore")).name.lower()
                    if name:
                        ancestors.append(name)
                except Exception as cmd_exc:
                    logger.debug("Failed reading cmdline for ppid %d: %s", ppid, cmd_exc)
            curr_pid = ppid
    except Exception as exc:
        logger.debug("Process tree lookup failed on POSIX: %s", exc)

    return ancestors


_WINDOWS_AUTHORIZED_BROWSER_PROCESSES = frozenset({"chrome.exe", "msedge.exe", "brave.exe"})

_WINDOWS_BROWSER_PATH_SUFFIXES = {
    "chrome.exe": ("google", "chrome", "application", "chrome.exe"),
    "msedge.exe": ("microsoft", "edge", "application", "msedge.exe"),
    "brave.exe": ("bravesoftware", "brave-browser", "application", "brave.exe"),
}

_WINDOWS_BROWSER_PUBLISHERS = {
    "chrome.exe": "GOOGLE LLC",
    "msedge.exe": "MICROSOFT CORPORATION",
    "brave.exe": "BRAVE SOFTWARE",
}

_WINDOWS_PROGRAM_FILES_FOLDER_IDS = (
    (0x905E63B6, 0xC1BF, 0x494E, (0xB2, 0x9C, 0x65, 0xB7, 0x32, 0xD3, 0xD2, 0x1A)),  # FOLDERID_ProgramFiles
    (0x7C5A40EF, 0xA0FB, 0x4BFC, (0x87, 0x4A, 0xC0, 0xF2, 0xE0, 0xB9, 0xFA, 0x8E)),  # FOLDERID_ProgramFilesX86
    (0x6D809377, 0x6AF0, 0x444B, (0x89, 0x57, 0xA3, 0x77, 0x3F, 0x02, 0x20, 0x0E)),  # FOLDERID_ProgramFilesX64
    (0xF1B32785, 0x6FBA, 0x4FCF, (0x9D, 0x55, 0x7B, 0x8E, 0x7F, 0x15, 0x70, 0x91)),  # FOLDERID_LocalAppData
)

_AUTHENTICODE_POWERSHELL_COMMAND = (
    "$signature = Get-AuthenticodeSignature -LiteralPath $args[0]; "
    "if ($null -eq $signature -or $null -eq $signature.SignerCertificate) { exit 1 }; "
    "[Console]::Out.Write((@{Status=$signature.Status.ToString(); "
    "Subject=$signature.SignerCertificate.Subject} | ConvertTo-Json -Compress))"
)


def _get_windows_process_image_path(pid: int) -> str | None:
    """Return a Windows process's full image path, or None when it cannot be read."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        k32: Any = getattr(getattr(ctypes, "windll", None), "kernel32", None)
        if k32 is None:
            return None

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL

        h_proc = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if _is_invalid_windows_handle(h_proc, ctypes):
            return None
        try:
            path_buffer = ctypes.create_unicode_buffer(32768)
            path_length = wintypes.DWORD(len(path_buffer))
            if not k32.QueryFullProcessImageNameW(
                h_proc, 0, path_buffer, ctypes.byref(path_length)
            ):
                return None
            return path_buffer.value
        finally:
            k32.CloseHandle(h_proc)
    except Exception:
        logger.debug("Failed to obtain a Windows ancestor process image path")
        return None


def _get_windows_powershell_path() -> str | None:
    """Return the system PowerShell path without trusting the inherited PATH."""
    if os.name != "nt":
        return None
    try:
        import ctypes

        k32: Any = getattr(getattr(ctypes, "windll", None), "kernel32", None)
        if k32 is None:
            return None
        system_dir = ctypes.create_unicode_buffer(32768)
        if not k32.GetSystemDirectoryW(system_dir, len(system_dir)):
            return None
        return str(
            Path(system_dir.value)
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
    except Exception:
        logger.debug("Failed to resolve the system PowerShell executable")
        return None


def _get_windows_win32_signature(image_path: str) -> tuple[str, str] | None:
    """Return Authenticode status and subject using in-process Win32 APIs."""
    if os.name != "nt" or not image_path:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        wintrust: Any = getattr(getattr(ctypes, "windll", None), "wintrust", None)
        crypt32: Any = getattr(getattr(ctypes, "windll", None), "crypt32", None)

        if wintrust is None or crypt32 is None:
            return None

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", wintypes.BYTE * 8),
            ]

        WINTRUST_ACTION_GENERIC_VERIFY_V2 = GUID(
            0x00AAC56B,
            0xCD44,
            0x11D0,
            (wintypes.BYTE * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE),
        )

        class WINTRUST_FILE_INFO(ctypes.Structure):
            _fields_ = [
                ("cbStruct", wintypes.DWORD),
                ("pcwszFilePath", wintypes.LPCWSTR),
                ("hFile", wintypes.HANDLE),
                ("pgKnownSubject", ctypes.c_void_p),
            ]

        class WINTRUST_DATA(ctypes.Structure):
            _fields_ = [
                ("cbStruct", wintypes.DWORD),
                ("pPolicyCallbackData", ctypes.c_void_p),
                ("pSIPClientData", ctypes.c_void_p),
                ("dwUIChoice", wintypes.DWORD),
                ("fdwRevocationChecks", wintypes.DWORD),
                ("dwUnionChoice", wintypes.DWORD),
                ("pFile", ctypes.POINTER(WINTRUST_FILE_INFO)),
                ("dwStateAction", wintypes.DWORD),
                ("hWVTStateData", wintypes.HANDLE),
                ("pwszURLReference", wintypes.LPCWSTR),
                ("dwProvFlags", wintypes.DWORD),
                ("dwUIContext", wintypes.DWORD),
                ("pSignatureSettings", ctypes.c_void_p),
            ]

        file_info = WINTRUST_FILE_INFO()
        file_info.cbStruct = ctypes.sizeof(WINTRUST_FILE_INFO)
        file_info.pcwszFilePath = image_path
        file_info.hFile = None
        file_info.pgKnownSubject = None

        wtd = WINTRUST_DATA()
        wtd.cbStruct = ctypes.sizeof(WINTRUST_DATA)
        wtd.pPolicyCallbackData = None
        wtd.pSIPClientData = None
        wtd.dwUIChoice = 2  # WTD_UI_NONE
        wtd.fdwRevocationChecks = 0  # WTD_REVOKE_NONE
        wtd.dwUnionChoice = 1  # WTD_CHOICE_FILE
        wtd.pFile = ctypes.pointer(file_info)
        wtd.dwStateAction = 0  # WTD_STATEACTION_IGNORE
        wtd.hWVTStateData = None
        wtd.pwszURLReference = None
        wtd.dwProvFlags = 0x00000100  # WTD_SAFER_FLAG
        wtd.dwUIContext = 0

        status = wintrust.WinVerifyTrust(
            0,
            ctypes.byref(WINTRUST_ACTION_GENERIC_VERIFY_V2),
            ctypes.byref(wtd),
        )
        if status != 0:
            return None

        CERT_QUERY_OBJECT_FILE = 1
        CERT_QUERY_CONTENT_FLAG_PKCS7_SIGNED_EMBED = 1 << 10
        CERT_QUERY_FORMAT_FLAG_ALL = 0x0000000E

        encoding = wintypes.DWORD()
        content_type = wintypes.DWORD()
        format_type = wintypes.DWORD()
        h_store = wintypes.HANDLE()
        h_msg = wintypes.HANDLE()

        crypt32.CryptQueryObject.argtypes = [
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.HANDLE),
            ctypes.POINTER(wintypes.HANDLE),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        crypt32.CryptQueryObject.restype = wintypes.BOOL

        crypt32.CertEnumCertificatesInStore.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        crypt32.CertEnumCertificatesInStore.restype = ctypes.c_void_p

        crypt32.CertGetNameStringW.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.LPWSTR,
            wintypes.DWORD,
        ]
        crypt32.CertGetNameStringW.restype = wintypes.DWORD

        crypt32.CertFreeCertificateContext.argtypes = [ctypes.c_void_p]
        crypt32.CertFreeCertificateContext.restype = wintypes.BOOL

        crypt32.CertCloseStore.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        crypt32.CertCloseStore.restype = wintypes.BOOL

        crypt32.CryptMsgClose.argtypes = [wintypes.HANDLE]
        crypt32.CryptMsgClose.restype = wintypes.BOOL

        path_ptr = ctypes.c_wchar_p(image_path)
        res = crypt32.CryptQueryObject(
            CERT_QUERY_OBJECT_FILE,
            ctypes.cast(path_ptr, ctypes.c_void_p),
            CERT_QUERY_CONTENT_FLAG_PKCS7_SIGNED_EMBED,
            CERT_QUERY_FORMAT_FLAG_ALL,
            0,
            ctypes.byref(encoding),
            ctypes.byref(content_type),
            ctypes.byref(format_type),
            ctypes.byref(h_store),
            ctypes.byref(h_msg),
            None,
        )
        if not res or not h_store:
            return None

        subjects: list[str] = []
        try:
            CERT_NAME_SIMPLE_DISPLAY_TYPE = 4
            p_cert = None
            while True:
                p_cert = crypt32.CertEnumCertificatesInStore(h_store, p_cert)
                if not p_cert:
                    break
                buf_size = crypt32.CertGetNameStringW(
                    p_cert,
                    CERT_NAME_SIMPLE_DISPLAY_TYPE,
                    0,
                    None,
                    None,
                    0,
                )
                if buf_size > 1:
                    buf = ctypes.create_unicode_buffer(buf_size)
                    crypt32.CertGetNameStringW(
                        p_cert,
                        CERT_NAME_SIMPLE_DISPLAY_TYPE,
                        0,
                        None,
                        buf,
                        buf_size,
                    )
                    subjects.append(buf.value)
        finally:
            crypt32.CertCloseStore(h_store, 0)
            if h_msg:
                crypt32.CryptMsgClose(h_msg)

        if subjects:
            return "Valid", "; ".join(subjects)
        return None
    except Exception:
        logger.debug("Win32 Authenticode verification failed", exc_info=True)
        return None


def _get_windows_powershell_signature(image_path: str) -> tuple[str, str] | None:
    """Return Authenticode status and subject using PowerShell fallback."""
    powershell_path = _get_windows_powershell_path()
    if not powershell_path:
        return None
    try:
        result = subprocess.run(
            [
                powershell_path,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _AUTHENTICODE_POWERSHELL_COMMAND,
                image_path,
            ],
            capture_output=True,
            check=False,
            shell=False,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        signature = json.loads(result.stdout)
        status = signature.get("Status")
        subject = signature.get("Subject")
        if not isinstance(status, str) or not isinstance(subject, str):
            return None
        return status, subject
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, TypeError, ValueError):
        logger.debug("Authenticode verification was unavailable for a Windows ancestor")
        return None


def _get_windows_authenticode_signature(image_path: str) -> tuple[str, str] | None:
    """Return Authenticode status and subject for an image, failing closed on errors.

    Verifies the file's digital signature and publisher using native Win32 APIs
    (wintrust.dll and crypt32.dll) to avoid external process execution dependencies,
    falling back to PowerShell if needed.
    """
    if os.name != "nt" or not image_path:
        return None
    win32_sig = _get_windows_win32_signature(image_path)
    if win32_sig is not None:
        return win32_sig
    return _get_windows_powershell_signature(image_path)


def _get_windows_browser_install_roots() -> tuple[str, ...]:
    """Return OS-owned Program Files directories without trusting process environment variables."""
    if os.name != "nt":
        return ()
    try:
        import ctypes
        from ctypes import wintypes

        shell32: Any = getattr(getattr(ctypes, "windll", None), "shell32", None)
        ole32: Any = getattr(getattr(ctypes, "windll", None), "ole32", None)
        if shell32 is None or ole32 is None:
            return ()

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", wintypes.BYTE * 8),
            ]

        roots: list[str] = []
        for data1, data2, data3, data4 in _WINDOWS_PROGRAM_FILES_FOLDER_IDS:
            folder_id = GUID(data1, data2, data3, (wintypes.BYTE * 8)(*data4))
            folder_path = ctypes.c_wchar_p()
            if shell32.SHGetKnownFolderPath(
                ctypes.byref(folder_id), 0, None, ctypes.byref(folder_path)
            ) != 0 or not folder_path:
                continue
            try:
                folder_path_value = folder_path.value
                if folder_path_value:
                    roots.append(folder_path_value)
            finally:
                ole32.CoTaskMemFree(folder_path)
        return tuple(dict.fromkeys(roots))
    except Exception:
        logger.debug("Failed to resolve Windows Program Files directories")
        return ()


def _is_windows_authorized_browser_process(image_path: str | None, image_name: str) -> bool:
    """Verify a Chrome or Edge ancestor's canonical location and publisher signature."""
    normalized_name = image_name.lower()
    expected_suffix = _WINDOWS_BROWSER_PATH_SUFFIXES.get(normalized_name)
    expected_publisher = _WINDOWS_BROWSER_PUBLISHERS.get(normalized_name)
    if not image_path or not expected_suffix or not expected_publisher:
        return False

    normalized_path = ntpath.normcase(ntpath.normpath(image_path))
    if not any(
        normalized_path == ntpath.normcase(ntpath.join(root, *expected_suffix))
        for root in _get_windows_browser_install_roots()
    ):
        return False

    signature = _get_windows_authenticode_signature(image_path)
    if signature is None:
        return False
    status, subject = signature
    normalized_subject = subject.upper()
    return status.casefold() == "valid" and expected_publisher in normalized_subject


def _get_windows_ancestor_processes(max_depth: int = 5) -> list[tuple[int, str, str | None]]:
    """Return Windows ancestor PID, base image name, and full image path metadata."""
    ancestors: list[tuple[int, str, str | None]] = []
    try:
        import ctypes
        from ctypes import wintypes

        k32 = getattr(getattr(ctypes, "windll", None), "kernel32", None)
        if k32 is None:
            return []

        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.Process32FirstW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESSENTRY32W),
        ]
        k32.Process32FirstW.restype = wintypes.BOOL
        k32.Process32NextW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESSENTRY32W),
        ]
        k32.Process32NextW.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL

        h_snapshot = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if _is_invalid_windows_handle(h_snapshot, ctypes):
            return []
        try:
            pid_map: dict[int, tuple[int, str]] = {}
            pe = PROCESSENTRY32W()
            pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if k32.Process32FirstW(h_snapshot, ctypes.byref(pe)):
                while True:
                    pid_map[pe.th32ProcessID] = (pe.th32ParentProcessID, pe.szExeFile.lower())
                    if not k32.Process32NextW(h_snapshot, ctypes.byref(pe)):
                        break

            curr_pid = os.getpid()
            curr_ctime = _get_proc_creation_time(curr_pid)
            for _ in range(max_depth):
                if curr_pid not in pid_map:
                    break
                ppid = pid_map[curr_pid][0]
                if ppid == curr_pid or ppid == 0:
                    break

                # Verify parent creation time to guard against PID reuse (fail-closed).
                ppid_ctime = _get_proc_creation_time(ppid)
                if curr_ctime is None or ppid_ctime is None or ppid_ctime > curr_ctime:
                    break

                parent_name = pid_map.get(ppid, (0, ""))[1]
                if parent_name:
                    ancestors.append((ppid, parent_name, _get_windows_process_image_path(ppid)))
                curr_pid = ppid
                curr_ctime = ppid_ctime
        finally:
            k32.CloseHandle(h_snapshot)
    except Exception:
        logger.debug("Process tree lookup failed on Windows")
    return ancestors


def _get_ancestor_process_names(max_depth: int = 5) -> list[str]:
    """Return lower-case executable basenames of ancestor processes.

    Caller validation is fail-closed: process-tree lookup failures return an
    empty list, which :func:`_is_caller_authorized_browser` rejects.  Do not
    add a development environment-variable bypass here; a local process can
    forge both such a variable and the public native-messaging origin argument.
    """
    if os.name == "nt":
        ancestors = _get_windows_ancestor_processes(max_depth=max_depth)
        return [
            image_name
            for _pid, image_name, image_path in ancestors
            if image_name not in _AUTHORIZED_BROWSER_PROCESSES
            or (
                image_name in _WINDOWS_AUTHORIZED_BROWSER_PROCESSES
                and _is_windows_authorized_browser_process(image_path, image_name)
            )
        ]
    return _get_posix_ancestor_process_names(max_depth=max_depth)


_AUTHORIZED_BROWSER_PROCESSES = frozenset(
    {
        "chrome.exe",
        "msedge.exe",
        "brave.exe",
        "chrome",
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "msedge",
        "brave",
    }
)


def _is_caller_authorized_browser(ancestors: list[str] | None = None) -> bool:
    """Return whether this host can trace its caller to Chrome or Edge.

    Native-messaging ``allowed_origins`` and the first command-line argument
    bind a host to an extension when the browser launches it, but both values
    are public and can be forged by an arbitrary local process.  The process
    ancestry therefore must contain an actual supported browser.  Wrapper
    processes such as ``cmd.exe`` and ``python.exe`` are expected in the
    legitimate Windows launcher chain, but are not authorization evidence by
    themselves.
    """
    if ancestors is None:
        ancestors = _get_ancestor_process_names()
    if not ancestors:
        logger.warning(
            "Native message caller rejected: process ancestry unavailable; failing closed. "
            "pid=%d ppid=%d",
            os.getpid(),
            getattr(os, "getppid", lambda: 0)(),
        )
        return False
    if any(name in _AUTHORIZED_BROWSER_PROCESSES for name in ancestors):
        return True
    logger.warning(
        "Native message caller rejected: process ancestry contains no authorized browser; ancestors=%s",
        ancestors[:5],
    )
    return False


def _require_valid_extension_id(req):
    """全 Native Messaging アクションで拡張機能IDを必須検証する。
    Chromeがコマンドライン引数として渡すオリジン(chrome-extension://ID/)とも照合する。
    """
    raw_extension_id = req.get("extensionId")
    validated_id = _validate_extension_id(raw_extension_id)

    if not validated_id:
        logger.warning(
            "Native message rejected because extensionId is missing or invalid: action=%s id=%s",
            req.get("action"),
            str(raw_extension_id or "")[:20],
        )
        send_message({"ok": False, "error": "Invalid extension ID"})
        return None

    ancestors = _get_ancestor_process_names()
    if not _is_caller_authorized_browser(ancestors):
        logger.error(
            "Native message rejected because caller process is not an authorized browser: ancestors=%s action=%s",
            ancestors[:3],
            req.get("action"),
        )
        send_message({"ok": False, "error": "Unauthorized parent process"})
        return None


    # Chrome passes the extension origin as the first argument: chrome-extension://[id]/
    # (Edge also uses chrome-extension:// per Microsoft docs). Validate that the
    # message-level extensionId matches the process-level origin argument.
    # Fail closed when the origin argument is missing or unknown: otherwise any
    # local process could inject native messages without origin binding.
    if len(sys.argv) <= 1:
        logger.error(
            "Native message rejected because process origin argument is missing: action=%s",
            req.get("action"),
        )
        send_message({"ok": False, "error": "Missing process origin"})
        return None

    origin_arg = sys.argv[1].strip().lower()
    origin_prefix = "chrome-extension://"
    if not origin_arg.startswith(origin_prefix):
        logger.error(
            "Native message rejected because process origin is unrecognized: origin=%s action=%s",
            origin_arg[:80],
            req.get("action"),
        )
        send_message({"ok": False, "error": "Unrecognized process origin"})
        return None

    actual_id = origin_arg[len(origin_prefix) :].rstrip("/")

    if actual_id != validated_id:
        logger.error(
            "Security breach attempt: extensionId in message (%s) does not match process origin (%s)",
            validated_id,
            actual_id,
        )
        send_message({"ok": False, "error": "Origin mismatch"})
        return None

    return validated_id



def read_message():
    """Read a native message from stdin.

    Returns a decoded value on success, None on clean EOF, SKIP_FRAME for a
    fully consumed but invalid frame, or FATAL_FRAME when stream alignment can
    no longer be guaranteed.
    """
    payload_str_buf: list[str] = []
    payload = bytearray()
    payload_length = 0
    try:
        # Robust 4-byte header read loop: pipe chunks may arrive fragmented.
        header_chunks: list[Any] = []
        header_is_text: bool | None = None
        header_chars_read = 0
        while header_chars_read < 4:
            chunk = RAW_STDIN.read(4 - header_chars_read)
            if not chunk:
                break
            chunk_is_text = isinstance(chunk, str)
            if header_is_text is None:
                header_is_text = chunk_is_text
            if chunk_is_text != header_is_text:
                logger.error("Native message stream changed between text and binary modes")
                return FATAL_FRAME
            chunk_size = len(chunk)
            if header_chars_read + chunk_size > 4:
                logger.error("Native message header read past its 4-byte boundary")
                return FATAL_FRAME
            header_chunks.append(chunk)
            header_chars_read += chunk_size
        if header_chars_read == 0:
            return None
        if header_chars_read < 4:
            logger.error("Incomplete native message header (got %s bytes)", header_chars_read)
            return FATAL_FRAME

        # Handle both str and bytes for robustness in testing/mock environments
        if header_is_text:
            # StringIO-based test doubles represent the four raw header bytes as
            # latin-1 characters.  UTF-8 encoding here would expand bytes >= 128
            # and corrupt the frame length, so use a one-byte-preserving codec.
            try:
                header_bytes = "".join(header_chunks).encode("latin-1")
            except UnicodeEncodeError:
                logger.error("Native message text header is not byte-preserving")
                return FATAL_FRAME
        else:
            header_bytes = b"".join(
                bytes(chunk) if not isinstance(chunk, bytes) else chunk
                for chunk in header_chunks
            )

        length = struct.unpack("<I", header_bytes[:4])[0]
        if length > MAX_MESSAGE_BYTES:
            if length > MAX_DRAIN_BYTES:
                logger.error(
                    "Excessively large native message length rejected without drain: claimed=%s limit=%s",
                    length,
                    MAX_DRAIN_BYTES,
                )
                return FATAL_FRAME

            # Drain reasonable oversized frame so next length header starts at known boundary.
            remaining = length
            chunk_size = 65536
            drained = 0
            while remaining > 0:
                to_read = 1 if header_is_text else min(chunk_size, remaining)
                chunk = RAW_STDIN.read(to_read)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    try:
                        chunk_len = len(chunk.encode("utf-8"))
                    except UnicodeEncodeError:
                        return FATAL_FRAME
                else:
                    chunk_len = len(chunk)
                if chunk_len > remaining:
                    logger.error("Native message drain read past its frame boundary")
                    return FATAL_FRAME
                drained += chunk_len
                remaining -= chunk_len
            logger.error(
                "Oversized native message rejected and drained: claimed=%s drained=%s",
                length,
                drained,
            )
            if remaining > 0:
                return FATAL_FRAME
            return SKIP_FRAME

        # Loop until exactly ``length`` bytes are consumed: a single
        # ``read(n)`` on a pipe may return fewer bytes without EOF.
        if header_is_text:
            # A text stream's read(size) counts characters rather than UTF-8
            # bytes.  Read one character at a time so a multibyte character does
            # not consume bytes belonging to the next native frame.  The real
            # host uses sys.stdin.buffer; this branch exists for test doubles
            # and other text-mode wrappers.
            while payload_length < length:
                chunk = RAW_STDIN.read(1)
                if not chunk:
                    break
                if not isinstance(chunk, str) or len(chunk) != 1:
                    logger.error("Native message text stream returned an invalid chunk")
                    return FATAL_FRAME
                try:
                    chunk_length = len(chunk.encode("utf-8"))
                except UnicodeEncodeError:
                    logger.error("Native message text payload is not UTF-8 encodable")
                    return FATAL_FRAME
                if payload_length + chunk_length > length:
                    logger.error("Native message text payload crossed its frame boundary")
                    return FATAL_FRAME
                payload_str_buf.append(chunk)
                payload_length += chunk_length
            if payload_length < length:
                logger.error(
                    "Incomplete native message payload (expected %s, got %s)",
                    length,
                    payload_length,
                )
                return FATAL_FRAME
            return json.loads("".join(payload_str_buf))
        else:
            while len(payload) < length:
                remaining = length - len(payload)
                chunk = RAW_STDIN.read(remaining)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                if len(chunk) > remaining:
                    logger.error("Native message payload read past its frame boundary")
                    return FATAL_FRAME
                payload.extend(chunk)
            if len(payload) < length:
                logger.error(
                    "Incomplete native message payload (expected %s, got %s)",
                    length,
                    len(payload),
                )
                return FATAL_FRAME

        payload_str = bytes(payload).decode("utf-8")
        return json.loads(payload_str)
    except json.JSONDecodeError as e:
        logger.error(
            "JSON decode error while reading native message: %s; payload_len=%s",
            e,
            payload_length if payload_str_buf else len(payload),
        )
        return SKIP_FRAME
    except UnicodeDecodeError as e:
        logger.error("Invalid UTF-8 native message: %s", e)
        return SKIP_FRAME
    except (OSError, ValueError, struct.error) as e:
        logger.error("Read error (type=%s): %s", type(e).__name__, e)
        return FATAL_FRAME


SEND_LOCK = threading.Lock()


def send_message(message):
    """Send a native message to stdout."""
    try:
        content = json.dumps(message, ensure_ascii=False).encode("utf-8")
        with SEND_LOCK:
            RAW_STDOUT.write(struct.pack("<I", len(content)))
            RAW_STDOUT.write(content)
            RAW_STDOUT.flush()
        logger.debug("Message sent: %s", message.get("ok"))
    except (OSError, TypeError, ValueError) as e:
        logger.error("Send error: %s", e)


def main():
    """ネイティブメッセージホストのメインループ"""
    logger.info("Native host started (V3 - Binary/Redirected mode)")
    try:
        while True:
            req = read_message()
            if req is None:
                logger.info("Connection closed (EOF)")
                break
            if req is FATAL_FRAME:
                logger.error("Closing native messaging channel after framing error")
                break
            if req is SKIP_FRAME:
                # The complete frame was consumed, so the next header remains aligned.
                logger.warning("Skipping malformed native message frame")
                continue
            if not isinstance(req, dict):
                # Do not format an attacker-controlled megabyte-sized value or
                # control characters into the native-host log.
                logger.warning("Expected dict, got %s", type(req).__name__)
                continue

            action = req.get("action")

            # レート制限チェック
            if not _check_rate_limit():
                logger.warning("Rate limit exceeded for IPC messages")
                send_message({"ok": False, "error": "Rate limit exceeded"})
                continue

            # アクションのホワイトリスト検証
            if not isinstance(action, str) or len(action) > _MAX_ACTION_LENGTH:
                logger.warning(
                    "Rejected invalid action type=%s length=%s",
                    type(action).__name__,
                    len(action) if isinstance(action, (str, list, dict)) else "n/a",
                )
                send_message({"ok": False, "error": "Unknown or disallowed action"})
                continue
            if action not in ALLOWED_ACTIONS:
                logger.warning("Rejected unknown native-host action")
                send_message({"ok": False, "error": "Unknown or disallowed action"})
                continue

            logger.info("Processing action: %s", action)

            validated_id = _require_valid_extension_id(req)
            if not validated_id:
                continue

            if action == "start_backend":
                if start is not None:
                    try:
                        res = start(extension_id=validated_id)
                    except Exception as exc:  # pylint: disable=broad-exception-caught
                        # _startup_lock can raise OSError on Windows when another
                        # native-host process holds the lock longer than the
                        # msvcrt LK_LOCK retry window; the backend spawn can also
                        # fail with OSError. Keep the native-host loop alive so a
                        # transient contention does not kill the process and drop
                        # the browser extension's channel.
                        logger.error("start_backend failed: %s", type(exc).__name__)
                        send_message(
                            {
                                "ok": False,
                                "error": "Backend start failed. Retry the request.",
                            }
                        )
                        continue
                    send_message(res)
                else:
                    send_message({"ok": False, "error": "Backend starter missing"})
            elif action == "get_shutdown_token":
                if not _token_action_allowed():
                    send_message({"ok": False, "error": "Token action rate limit exceeded"})
                    continue
                # The token only protects a running backend, so never hand it
                # out while the backend is down (avoids useless secret exposure
                # and stale-token harvesting by a local process).
                if is_backend_healthy_once is None:
                    send_message({"ok": False, "error": "Backend health check unavailable"})
                    continue
                if not is_backend_healthy_once():
                    send_message({"ok": False, "error": "Backend is not running"})
                    continue
                # R1: Prefer the per-user runtime-state copy and fall back to
                # a legacy project-root copy so older backend installations
                # keep working after this native host update.
                try:
                    from config_store import APP_DATA_DIR as _TOKEN_STATE_DIR  # type: ignore

                    _TOKEN_STATE_DIR.mkdir(parents=True, exist_ok=True)
                except Exception:
                    _TOKEN_STATE_DIR = ROOT  # type: ignore[name-defined]
                primary_token_file = _TOKEN_STATE_DIR / ".mns_shutdown_token"  # type: ignore[operator]
                legacy_token_file = ROOT / ".mns_shutdown_token"
                primary_used_marker = _TOKEN_STATE_DIR / ".mns_shutdown_token.used"  # type: ignore[operator]
                legacy_used_marker = ROOT / ".mns_shutdown_token.used"
                if primary_token_file.exists():
                    token_file = primary_token_file
                    used_marker = primary_used_marker
                else:
                    token_file = legacy_token_file
                    used_marker = legacy_used_marker

                if used_marker.exists():
                    logger.warning("Shutdown token has already been consumed (used marker exists)")
                    send_message(
                        {
                            "ok": False,
                            "error": "Shutdown token has already been consumed. Restart backend to regenerate.",
                        }
                    )
                elif token_file.exists():
                    try:
                        # Enforce owner-only permissions on Unix. The token is
                        # encrypted at rest, but restricting the file removes a
                        # needless information-leak surface for the ciphertext.
                        if os.name != "nt":
                            import stat

                            file_mode = token_file.stat().st_mode
                            if file_mode & stat.S_IROTH:
                                logger.warning(
                                    "Token file is world-readable (mode=%o); "
                                    "restricting to owner-only (0o600).",
                                    file_mode,
                                )
                                try:
                                    token_file.chmod(0o600)
                                except OSError as perm_exc:
                                    logger.warning(
                                        "Failed to restrict shutdown token file permissions: %s",
                                        perm_exc,
                                    )
                        # R5 fix & R2 fix: acquire shared lock before reading to prevent
                        # reading a partially-written file during token rotation,
                        # retrying on Windows when temporarily locked by backend.
                        raw = ""
                        import random
                        for attempt in range(10):
                            try:
                                with open(token_file, "r", encoding="utf-8") as fh:
                                    if os.name == "nt":
                                        import msvcrt as _msvcrt
                                        _msvcrt_mod = cast(Any, _msvcrt)

                                        fd = fh.fileno()
                                        locked = False
                                        if os.fstat(fd).st_size > 0:
                                            try:
                                                _msvcrt_mod.locking(fd, _msvcrt_mod.LK_NBLCK, 1)
                                                locked = True
                                            except OSError:
                                                pass
                                        try:
                                            raw = fh.read().strip()
                                        finally:
                                            if locked:
                                                try:
                                                    os.lseek(fd, 0, os.SEEK_SET)
                                                    _msvcrt_mod.locking(fd, _msvcrt_mod.LK_UNLCK, 1)
                                                except OSError:
                                                    pass
                                    else:
                                        try:
                                            import fcntl as _fcntl  # type: ignore[import-not-found]

                                            _fcntl.flock(fh.fileno(), _fcntl.LOCK_SH)  # type: ignore[attr-defined]
                                            raw = fh.read().strip()
                                        except (ImportError, OSError):
                                            raw = fh.read().strip()
                                if raw:
                                    break
                            except OSError:
                                time.sleep(0.02 * (1.5**attempt) + random.uniform(0.005, 0.015))
                        if raw:
                            try:
                                entry = json.loads(raw)
                                token = unprotect_data(entry, "shutdown_token")
                            except (json.JSONDecodeError, TypeError, ValueError):
                                logger.warning(
                                    "Rejected legacy plaintext shutdown token file; restart backend to regenerate it securely."
                                )
                                token = ""  # nosec B105
                            if token:
                                send_message({"ok": True, "token": token})
                            else:
                                send_message(
                                    {
                                        "ok": False,
                                        "error": "Token file is invalid. Restart backend to regenerate.",
                                    }
                                )
                        else:
                            send_message(
                                {
                                    "ok": False,
                                    "error": "Token file is empty. Restart backend to regenerate.",
                                }
                            )
                    except Exception as e:
                        logger.error("Failed to read shutdown token: %s", e)
                        send_message(
                            {
                                "ok": False,
                                "error": "Failed to read token file. Restart backend to regenerate.",
                            }
                        )
                else:
                    send_message(
                        {
                            "ok": False,
                            "error": "Shutdown token file does not exist. Ensure backend is running.",
                        }
                    )
            elif action == "get_backend_port":
                if get_backend_port is not None:
                    send_message({"ok": True, "port": get_backend_port()})
                else:
                    try:
                        fallback_port = int(os.environ.get("MNS_BACKEND_PORT", "5000") or "5000")
                    except ValueError:
                        fallback_port = 5000
                    send_message({"ok": True, "port": fallback_port})
            elif action == "get_extension_api_token":
                if not _token_action_allowed():
                    send_message({"ok": False, "error": "Token action rate limit exceeded"})
                    continue
                # NH-2 / R9: The token only protects a running backend, so never
                # hand it out while the backend is down. This mirrors the
                # get_shutdown_token policy and avoids disclosing a long-lived
                # (90-day) reusable secret to a local process while no backend
                # is running to validate it against.
                if is_backend_healthy_once is None:
                    send_message({"ok": False, "error": "Backend health check unavailable"})
                    continue
                if not is_backend_healthy_once():
                    send_message({"ok": False, "error": "Backend is not running"})
                    continue
                try:
                    from credential_manager import get_or_create_extension_api_token

                    token = get_or_create_extension_api_token()
                    send_message({"ok": True, "token": token})
                except Exception as e:
                    logger.error("Failed to get extension token: %s", e)
                    send_message({"ok": False, "error": "Failed to get token"})
            elif action == "ping":
                send_message({"ok": True, "message": "pong"})
            else:
                # ここには到達しないはず（ホワイトリスト検証済み）
                send_message({"ok": False, "error": f"Unknown action: {action}"})

    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("Unexpected error in main")


if __name__ == "__main__":
    main()
