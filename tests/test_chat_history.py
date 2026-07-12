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
