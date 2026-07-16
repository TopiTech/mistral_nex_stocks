"""
shutdown_manager.py - Shutdown token generation, validation, and rotation.

Extracted from app_state.py to reduce module complexity.
"""

import json
import logging
import platform
import threading
import time
from pathlib import Path
from typing import Optional


class ShutdownTokenManager:
    """Manages shutdown token generation, validation, and rotation."""

    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger("backend")
        base_dir = Path(__file__).resolve().parent
        self.token_file = base_dir / ".mns_shutdown_token"
        self.used_marker = base_dir / ".mns_shutdown_token.used"
        self.shutdown_token: Optional[str] = None
        self.shutdown_token_used = False
        self._lock = threading.Lock()

    def get_or_create_shutdown_token(self) -> str:
        with self._lock:
            if self.shutdown_token and not self.used_marker.exists():
                return self.shutdown_token

            was_used = self.used_marker.exists()
            if was_used:
                try:
                    self.used_marker.unlink(missing_ok=True)
                except OSError:
                    pass

            try:
                if not was_used and self.token_file.exists():
                    from crypto_utils import enforce_secure_permissions, unprotect_data

                    enforce_secure_permissions(self.token_file)
                    raw = self.token_file.read_text(encoding="utf-8").strip()
                    if raw:
                        try:
                            entry = json.loads(raw)
                            token = unprotect_data(entry, "shutdown_token")
                        except (json.JSONDecodeError, TypeError, ValueError):
                            self.logger.warning(
                                "Ignoring legacy plaintext shutdown token file; regenerating secure token."
                            )
                            token = ""  # nosec B105
                        if token:
                            self.shutdown_token = token
                            self.shutdown_token_used = False
                            return self.shutdown_token
            except (OSError, UnicodeDecodeError):
                pass

            import secrets
            from crypto_utils import protect_data, enforce_secure_permissions

            token = secrets.token_urlsafe(32)
            self.shutdown_token = token
            self.shutdown_token_used = False
            try:
                protected = protect_data(token, "shutdown_token")
                self.token_file.write_text(json.dumps(protected), encoding="utf-8")
                enforce_secure_permissions(self.token_file)
                self.logger.info("Session shutdown token generated and secured.")
            except Exception as exc:
                self.logger.error("Failed to write shutdown token file: %s", exc)
            return self.shutdown_token

    def consume_shutdown_token(self, token: str) -> bool:
        """Validate and mark the shutdown token as used (consume).

        For two-phase usage (validate then commit), use
        ``validate_shutdown_token`` followed by ``commit_shutdown_token``.
        """
        with self._lock:
            if not self.shutdown_token:
                self.logger.warning("No shutdown token configured")
                return False
            if self.shutdown_token_used:
                self.logger.warning("Shutdown token already used")
                return False
            if not token or not isinstance(token, str):
                return False

            import secrets

            if not secrets.compare_digest(self.shutdown_token, token):
                return False

            self.shutdown_token_used = True
            return True

    def validate_shutdown_token(self, token: str) -> bool:
        """Validate a shutdown token WITHOUT consuming it.

        Use this for pre-validation before an operation that may fail.
        Follow up with ``commit_shutdown_token`` after the operation succeeds.
        """
        with self._lock:
            if not self.shutdown_token or self.shutdown_token_used:
                return False
            if not token or not isinstance(token, str):
                return False
            import secrets

            return secrets.compare_digest(self.shutdown_token, token)

    def commit_shutdown_token(self) -> None:
        """Mark the shutdown token as consumed after a validated operation succeeds."""
        with self._lock:
            self.shutdown_token_used = True

    def rotate_shutdown_token(self):
        import secrets
        from crypto_utils import protect_data

        with self._lock:
            new_token = secrets.token_urlsafe(32)
            self.shutdown_token = new_token
            self.shutdown_token_used = False
            try:
                protected = protect_data(new_token, "shutdown_token")
                self.token_file.write_text(json.dumps(protected), encoding="utf-8")
                if platform.system().lower() != "windows":
                    self.token_file.chmod(0o600)
                self.used_marker.write_text(str(time.time()), encoding="utf-8")
                self.logger.info("New shutdown token generated after consumption.")
            except Exception as exc:
                self.logger.error("Failed to write new shutdown token: %s", exc)
