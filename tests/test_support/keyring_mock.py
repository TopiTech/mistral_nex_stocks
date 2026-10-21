"""In-memory keyring backend mock for test isolation."""

from __future__ import annotations

import keyring.errors
from keyring.backend import KeyringBackend


class MemoryKeyring(KeyringBackend):
    """In-memory keyring implementation for tests to prevent accessing OS keyrings."""

    priority = 10

    def __init__(self) -> None:
        self.passwords: dict[tuple[str, str], str] = {}

    def set_password(self, servicename: str, username: str, password: str) -> None:
        self.passwords[(servicename, username)] = password

    def get_password(self, servicename: str, username: str) -> str | None:
        return self.passwords.get((servicename, username), None)

    def delete_password(self, servicename: str, username: str) -> None:
        if (servicename, username) not in self.passwords:
            raise keyring.errors.PasswordDeleteError("Password not found")
        self.passwords.pop((servicename, username), None)

    def clear(self) -> None:
        """Clear all stored test passwords."""
        self.passwords.clear()
