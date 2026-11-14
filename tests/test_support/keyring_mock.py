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


_SHARED_MEMORY_KEYRING: MemoryKeyring | None = None


def get_shared_memory_keyring() -> MemoryKeyring:
    """Return the process-wide MemoryKeyring singleton used for test isolation."""
    global _SHARED_MEMORY_KEYRING
    if _SHARED_MEMORY_KEYRING is None:
        _SHARED_MEMORY_KEYRING = MemoryKeyring()
    return _SHARED_MEMORY_KEYRING
