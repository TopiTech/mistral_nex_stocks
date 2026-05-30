#!/usr/bin/env python3
"""Native host wrapper for Chrome native messaging and backend startup."""

import json
import logging
import struct
import sys
import os
import re
from pathlib import Path
from logging.handlers import RotatingFileHandler

# --- I/O Protection & Binary Mode Setup ---
# Protocol streams (must be captured before stdout is redirected)
RAW_STDIN = sys.stdin.buffer
RAW_STDOUT = sys.stdout.buffer

if os.name == 'nt':
    import msvcrt
    # Ensure binary mode for raw streams on Windows
    msvcrt.setmode(RAW_STDIN.fileno(), 0x8000)  # _O_BINARY
    msvcrt.setmode(RAW_STDOUT.fileno(), 0x8000)  # _O_BINARY


# Redirect stdout to stderr so that stray print calls don't break the protocol
class StdoutRedirectionGuard:
    """stdoutをstderrへリダイレクトするガード"""
    def write(self, data):
        """データをstderrに書き込む"""
        sys.stderr.write(data)

    def flush(self):
        """stderrをフラッシュする"""
        sys.stderr.flush()


sys.stdout = StdoutRedirectionGuard()

# --- Logging Configuration ---
# Since stdout is now redirected to stderr, we must be careful with logging levels
logging.basicConfig(
    level=logging.DEBUG,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    handlers=[
        RotatingFileHandler(
            Path(__file__).parent / 'native_host.log',
            maxBytes=1024 * 1024,
            backupCount=3,
            encoding='utf-8',
        ),
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger(__name__)

# Suppress debug/info logs from stderr to avoid cluttering Chrome's stderr capture
for _handler in logging.getLogger().handlers:
    if isinstance(_handler, logging.StreamHandler) and getattr(
        _handler, 'stream', None
    ) is sys.stderr:
        _handler.setLevel(logging.WARNING)

# --- Imports and Constants ---
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'native_host'))
try:
    from start_backend import start, get_backend_port
except ImportError as imp_exc:
    logger.error("Failed to import start_backend: %s", imp_exc, exc_info=True)
    start = None
    get_backend_port = None

MAX_MESSAGE_BYTES = int(os.environ.get("NATIVE_HOST_MAX_MESSAGE_BYTES", str(1024 * 1024)))

# --- Security Constants ---
# 許可されたアクションのホワイトリスト
ALLOWED_ACTIONS = frozenset({"start_backend", "get_shutdown_token", "get_backend_port", "ping"})

# extensionId のフォーマット検証（Chrome 拡張IDは32文字の小文字英数字）
_EXTENSION_ID_PATTERN = re.compile(r'^[a-z0-9]{32}$')


def _validate_extension_id(extension_id):
    """Chrome 拡張機能のIDフォーマットを検証"""
    if extension_id is None:
        return None
    extension_id = str(extension_id).strip()
    if _EXTENSION_ID_PATTERN.match(extension_id):
        return extension_id
    logger.warning("Invalid extension ID format: %s", extension_id[:20] if extension_id else "None")
    return None


def read_message():
    """Read a native message from stdin."""
    try:
        header = RAW_STDIN.read(4)
        if len(header) == 0:
            return None
        if len(header) < 4:
            raise ValueError(f'Incomplete header (got {len(header)} bytes)')

        length = struct.unpack('<I', header)[0]
        if length > MAX_MESSAGE_BYTES:
            raise ValueError(f'Message too large ({length} bytes)')

        payload = RAW_STDIN.read(length)
        if len(payload) < length:
            raise ValueError(f'Incomplete payload (expected {length}, got {len(payload)})')

        return json.loads(payload.decode('utf-8'))
    except json.JSONDecodeError as e:
        logger.error(
            "JSON decode error while reading native message: %s; payload=%s",
            e,
            payload.decode('utf-8', errors='replace')
            if 'payload' in locals() else '<missing payload>',
        )
        return None
    except (OSError, UnicodeDecodeError, ValueError) as e:
        logger.error("Read error (type=%s): %s", type(e).__name__, e)
        return None


def send_message(message):
    """Send a native message to stdout."""
    try:
        content = json.dumps(message, ensure_ascii=False).encode('utf-8')
        RAW_STDOUT.write(struct.pack('<I', len(content)))
        RAW_STDOUT.write(content)
        RAW_STDOUT.flush()
        logger.debug("Message sent: %s", message.get('ok'))
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

            action = req.get('action')
            
            # アクションのホワイトリスト検証
            if action not in ALLOWED_ACTIONS:
                logger.warning("Rejected unknown action: %s", action)
                send_message({'ok': False, 'error': f'Unknown or disallowed action: {action}'})
                continue
            
            logger.info("Processing action: %s", action)

            if action == 'start_backend':
                if start:
                    # extensionId の検証
                    raw_extension_id = req.get('extensionId')
                    validated_id = _validate_extension_id(raw_extension_id)
                    if raw_extension_id and not validated_id:
                        logger.warning("Invalid extensionId rejected: %s", str(raw_extension_id)[:20])
                        send_message({'ok': False, 'error': 'Invalid extension ID format'})
                        continue
                    res = start(extension_id=validated_id)
                    send_message(res)
                else:
                    send_message({'ok': False, 'error': 'Backend starter missing'})
            elif action == 'get_shutdown_token':
                token_file = ROOT / ".mns_shutdown_token"
                if token_file.exists():
                    try:
                        token = token_file.read_text(encoding="utf-8").strip()
                        send_message({'ok': True, 'token': token})
                    except Exception as e:
                        logger.error("Failed to read shutdown token: %s", e)
                        send_message({'ok': False, 'error': 'Failed to read token file'})
                else:
                    send_message({'ok': False, 'error': 'Shutdown token file does not exist'})
            elif action == 'get_backend_port':
                if get_backend_port:
                    send_message({'ok': True, 'port': get_backend_port()})
                else:
                    try:
                        fallback_port = int(os.environ.get("MNS_BACKEND_PORT", "5000") or "5000")
                    except ValueError:
                        fallback_port = 5000
                    send_message({'ok': True, 'port': fallback_port})
            elif action == 'ping':
                send_message({'ok': True, 'message': 'pong'})
            else:
                # ここには到達しないはず（ホワイトリスト検証済み）
                send_message({'ok': False, 'error': f'Unknown action: {action}'})

    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.exception("Unexpected error in main: %s", e)


if __name__ == '__main__':
    main()
