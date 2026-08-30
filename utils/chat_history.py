import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("backend")

# Allow the chat history database to live under MNS_DATA_DIR (same config/storage
# isolation used by config_store.py).  When MNS_DATA_DIR is set (e.g. by conftest.py
# during tests), the DB is scoped to that directory so multiple test sessions never
# share the same SQLite file.  Falls back to ``APP_DATA_DIR`` from config_store
# so runtime state stays outside the source tree.
_chat_db_dir = os.environ.get("MNS_DATA_DIR") or os.environ.get("MNS_APP_DATA_DIR")
if _chat_db_dir:
    DB_PATH = Path(_chat_db_dir) / "chat_history.db"
else:
    from config_store import APP_DATA_DIR
    DB_PATH = APP_DATA_DIR / "chat_history.db"

# Module-level guard to ensure init_db() runs at most once per process,
# regardless of how many SQLiteChatHistoryStore instances are created.
# The guard is protected by _db_init_lock to make the check-and-set atomic
# across threads.
_db_initialized: bool = False
_db_init_lock = threading.Lock()

_last_ts_lock = threading.Lock()
_last_ts: float = 0.0


def _get_timestamp() -> float:
    """Return a strictly increasing timestamp for session LRU ordering."""
    global _last_ts
    with _last_ts_lock:
        now = time.time()
        if now <= _last_ts:
            now = _last_ts + 0.000001
        _last_ts = now
        return now



# ---------------------------------------------------------------------------
# Message content encryption (M-3)
# Chat messages are encrypted at rest with Fernet using the same master key
# that protects user_stocks.json. Legacy rows written before this change are
# stored without the ``fernet:`` prefix and remain readable (read compatibility).
# ---------------------------------------------------------------------------
_FERNET_PREFIX = "fernet:"
_fernet_instance: Any = None
_fernet_lock = threading.Lock()


def _get_fernet():
    """Return a process-lifetime Fernet instance built from the master key."""
    global _fernet_instance
    if _fernet_instance is None:
        with _fernet_lock:
            if _fernet_instance is None:
                from cryptography.fernet import Fernet

                from config_store import get_or_create_master_key

                _fernet_instance = Fernet(get_or_create_master_key().encode("ascii"))
    return _fernet_instance


def _encrypt_content(content: str) -> str:
    """Encrypt a chat message body for storage.

    FAIL-CLOSED: if encryption is impossible (e.g. secure storage / master key
    unavailable), raise RuntimeError so callers do not persist plaintext chat.
    Legacy plaintext rows remain readable on load for backward compatibility.
    """
    if not content:
        return content
    try:
        token = _get_fernet().encrypt(content.encode("utf-8"))
        return _FERNET_PREFIX + token.decode("ascii")
    except Exception as exc:
        logger.error("Chat history encryption failed (fail-closed): %s", exc)
        raise RuntimeError(f"Chat history encryption failed: {exc}") from exc


def _decrypt_content(content: str) -> str:
    """Decrypt a chat message body read from storage.

    Values without the ``fernet:`` prefix are legacy plaintext rows and are
    returned unchanged. Decryption failures (e.g. master key rotated) return
    an empty string and log a warning rather than surfacing ciphertext.
    """
    if not content or not content.startswith(_FERNET_PREFIX):
        return content
    try:
        raw = content[len(_FERNET_PREFIX) :].encode("ascii")
        return _get_fernet().decrypt(raw).decode("utf-8")
    except Exception as exc:
        logger.warning("Chat history decryption failed (key rotated?): %s", exc)
        return ""


def _reset_db_state() -> None:
    """Reset the module-level DB initialization state for testing.

    TESTING ONLY: This clears the singleton guard so that the next call
    to init_db() re-initializes the database schema. Callers should also
    call SQLiteChatHistoryStore._reset_for_testing() to reset instance-level
    state.
    """
    global _db_initialized, _fernet_instance
    with _db_init_lock:
        _db_initialized = False
    with _fernet_lock:
        _fernet_instance = None


_SCHEMA_VERSION = 2


def _get_user_version(conn: sqlite3.Connection) -> int:
    """Read the current schema version from PRAGMA user_version."""
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    except Exception:
        return 0


