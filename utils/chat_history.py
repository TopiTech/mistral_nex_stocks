import sqlite3
import time
import logging
import threading
from pathlib import Path
from typing import Any
import os
from constants import BASE_DIR

logger = logging.getLogger("backend")

# Allow the chat history database to live under MNS_DATA_DIR (same config/storage
# isolation used by config_store.py).  When MNS_DATA_DIR is set (e.g. by conftest.py
# during tests), the DB is scoped to that directory so multiple test sessions never
# share the same SQLite file.  Falls back to ``BASE_DIR/.cache/chat_history.db``
# for production (unchanged behavior).
_chat_db_dir = os.environ.get("MNS_DATA_DIR") or os.environ.get("MNS_APP_DATA_DIR")
if _chat_db_dir:
    DB_PATH = Path(_chat_db_dir) / "chat_history.db"
else:
    DB_PATH = BASE_DIR / ".cache" / "chat_history.db"

# Module-level guard to ensure init_db() runs at most once per process,
# regardless of how many SQLiteChatHistoryStore instances are created.
# The guard is protected by _db_init_lock to make the check-and-set atomic
# across threads.
_db_initialized: bool = False
_db_init_lock = threading.Lock()


def _reset_db_state() -> None:
    """Reset the module-level DB initialization state for testing.

    TESTING ONLY: This clears the singleton guard so that the next call
    to init_db() re-initializes the database schema. Callers should also
    call SQLiteChatHistoryStore._reset_for_testing() to reset instance-level
    state.
    """
    global _db_initialized
    with _db_init_lock:
        _db_initialized = False


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
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
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
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
            try:
                _run_migration(conn)
            finally:
                conn.close()
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
        # Lazy initialization: ensure DB schema exists on first use.
        init_db()
        # Ensure the thread-local connection (created lazily per thread) is
        # closed when this store is garbage-collected, so SQLite handles are
        # not leaked until process exit (which would emit ResourceWarning).
        # Each thread that touched the store gets its own connection; the
        # finalizer closes whichever connection exists on the collecting
        # thread (usually the main thread at interpreter shutdown).
        self._finalizer: Any = None
        try:
            import weakref

            self._finalizer = weakref.finalize(
                self, SQLiteChatHistoryStore._close_local_conn, self._local
            )
        except Exception:
            self._finalizer = None

    @staticmethod
    def _close_local_conn(local) -> None:
        conn = getattr(local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # nosec B110
                pass
            local.conn = None

    # ------------------------------------------------------------------
    # Connection-per-thread management
    # ------------------------------------------------------------------

    def _get_connection(self) -> sqlite3.Connection:
        """Return a connection for the current thread (lazy-created per thread).

        The connection is cached on ``self._local`` so each thread creates
        at most one connection over its lifetime.  This avoids both the
        ``check_same_thread=False`` anti-pattern and the overhead of opening
        a new connection per operation.

        Thread-local connections are closed explicitly via the ``close()``
        method, which should be called when a worker thread finishes (e.g.
        in a finally block in background jobs) to prevent leaking connections
        over the process lifetime (M-3).
        """
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        self._local.conn = conn
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
            except Exception as exc:
                logger.debug("Error closing thread-local chat history connection: %s", exc)
            self._local.conn = None  # type: ignore[attr-defined]

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
                (session_id, time.time()),
            )
            # Insert the new message
            cursor.execute(
                """
                INSERT INTO chat_messages (session_id, role, content, timestamp)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, message["role"], message["content"], time.time()),
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

        try:
            self._execute_in_transaction(_add)
        except Exception as e:
            logger.error("Failed to add chat message for session %s: %s", session_id, e)

    # ------------------------------------------------------------------
    # Dict-like interface
    # ------------------------------------------------------------------

    def __contains__(self, key: str) -> bool:
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM chat_sessions WHERE session_id = ?", (key,))
            return cursor.fetchone() is not None
        except Exception:
            return False

    def __getitem__(self, key: str) -> list[dict[str, Any]]:
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
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
            return [{"role": r[0], "content": r[1]} for r in rows]
        except KeyError:
            raise
        except Exception as e:
            logger.error("Failed to get chat history for session %s: %s", key, e)
            return []

    def __setitem__(self, key: str, value: list[dict[str, Any]]) -> None:
        def _set(conn, cursor):
            cursor.execute("DELETE FROM chat_messages WHERE session_id = ?", (key,))
            cursor.execute(
                """
                INSERT INTO chat_sessions (session_id, last_accessed)
                VALUES (?, ?)
                ON CONFLICT(session_id) DO UPDATE SET last_accessed = excluded.last_accessed
                """,
                (key, time.time()),
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
                    [(key, msg["role"], msg["content"], time.time()) for msg in to_insert],
                )
            self._enforce_session_limit(cursor)

        try:
            self._execute_in_transaction(_set)
        except Exception as e:
            logger.error("Failed to set chat history for session %s: %s", key, e)

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
                    # placeholders are only "?" characters — no user data in the
                    # SQL string itself, so this is a safe parameterized query.
                    placeholders = ",".join(["?"] * len(sessions_to_delete))
                    stmt = "DELETE FROM chat_sessions WHERE session_id IN (" + placeholders + ")"  # nosec B608
                    cursor.execute(stmt, sessions_to_delete)
        except Exception as e:
            logger.error("Failed to enforce session limit: %s", e)

    def move_to_end(self, key: str) -> None:
        """Touch the session to update last_accessed timestamp."""
        try:
            conn = self._get_connection()
            conn.execute(
                """
                INSERT INTO chat_sessions (session_id, last_accessed)
                VALUES (?, ?)
                ON CONFLICT(session_id) DO UPDATE SET last_accessed = excluded.last_accessed
                """,
                (key, time.time()),
            )
            conn.commit()
        except Exception as e:
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
        except Exception as e:
            logger.error("Failed to pop session: %s", e)

    def clear(self) -> None:
        try:
            conn = self._get_connection()
            conn.execute("DELETE FROM chat_sessions")
            conn.commit()
        except Exception as e:
            logger.error("Failed to clear chat history: %s", e)

    def __len__(self) -> int:
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM chat_sessions")
            return cursor.fetchone()[0]
        except Exception:
            return 0
