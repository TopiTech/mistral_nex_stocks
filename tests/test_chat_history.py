"""Unit tests for SQLiteChatHistoryStore in utils/chat_history.py."""

import pytest

import utils.chat_history as chat_history_module
from utils.chat_history import SQLiteChatHistoryStore


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Monkeypatches the DB_PATH to a temporary path and re-runs init_db.

    Resets the module-level ``_db_initialized`` flag so that each test method
    gets a fresh database at its own temp path. Without this reset, the guard
    inside init_db() would skip DB creation for all tests after the first one,
    because the flag persists across tests within the same Python process.
    """
    db_file = tmp_path / "chat_history.db"
    monkeypatch.setattr(chat_history_module, "DB_PATH", db_file)
    monkeypatch.setattr(chat_history_module, "_db_initialized", False)
    chat_history_module.init_db()
    return db_file


def test_sqlite_chat_history_basic_operations(temp_db):
    store = SQLiteChatHistoryStore(max_sessions=5, max_msgs_per_session=10)

    # Basic dict interface __contains__ and __getitem__/__setitem__
    session_id = "test_session"
    assert session_id not in store

    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello!"},
    ]

    store[session_id] = messages
    assert session_id in store

    loaded = store[session_id]
    assert len(loaded) == 2
    assert loaded[0]["role"] == "system"
    assert loaded[1]["content"] == "Hello!"


def test_sqlite_chat_history_max_sessions_limit(temp_db):
    store = SQLiteChatHistoryStore(max_sessions=3, max_msgs_per_session=5)

    # Create 4 sessions
    for i in range(4):
        store[f"session_{i}"] = [{"role": "user", "content": f"msg_{i}"}]

    # Only sessions 1, 2, 3 should exist. session_0 should have been popped (oldest accessed).
    assert "session_0" not in store
    assert "session_1" in store
    assert "session_2" in store
    assert "session_3" in store
    assert len(store) == 3


def test_sqlite_chat_history_max_messages_limit(temp_db):
    store = SQLiteChatHistoryStore(max_sessions=5, max_msgs_per_session=3)

    session_id = "limit_test"
    messages = [
        {"role": "system", "content": "System message"},
        {"role": "user", "content": "Msg 1"},
        {"role": "assistant", "content": "Reply 1"},
        {"role": "user", "content": "Msg 2"},
    ]

    store[session_id] = messages

    loaded = store[session_id]
    # Length should be capped at max_msgs_per_session (3)
    # The system message at index 0 should be preserved, and the last 2 messages (Reply 1, Msg 2) kept
    assert len(loaded) == 3
    assert loaded[0]["role"] == "system"
    assert loaded[1]["content"] == "Reply 1"
    assert loaded[2]["content"] == "Msg 2"


def test_sqlite_chat_history_key_error_on_missing(temp_db):
    store = SQLiteChatHistoryStore()
    with pytest.raises(KeyError):
        _ = store["non_existent"]


def test_sqlite_chat_history_clear_and_len(temp_db):
    store = SQLiteChatHistoryStore()
    store["session_1"] = [{"role": "user", "content": "hi"}]
    store["session_2"] = [{"role": "user", "content": "hello"}]
    assert len(store) == 2

    store.clear()
    assert len(store) == 0
    assert "session_1" not in store


def test_chat_message_content_encrypted_at_rest(temp_db):
    """M-3: stored chat message content must be Fernet-encrypted, not plaintext."""
    import sqlite3

    store = SQLiteChatHistoryStore()
    store["enc_session"] = [{"role": "user", "content": "super secret message"}]

    conn = sqlite3.connect(str(temp_db))
    row = conn.execute(
        "SELECT content FROM chat_messages WHERE session_id = 'enc_session'"
    ).fetchone()
    conn.close()

    assert row is not None
    stored = row[0]
    # Content must be prefixed with the Fernet marker and not be plaintext.
    assert stored.startswith("fernet:")
    assert "super secret message" not in stored
    # Round-trip through the store still returns the plaintext.
    assert store["enc_session"][0]["content"] == "super secret message"


def test_legacy_plaintext_rows_still_readable(temp_db):
    """M-3: pre-encryption plaintext rows remain readable (backward compat)."""
    import sqlite3

    store = SQLiteChatHistoryStore()
    conn = sqlite3.connect(str(temp_db))
    conn.execute("INSERT INTO chat_sessions (session_id, last_accessed) VALUES ('legacy', 1.0)")
    conn.execute(
        "INSERT INTO chat_messages (session_id, role, content, timestamp) "
        "VALUES ('legacy', 'user', 'old plaintext message', 1.0)"
    )
    conn.commit()
    conn.close()

    loaded = store["legacy"]
    assert len(loaded) == 1
    assert loaded[0]["content"] == "old plaintext message"


def test_add_message_encrypts_content(temp_db):
    """M-3: add_message path must also encrypt content at rest."""
    import sqlite3

    store = SQLiteChatHistoryStore()
    store.add_message("app_enc", {"role": "user", "content": "append secret"})

    conn = sqlite3.connect(str(temp_db))
    row = conn.execute("SELECT content FROM chat_messages WHERE session_id = 'app_enc'").fetchone()
    conn.close()

    assert row is not None
    assert row[0].startswith("fernet:")
    assert "append secret" not in row[0]
    assert store["app_enc"][0]["content"] == "append secret"


def test_encrypt_failure_is_fail_closed(temp_db, monkeypatch):
    """Encryption failure must not persist plaintext chat content."""
    import sqlite3

    def _boom(_content: str) -> str:
        raise RuntimeError("no master key")

    monkeypatch.setattr(chat_history_module, "_encrypt_content", _boom)
    store = SQLiteChatHistoryStore()

    try:
        store["fail_closed"] = [{"role": "user", "content": "must not leak"}]
        raised = False
    except RuntimeError:
        raised = True
    assert raised

    conn = sqlite3.connect(str(temp_db))
    row = conn.execute(
        "SELECT content FROM chat_messages WHERE session_id = 'fail_closed'"
    ).fetchone()
    conn.close()
    assert row is None


def test_sqlite_chat_history_move_to_end_and_popitem(temp_db):
    store = SQLiteChatHistoryStore(max_sessions=2)
    store["session_1"] = [{"role": "user", "content": "hi"}]
    store["session_2"] = [{"role": "user", "content": "hello"}]

    # session_1 is oldest, session_2 is newest.
    # Touch session_1 to make it newest.
    store.move_to_end("session_1")

    # Add third session, session_2 should be removed now because it's the oldest.
    store["session_3"] = [{"role": "user", "content": "hey"}]

    assert "session_2" not in store
    assert "session_1" in store
    assert "session_3" in store


def test_sqlite_chat_history_get(temp_db):
    """Test get() method with existing, empty and missing keys."""
    store = SQLiteChatHistoryStore()
    store["session_1"] = [{"role": "user", "content": "hello"}]

    # Existing key
    msgs = store.get("session_1")
    assert msgs is not None
    assert len(msgs) == 1
    assert msgs[0]["content"] == "hello"

    # Missing key with default None
    assert store.get("non_existent") is None

    # Missing key with custom default
    assert store.get("non_existent", []) == []
    assert store.get("non_existent", "default_val") == "default_val"


def test_sqlite_chat_history_delitem(temp_db):
    """Test __delitem__ deletes session and cascades to messages, raising KeyError if missing."""
    import sqlite3

    store = SQLiteChatHistoryStore()
    store["del_session"] = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]
    assert "del_session" in store
    assert len(store) == 1

    # Deleting existing session
    del store["del_session"]
    assert "del_session" not in store
    assert len(store) == 0

    # Ensure message rows are also deleted by foreign-key cascade
    conn = sqlite3.connect(str(temp_db))
    msg_rows = conn.execute(
        "SELECT COUNT(*) FROM chat_messages WHERE session_id = 'del_session'"
    ).fetchone()[0]
    session_rows = conn.execute(
        "SELECT COUNT(*) FROM chat_sessions WHERE session_id = 'del_session'"
    ).fetchone()[0]
    conn.close()
    assert msg_rows == 0
    assert session_rows == 0

    # Deleting non-existent session raises KeyError
    with pytest.raises(KeyError):
        del store["del_session"]


def test_sqlite_chat_history_pop(temp_db):
    """Test pop() returns messages and deletes session, supporting default values."""
    store = SQLiteChatHistoryStore()
    store["pop_session"] = [{"role": "user", "content": "pop me"}]

    # Pop existing session
    popped = store.pop("pop_session")
    assert len(popped) == 1
    assert popped[0]["content"] == "pop me"
    assert "pop_session" not in store

    # Pop missing session with default
    assert store.pop("pop_session", "default_res") == "default_res"
    assert store.pop("pop_session", None) is None
    assert store.pop("pop_session", []) == []

    # Pop missing session without default raises KeyError
    with pytest.raises(KeyError):
        store.pop("pop_session")

    # Extra arguments raise TypeError
    with pytest.raises(TypeError):
        store.pop("pop_session", "default", "extra")


def test_sqlite_chat_history_cross_thread_closing(temp_db):
    """Test cross-thread connection closing operates without ProgrammingError or unclosed warnings."""
    import threading

    store = SQLiteChatHistoryStore()

    def _worker(thread_id: int):
        store[f"thread_sess_{thread_id}"] = [{"role": "user", "content": f"msg {thread_id}"}]

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(store) == 4

    # Close all connections from main thread (cross-thread close)
    store.close_all()
