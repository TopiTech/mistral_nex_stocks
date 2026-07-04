#!/usr/bin/env python3
"""Native host wrapper for Chrome native messaging and backend startup."""

import io
import json
import logging
import os
import re
import struct
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import BinaryIO, cast

# --- I/O Protection & Binary Mode Setup ---
# Protocol streams (must be captured before stdout is redirected)
RAW_STDIN = cast(BinaryIO, getattr(sys.stdin, "buffer", sys.stdin))
RAW_STDOUT = cast(BinaryIO, getattr(sys.stdout, "buffer", sys.stdout))

if os.name == "nt":  # pragma: no cover
    import msvcrt  # pylint: disable=import-error

    # Ensure binary mode for raw streams on Windows. Pytest may provide pseudo
    # streams without fileno(), so skip this during import-time tests.
    try:
        msvcrt.setmode(RAW_STDIN.fileno(), 0x8000)  # _O_BINARY
        msvcrt.setmode(RAW_STDOUT.fileno(), 0x8000)  # _O_BINARY
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
    sensitive_patterns = [
        r"api[_-]?key['\"]?\s*[:=]\s*['\"]?[^\s'\"]+",
        r"token['\"]?\s*[:=]\s*['\"]?[^\s'\"]+",
        r"password['\"]?\s*[:=]\s*['\"]?[^\s'\"]+",
        r"authorization['\"]?\s*[:=]\s*['\"]?[^\s'\"]+",
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
_file_handler = RotatingFileHandler(
    Path(__file__).parent / "native_host.log",
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
        from native_host.start_backend import get_backend_port, start
    except ImportError:
        from start_backend import get_backend_port, start  # type: ignore
except ImportError as imp_exc:
    logger.error("Failed to import start_backend: %s", imp_exc, exc_info=True)
    start = None  # type: ignore
    get_backend_port = None  # type: ignore

try:
    from config_utils import unprotect_data
except ImportError as imp_exc:
    logger.error("Failed to import config_utils: %s", imp_exc, exc_info=True)
    def unprotect_data(entry: dict, key_name: str = "general_data") -> str:
        if isinstance(entry, dict) and key_name in entry:
            return str(entry[key_name])
        return str(entry)

MAX_MESSAGE_BYTES = int(
    os.environ.get("NATIVE_HOST_MAX_MESSAGE_BYTES", str(1024 * 1024))
)

# --- Rate Limiting for IPC ---
_NATIVE_RATE_LIMIT_MAX = int(os.environ.get("NATIVE_HOST_RATE_LIMIT_MAX", "10"))
_NATIVE_RATE_LIMIT_WINDOW = float(os.environ.get("NATIVE_HOST_RATE_LIMIT_WINDOW", "1.0"))
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

# --- Security Constants ---
# 許可されたアクションのホワイトリスト
ALLOWED_ACTIONS = frozenset(
    {"start_backend", "get_shutdown_token", "get_backend_port", "get_extension_api_token", "ping"}
)

# extensionId のフォーマット検証（Chrome 拡張IDは32文字の小文字英数字）
_EXTENSION_ID_PATTERN = re.compile(r"^[a-z0-9]{32}$")


def _load_allowed_manifest_origins():
    """ホストマニフェストから許可された拡張機能IDのセットを取得"""
    origins = set()
    try:
        manifest_path = ROOT / "native_host" / "com.mistral_nex_stocks.host.json"
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

    # Chrome passes the extension origin as the first argument: chrome-extension://[id]/
    # Validate that the message-level extensionId matches the process-level origin argument.
    if len(sys.argv) > 1:
        origin_arg = sys.argv[1].lower()
        if origin_arg.startswith("chrome-extension://"):
            actual_id = origin_arg[len("chrome-extension://") :].rstrip("/")
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
    """Read a native message from stdin."""
    try:
        header = RAW_STDIN.read(4)
        if len(header) == 0:
            return None
        if len(header) < 4:
            raise ValueError(f"Incomplete header (got {len(header)} bytes)")

        # Handle both str and bytes for robustness in testing/mock environments
        header_bytes = header.encode("utf-8") if isinstance(header, str) else header

        length = struct.unpack("<I", header_bytes)[0]
        if length > MAX_MESSAGE_BYTES:
            raise ValueError(f"Message too large ({length} bytes)")

        payload = RAW_STDIN.read(length)
        if len(payload) < length:
            raise ValueError(
                f"Incomplete payload (expected {length}, got {len(payload)})"
            )

        payload_str = payload if isinstance(payload, str) else payload.decode("utf-8")
        return json.loads(payload_str)
    except json.JSONDecodeError as e:
        payload_len = len(payload) if "payload" in locals() else 0
        logger.error(
            "JSON decode error while reading native message: %s; payload_len=%s",
            e,
            payload_len,
        )
        return None
    except (OSError, UnicodeDecodeError, ValueError) as e:
        logger.error("Read error (type=%s): %s", type(e).__name__, e)
        return None


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
            if not isinstance(req, dict):
                logger.warning("Expected dict, got %s: %s", type(req).__name__, req)
                continue

            action = req.get("action")

            # レート制限チェック
            if not _check_rate_limit():
                logger.warning("Rate limit exceeded for IPC messages")
                send_message({"ok": False, "error": "Rate limit exceeded"})
                continue

            # アクションのホワイトリスト検証
            if action not in ALLOWED_ACTIONS:
                logger.warning("Rejected unknown action: %s", action)
                send_message(
                    {"ok": False, "error": f"Unknown or disallowed action: {action}"}
                )
                continue

            logger.info("Processing action: %s", action)

            validated_id = _require_valid_extension_id(req)
            if not validated_id:
                continue

            if action == "start_backend":
                if start is not None:
                    res = start(extension_id=validated_id)
                    send_message(res)
                else:
                    send_message({"ok": False, "error": "Backend starter missing"})
            elif action == "get_shutdown_token":
                token_file = ROOT / ".mns_shutdown_token"
                if token_file.exists():
                    try:
                        # Check file permissions on Unix - warn if world-readable
                        if os.name != "nt":
                            import stat

                            file_mode = token_file.stat().st_mode
                            if file_mode & stat.S_IROTH:
                                logger.warning(
                                    "Token file is world-readable (mode=%o). "
                                    "Consider restricting permissions to owner only.",
                                    file_mode,
                                )
                        raw = token_file.read_text(encoding="utf-8").strip()
                        if raw:
                            try:
                                entry = json.loads(raw)
                                token = unprotect_data(entry, "shutdown_token")
                            except (json.JSONDecodeError, TypeError, ValueError):
                                logger.warning(
                                    "Rejected legacy plaintext shutdown token file; restart backend to regenerate it securely."
                                )
                                token = ""
                            if token:
                                send_message({"ok": True, "token": token})
                            else:
                                send_message(
                                    {"ok": False, "error": "Token file is invalid"}
                                )
                        else:
                            send_message({"ok": False, "error": "Token file is empty"})
                    except Exception as e:
                        logger.error("Failed to read shutdown token: %s", e)
                        send_message(
                            {"ok": False, "error": "Failed to read token file"}
                        )
                else:
                    send_message(
                        {"ok": False, "error": "Shutdown token file does not exist"}
                    )
            elif action == "get_backend_port":
                if get_backend_port is not None:
                    send_message({"ok": True, "port": get_backend_port()})
                else:
                    try:
                        fallback_port = int(
                            os.environ.get("MNS_BACKEND_PORT", "5000") or "5000"
                        )
                    except ValueError:
                        fallback_port = 5000
                    send_message({"ok": True, "port": fallback_port})
            elif action == "get_extension_api_token":
                try:
                    from config_utils import get_or_create_extension_api_token
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

    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.exception("Unexpected error in main: %s", e)


if __name__ == "__main__":
    main()
