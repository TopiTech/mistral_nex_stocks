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
        from native_host.start_backend import get_backend_port, start
    except ImportError:
        from start_backend import get_backend_port, start  # type: ignore
except ImportError:
    logger.exception("Failed to import start_backend")
    start = None  # type: ignore
    get_backend_port = None  # type: ignore

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


def _safe_int_env(key: str, default: int) -> int:
    val = os.environ.get(key, "").strip()
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        logger.warning("Invalid integer env %s=%r; using default %d", key, val, default)
        return default


def _safe_float_env(key: str, default: float) -> float:
    val = os.environ.get(key, "").strip()
    if not val:
        return default
    try:
        return float(val)
    except ValueError:
        logger.warning("Invalid float env %s=%r; using default %f", key, val, default)
        return default


MAX_MESSAGE_BYTES = _safe_int_env("NATIVE_HOST_MAX_MESSAGE_BYTES", 1024 * 1024)
MAX_DRAIN_BYTES = _safe_int_env("NATIVE_HOST_MAX_DRAIN_BYTES", MAX_MESSAGE_BYTES * 2)

# A fully consumed frame with invalid contents is safe to skip. A truncated or
# undrainable frame loses stream alignment and must terminate the connection.
SKIP_FRAME = object()
FATAL_FRAME = object()


# --- Rate Limiting for IPC ---
_NATIVE_RATE_LIMIT_MAX = _safe_int_env("NATIVE_HOST_RATE_LIMIT_MAX", 10)
_NATIVE_RATE_LIMIT_WINDOW = _safe_float_env("NATIVE_HOST_RATE_LIMIT_WINDOW", 1.0)
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
_NATIVE_TOKEN_ACTION_MAX = _safe_int_env("NATIVE_HOST_TOKEN_ACTION_MAX", 3)
_NATIVE_TOKEN_ACTION_WINDOW = _safe_float_env("NATIVE_HOST_TOKEN_ACTION_WINDOW", 30.0)
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

    origin_arg = sys.argv[1].lower()
    actual_id = None
    for prefix in ("chrome-extension://", "extension://"):
        if origin_arg.startswith(prefix):
            actual_id = origin_arg[len(prefix) :].rstrip("/")
            break

    if actual_id is None:
        logger.error(
            "Native message rejected because process origin is unrecognized: origin=%s action=%s",
            origin_arg[:80],
            req.get("action"),
        )
        send_message({"ok": False, "error": "Unrecognized process origin"})
        return None

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
    try:
        header = RAW_STDIN.read(4)
        if len(header) == 0:
            return None
        if len(header) < 4:
            logger.error("Incomplete native message header (got %s bytes)", len(header))
            return FATAL_FRAME

        # Handle both str and bytes for robustness in testing/mock environments
        header_bytes = header.encode("utf-8") if isinstance(header, str) else header

        length = struct.unpack("<I", header_bytes)[0]
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
                to_read = min(chunk_size, remaining)
                chunk = RAW_STDIN.read(to_read)
                if not chunk:
                    break
                chunk_len = len(chunk) if not isinstance(chunk, str) else len(chunk.encode("utf-8"))
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

        payload = RAW_STDIN.read(length)
        if len(payload) < length:
            logger.error(
                "Incomplete native message payload (expected %s, got %s)",
                length,
                len(payload),
            )
            return FATAL_FRAME

        payload_str = payload if isinstance(payload, str) else payload.decode("utf-8")
        return json.loads(payload_str)
    except json.JSONDecodeError as e:
        payload_len = len(payload) if "payload" in locals() else 0
        logger.error(
            "JSON decode error while reading native message: %s; payload_len=%s",
            e,
            payload_len,
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
                send_message({"ok": False, "error": f"Unknown or disallowed action: {action}"})
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
                if not _token_action_allowed():
                    send_message({"ok": False, "error": "Token action rate limit exceeded"})
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
                token_file = primary_token_file if primary_token_file.exists() else legacy_token_file
                if token_file.exists():
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
                        raw = token_file.read_text(encoding="utf-8").strip()
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