def _run_migration(conn: sqlite3.Connection) -> None:
    """Run schema migrations incrementally based on PRAGMA user_version."""
    current_version = _get_user_version(conn)
    if current_version >= _SCHEMA_VERSION:
        return
    if current_version < 1:
        # v1: initial schema
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_sessions (
                session_id TEXT PRIMARY KEY,
                last_accessed REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                role TEXT,
                content TEXT,
                timestamp REAL,
                FOREIGN KEY(session_id) REFERENCES chat_sessions(session_id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_session ON chat_messages(session_id, id)"
        )
        current_version = 1
    if current_version < 2:
        # v2: add metadata columns for better observability
        try:
            conn.execute("ALTER TABLE chat_sessions ADD COLUMN created_at REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column may already exist
        try:
            conn.execute("ALTER TABLE chat_sessions ADD COLUMN message_count INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        current_version = 2
    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    conn.commit()


def init_db() -> None:
    """Initialize SQLite database for chat history with WAL mode and isolation.

    Thread-safe: uses ``_db_init_lock`` to ensure only one thread performs
    the actual initialization. Subsequent calls (including concurrent ones)
    are no-ops. Runs schema migrations based on PRAGMA user_version.
    """
    global _db_initialized
    if _db_initialized:
        return
    with _db_init_lock:
        if _db_initialized:
            return
        try:
            import os as _os

            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            if _os.name != "nt":
                try:
                    _os.chmod(DB_PATH.parent, 0o700)
                except OSError:
                    pass
            conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
            try:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA wal_autocheckpoint=100;")
                conn.execute("PRAGMA foreign_keys=ON;")
                _run_migration(conn)
            finally:
                conn.close()
            if _os.name != "nt":
                try:
                    _os.chmod(DB_PATH, 0o600)
                except OSError:
                    pass
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(str(DB_PATH) + suffix)
                    if sidecar.exists():
                        try:
                            _os.chmod(sidecar, 0o600)
                        except OSError:
                            pass
            _db_initialized = True
        except Exception as e:
            logger.error("Failed to initialize SQLite chat history database: %s", e)


# NOTE: init_db() is intentionally NOT called at module import time.
# The database is initialized lazily when the first SQLiteChatHistoryStore
# instance is created. This avoids side effects at import time.


class SQLiteChatHistoryStore:
    """SQLite-backed persistent chat store with dict-like compatibility.

    Uses a dedicated connection per thread (via threading.local) to avoid
    the ``check_same_thread=False`` pattern, which creates a correctness
    burden that the previous implementation's shared-lock design did not
    fully satisfy.  Each thread gets its own connection, so operations from
    different threads never contend on the same SQLite handle.

    Thread-local connections are automatically closed when the store instance
    is garbage-collected via ``weakref.finalize``, preventing connection leaks
    even if ``close()`` is never explicitly called.
    """

    def __init__(self, max_sessions: int = 50, max_msgs_per_session: int = 30) -> None:
        self.max_sessions = max_sessions
        self.max_msgs_per_session = max_msgs_per_session
        self._local = threading.local()
        self._schema_lock = threading.Lock()
        self._active_conns: set[sqlite3.Connection] = set()
        self._conns_lock = threading.Lock()
        # Lazy initialization: ensure DB schema exists on first use.
        init_db()
        # Ensure the thread-local connection (created lazily per thread) is
        # closed when this store is garbage-collected, so SQLite handles are
        # not leaked until process exit (which would emit ResourceWarning).
        self._finalizer: Any = None
        try:
            import weakref

            self._finalizer = weakref.finalize(
                self, SQLiteChatHistoryStore._close_local_conn, self._local, self._active_conns, self._conns_lock
            )
        except Exception:
            self._finalizer = None

    def __del__(self) -> None:
        try:
            self.close_all()
        except Exception:
            pass

    @staticmethod
    def _close_local_conn(local, active_conns=None, conns_lock=None) -> None:
        conn = getattr(local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except (sqlite3.Error, OSError):
                pass
            local.conn = None
        if active_conns is not None and conns_lock is not None:
            try:
                with conns_lock:
                    for c in list(active_conns):
                        try:
                            c.close()
                        except (sqlite3.Error, OSError):
                            pass
                    active_conns.clear()
            except Exception:  # nosec B110
                pass

    # ------------------------------------------------------------------
    # Connection-per-thread management
    # ------------------------------------------------------------------

    def _get_connection(self) -> sqlite3.Connection:
        """Return a connection for the current thread (lazy-created per thread).

        The connection is cached on ``self._local`` so each thread creates
        at most one connection over its lifetime.  This avoids both the
        ``check_same_thread=False`` anti-pattern and the overhead of opening
        a new connection per operation.
        """
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(str(DB_PATH), timeout=30.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA wal_autocheckpoint=100;")
        conn.execute("PRAGMA foreign_keys=ON;")
        self._local.conn = conn
        with self._conns_lock:
            self._active_conns.add(conn)
        return conn

    @classmethod
    def _reset_for_testing(cls) -> None:
        """Reset module-level state for test isolation.

        TESTING ONLY: This clears the singleton DB initialization guard so
        that the next SQLiteChatHistoryStore instance will re-initialize
        the database schema. Call ``_reset_db_state()`` as well to reset
        the module-level guard. Use in conjunction with test fixtures that
        need a fresh database state.
        """
        global _fernet_instance
        _fernet_instance = None
        _reset_db_state()

    def close(self) -> None:
        """Explicitly close the connection for the current thread.

        Call this when a worker thread finishes (e.g. in a finally block)
        to avoid leaking SQLite connections over the process lifetime.
        """
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except (sqlite3.Error, OSError) as exc:
                logger.debug("Error closing thread-local chat history connection: %s", exc)
            with self._conns_lock:
                self._active_conns.discard(conn)
            self._local.conn = None  # type: ignore[attr-defined]

    def close_all(self) -> None:
        """Close all active SQLite connections across all threads."""
        self.close()
        with self._conns_lock:
            for conn in list(self._active_conns):
                try:
                    conn.close()
                except (sqlite3.Error, OSError) as exc:
                    logger.debug("Error closing tracked chat history connection: %s", exc)
            self._active_conns.clear()

    # ------------------------------------------------------------------
    # Transaction helpers
    # ------------------------------------------------------------------

    def _execute_in_transaction(self, callback):
        """Execute *callback(conn, cursor)* inside a transaction.

        Commits on success, rolls back on failure.  Returns whatever the
        callback returns.
        """
        max_retries = 5
        backoff = 0.05
        for attempt in range(max_retries):
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                result = callback(conn, cursor)
                conn.commit()
                return result
            except sqlite3.OperationalError as exc:
                conn.rollback()
                err_msg = str(exc).lower()
                if ("locked" in err_msg or "busy" in err_msg) and attempt < max_retries - 1:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise
            except Exception:
                conn.rollback()
                raise

    # ------------------------------------------------------------------
    # Append-only helper (M-4)
    # ------------------------------------------------------------------

    def add_message(self, session_id: str, message: dict) -> None:
        """Append a single message to a chat session without read-modify-write.

        This is an append-only operation that avoids the full get-modify-set
        cycle of ``__getitem__`` + modify + ``__setitem__``, which is both
        more efficient and less prone to race conditions between the read and
        write phases when the lock is released.

        The method enforces ``max_msgs_per_session`` by deleting the oldest
        non-system message(s) after insertion.  Session-count eviction is NOT
        performed here because ``__setitem__`` (the full-sync path) already
        enforces ``max_sessions`` via ``_enforce_session_limit``, and this
        append-only path is designed to be lightweight.

        Args:
            session_id: The chat session identifier (e.g. "us:AAPL").
            message: A dict with ``role`` and ``content`` keys.
        """

        def _add(conn, cursor):
            cursor.execute(
                """
                INSERT INTO chat_sessions (session_id, last_accessed)
                VALUES (?, ?)
                ON CONFLICT(session_id) DO UPDATE SET last_accessed = excluded.last_accessed
                """,
                (session_id, _get_timestamp()),
            )
            # Insert the new message (content encrypted at rest, M-3)
            cursor.execute(
                """
                INSERT INTO chat_messages (session_id, role, content, timestamp)
                VALUES (?, ?, ?, ?)
                """,
                (
                    session_id,
                    message["role"],
                    _encrypt_content(message["content"]),
                    _get_timestamp(),
                ),
            )
            # Enforce per-session message limit: remove oldest non-system messages
            cursor.execute("SELECT COUNT(*) FROM chat_messages WHERE session_id = ?", (session_id,))
            msg_count = cursor.fetchone()[0]
            if msg_count > self.max_msgs_per_session:
                # Keep the system message (role='system') + the most recent ones
                cursor.execute(
                    """
                    DELETE FROM chat_messages
                    WHERE id IN (
                        SELECT id FROM chat_messages
                        WHERE session_id = ? AND role != 'system'
                        ORDER BY id ASC
                        LIMIT ?
                    )
                    """,
                    (session_id, msg_count - self.max_msgs_per_session),
                )
            self._enforce_session_limit(cursor)

        try:
            self._execute_in_transaction(_add)
        except (sqlite3.Error, OSError, ValueError, RuntimeError, TypeError) as e:
            logger.error("Failed to add chat message for session %s: %s", session_id, e)
            raise

    # ------------------------------------------------------------------
    # Dict-like interface
    # ------------------------------------------------------------------

    def __contains__(self, key: str) -> bool:
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            try:
                cursor.execute("SELECT 1 FROM chat_sessions WHERE session_id = ?", (key,))
                return cursor.fetchone() is not None
            finally:
                cursor.close()
                conn.rollback()
        except (sqlite3.Error, OSError):
            return False

    def __getitem__(self, key: str) -> list[dict[str, Any]]:
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """
                    SELECT role, content FROM chat_messages
                    WHERE session_id = ?
                    ORDER BY id ASC
                    """,
                    (key,),
                )
                rows = cursor.fetchall()
                if not rows:
                    # Session exists but has no messages (newly created session)
                    cursor.execute("SELECT 1 FROM chat_sessions WHERE session_id = ?", (key,))
                    if cursor.fetchone() is not None:
                        return []
                    raise KeyError(key)
                return [{"role": r[0], "content": _decrypt_content(r[1])} for r in rows]
            finally:
                cursor.close()
                conn.rollback()
        except KeyError:
            raise
        except (sqlite3.Error, OSError, IndexError) as e:
            logger.error("Failed to get chat history for session %s: %s", key, e)
            return []

    def get(self, key: str, default: Any = None) -> Any:
        """Return the messages for *key*, or *default* if key is not found."""
        try:
            return self[key]
        except KeyError:
            return default

    def __setitem__(self, key: str, value: list[dict[str, Any]]) -> None:
        def _set(conn, cursor):
            cursor.execute("DELETE FROM chat_messages WHERE session_id = ?", (key,))
            cursor.execute(
                """
                INSERT INTO chat_sessions (session_id, last_accessed)
                VALUES (?, ?)
                ON CONFLICT(session_id) DO UPDATE SET last_accessed = excluded.last_accessed
                """,
                (key, _get_timestamp()),
            )
            if value:
                to_insert = value
                if len(to_insert) > self.max_msgs_per_session:
                    system_msg = to_insert[0] if to_insert[0]["role"] == "system" else None
                    if system_msg:
                        remaining_slots = self.max_msgs_per_session - 1
                        if remaining_slots > 0:
                            to_insert = [system_msg] + to_insert[-remaining_slots:]
                        else:
                            to_insert = [system_msg]
                    else:
                        to_insert = to_insert[-self.max_msgs_per_session :]
                cursor.executemany(
                    """
                    INSERT INTO chat_messages (session_id, role, content, timestamp)
                    VALUES (?, ?, ?, ?)
                    """,
                    [
                        (key, msg["role"], _encrypt_content(msg["content"]), _get_timestamp())
                        for msg in to_insert
                    ],
                )
            self._enforce_session_limit(cursor)

        try:
            self._execute_in_transaction(_set)
        except (sqlite3.Error, OSError, ValueError, KeyError, RuntimeError, TypeError) as e:
            logger.error("Failed to set chat history for session %s: %s", key, e)
            raise

    def _enforce_session_limit(self, cursor: sqlite3.Cursor) -> None:
        try:
            cursor.execute("SELECT COUNT(*) FROM chat_sessions")
            count = cursor.fetchone()[0]
            if count > self.max_sessions:
                limit_to_delete = count - self.max_sessions
                cursor.execute(
                    "SELECT session_id FROM chat_sessions ORDER BY last_accessed ASC LIMIT ?",
                    (limit_to_delete,),
                )
                sessions_to_delete = [r[0] for r in cursor.fetchall()]
                if sessions_to_delete:
                    cursor.executemany(
                        "DELETE FROM chat_sessions WHERE session_id = ?",
                        [(session_id,) for session_id in sessions_to_delete],
                    )
        except (sqlite3.Error, OSError, ValueError) as e:
            logger.error("Failed to enforce session limit: %s", e)

    def move_to_end(self, key: str) -> None:
        """Touch the session to update last_accessed timestamp."""
        def _touch(conn, cursor):
            cursor.execute(
                """
                INSERT INTO chat_sessions (session_id, last_accessed)
                VALUES (?, ?)
                ON CONFLICT(session_id) DO UPDATE SET last_accessed = excluded.last_accessed
                """,
                (key, _get_timestamp()),
            )
            # R7: move_to_end can insert a new empty session (e.g. via
            # upstream callers touching a non-existent key). Enforce the
            # session cap so a spray of distinct keys cannot bypass the limit.
            count = cursor.execute("SELECT COUNT(*) FROM chat_sessions").fetchone()[0]
            if count > self.max_sessions:
                limit_to_delete = count - self.max_sessions
                cursor.execute(
                    "SELECT session_id FROM chat_sessions ORDER BY last_accessed ASC LIMIT ?",
                    (limit_to_delete,),
                )
                sessions_to_delete = [r[0] for r in cursor.fetchall()]
                if sessions_to_delete:
                    cursor.executemany(
                        "DELETE FROM chat_sessions WHERE session_id = ?",
                        [(session_id,) for session_id in sessions_to_delete],
                    )

        try:
            self._execute_in_transaction(_touch)
        except (sqlite3.Error, OSError, ValueError) as e:
            logger.debug("Failed to touch chat session %s: %s", key, e)

    def popitem(self, last: bool = False) -> None:
        """Remove the oldest or newest session based on last flag."""

        def _pop(conn, cursor):
            if last:
                cursor.execute(
                    "SELECT session_id FROM chat_sessions ORDER BY last_accessed DESC LIMIT 1"
                )
            else:
                cursor.execute(
                    "SELECT session_id FROM chat_sessions ORDER BY last_accessed ASC LIMIT 1"
                )
            row = cursor.fetchone()
            if row:
                cursor.execute("DELETE FROM chat_sessions WHERE session_id = ?", (row[0],))

        try:
            self._execute_in_transaction(_pop)
        except (sqlite3.Error, OSError, ValueError) as e:
            logger.error("Failed to pop session: %s", e)

    def __delitem__(self, key: str) -> None:
        """Delete a session and its associated messages, raising KeyError if missing."""
        deleted = False

        def _del(conn, cursor):
            nonlocal deleted
            cursor.execute("DELETE FROM chat_sessions WHERE session_id = ?", (key,))
            deleted = cursor.rowcount > 0

        try:
            self._execute_in_transaction(_del)
        except (sqlite3.Error, OSError, ValueError) as e:
            logger.error("Failed to delete chat session %s: %s", key, e)
            raise KeyError(key) from e

        if not deleted:
            raise KeyError(key)

    def pop(self, key: str, *args: Any) -> Any:
        """Remove *key* and return its messages, or *default* if not found."""
        if len(args) > 1:
            raise TypeError(f"pop expected at most 2 arguments, got {1 + len(args)}")
        try:
            val = self[key]
            del self[key]
            return val
        except KeyError:
            if args:
                return args[0]
            raise

    def clear(self) -> None:
        def _clear(conn, cursor):
            cursor.execute("DELETE FROM chat_sessions")

        try:
            self._execute_in_transaction(_clear)
        except (sqlite3.Error, OSError, ValueError) as e:
            logger.error("Failed to clear chat history: %s", e)

    def __len__(self) -> int:
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            try:
                cursor.execute("SELECT COUNT(*) FROM chat_sessions")
                res = cursor.fetchone()
                return res[0] if res else 0
            finally:
                cursor.close()
                conn.rollback()
        except (sqlite3.Error, OSError):
            return 0
