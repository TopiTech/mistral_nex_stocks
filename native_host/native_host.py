#!/usr/bin/env python3
"""Native host wrapper for Chrome native messaging and backend startup."""

import json
import logging
import struct
import sys
import os
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
    from start_backend import start
except ImportError as imp_exc:
    logger.error("Failed to import start_backend: %s", imp_exc, exc_info=True)
    start = None

MAX_MESSAGE_BYTES = int(os.environ.get("NATIVE_HOST_MAX_MESSAGE_BYTES", str(1024 * 1024)))


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
            logger.info("Processing action: %s", action)

            if action == 'start_backend':
                if start:
                    res = start(extension_id=req.get('extensionId'))
                    send_message(res)
                else:
                    send_message({'ok': False, 'error': 'Backend starter missing'})
            elif action == 'ping':
                send_message({'ok': True, 'message': 'pong'})
            else:
                send_message({'ok': False, 'error': f'Unknown action: {action}'})

    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.exception("Unexpected error in main: %s", e)


if __name__ == '__main__':
    main()
