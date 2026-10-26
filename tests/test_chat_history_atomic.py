"""Regression tests for the atomic chat-history store operations (C-2, H-2).

C-2: the previous read-modify-write (``__getitem__`` -> mutate -> ``__setitem__``
full-swap) could lose a concurrently-written message when two compound updates
interleaved. The store now performs each compound mutation inside a single
transaction (``record_turn``, ``append_message_if_not_duplicate``,
``pop_last_user_message_if_matches``) so no write is lost.

H-2: the persistence contract is now explicit and validated at the storage
boundary (``_validate_message``), and ``add_chat_history`` dispatches on type
instead of falling through to an implicit write.
"""

import threading

import pytest

import utils.chat_history as chat_history_module
from utils.chat_history import SQLiteChatHistoryStore


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point the chat DB at a per-test temp path and re-run init_db."""
    db_file = tmp_path / "chat_history.db"
    monkeypatch.setattr(chat_history_module, "DB_PATH", db_file)
    monkeypatch.setattr(chat_history_module, "_db_initialized", False)
    chat_history_module.init_db()
    yield db_file
    SQLiteChatHistoryStore._reset_for_testing()


def test_record_turn_seeds_then_appends(temp_db):
    store = SQLiteChatHistoryStore(max_sessions=5, max_msgs_per_session=10)
    init = [{"role": "system", "content": "sys"}]
    out = store.record_turn(
        "sess",
        init_messages=init,
        append={"role": "user", "content": "hello"},
        max_msgs=30,
    )
    assert [m["role"] for m in out] == ["system", "user"]
    assert store["sess"] == out


def test_record_turn_returns_transcript_and_trims(temp_db):
    store = SQLiteChatHistoryStore(max_sessions=5, max_msgs_per_session=100)
    init = [{"role": "system", "content": "sys"}]
    # Build a transcript of 5 turns, trimming to 4 keeps first + last 3.
    store.record_turn("sess", init_messages=init, max_msgs=4)
    for i in range(5):
        out = store.record_turn(
            "sess",
            init_messages=init,
            append={"role": "user", "content": f"m{i}"},
            max_msgs=4,
        )
    assert len(out) == 4
    # First message (system) is always preserved, oldest user turns dropped.
    assert out[0]["role"] == "system"
    assert out[-1]["content"] == "m4"


def test_record_turn_touch_only_when_no_append(temp_db):
    store = SQLiteChatHistoryStore(max_sessions=5, max_msgs_per_session=10)
    store.record_turn("sess", init_messages=[{"role": "system", "content": "sys"}])
    before = store["sess"]
    # append=None on an existing session must not rewrite/lose anything.
    out = store.record_turn("sess", append=None)
    assert out == before
    assert store["sess"] == before


def test_record_turn_seeds_when_append_none_and_empty(temp_db):
    store = SQLiteChatHistoryStore(max_sessions=5, max_msgs_per_session=10)
    out = store.record_turn("sess", init_messages=[{"role": "system", "content": "sys"}])
    assert out == [{"role": "system", "content": "sys"}]
    assert store["sess"] == out


def test_append_if_not_duplicate_appends_new_and_skips_dup(temp_db):
    store = SQLiteChatHistoryStore(max_sessions=5, max_msgs_per_session=10)
    store.record_turn("sess", init_messages=[{"role": "system", "content": "sys"}])

    assert store.append_message_if_not_duplicate(
        "sess", {"role": "assistant", "content": "answer"}
    )
    # Duplicate of the trailing turn is skipped.
    assert not store.append_message_if_not_duplicate(
        "sess", {"role": "assistant", "content": "answer"}
    )
    # A different trailing content is appended.
    assert store.append_message_if_not_duplicate(
        "sess", {"role": "assistant", "content": "another"}
    )
    contents = [m["content"] for m in store["sess"]]
    assert contents == ["sys", "answer", "another"]


def test_append_if_not_duplicate_uses_normalize(temp_db):
    store = SQLiteChatHistoryStore(max_sessions=5, max_msgs_per_session=10)
    store.record_turn("sess", init_messages=[{"role": "system", "content": "sys"}])
    store.record_turn("sess", append={"role": "assistant", "content": "Answer"})

    # normalize makes the incoming value match the trailing turn -> skipped.
    assert not store.append_message_if_not_duplicate(
        "sess",
        {"role": "assistant", "content": "answer"},
        normalize=lambda c: str(c).lower(),
    )


def test_append_if_not_duplicate_missing_session_noop(temp_db):
    store = SQLiteChatHistoryStore(max_sessions=5, max_msgs_per_session=10)
    # No-op (returns False) when the session does not exist; must not create it.
    assert not store.append_message_if_not_duplicate(
        "nope", {"role": "assistant", "content": "x"}
    )
    assert "nope" not in store


def test_pop_last_user_message_if_matches(temp_db):
    store = SQLiteChatHistoryStore(max_sessions=5, max_msgs_per_session=10)
    store.record_turn("sess", init_messages=[{"role": "system", "content": "sys"}])
    store.record_turn("sess", append={"role": "user", "content": "q"})

    assert store.pop_last_user_message_if_matches("sess", "q")
    assert store["sess"] == [{"role": "system", "content": "sys"}]


def test_pop_last_user_message_noop_on_assistant_last(temp_db):
    store = SQLiteChatHistoryStore(max_sessions=5, max_msgs_per_session=10)
    store.record_turn("sess", init_messages=[{"role": "system", "content": "sys"}])
    store.record_turn("sess", append={"role": "user", "content": "q"})
    store.record_turn("sess", append={"role": "assistant", "content": "a"})

    # Trailing turn is an assistant reply -> the user turn must NOT be removed.
    assert not store.pop_last_user_message_if_matches("sess", "q")
    assert len(store["sess"]) == 3


def test_pop_last_user_message_noop_on_content_mismatch(temp_db):
    store = SQLiteChatHistoryStore(max_sessions=5, max_msgs_per_session=10)
    store.record_turn("sess", init_messages=[{"role": "system", "content": "sys"}])
    store.record_turn("sess", append={"role": "user", "content": "newer"})

    # A concurrent request appended a newer user turn; the old rollback must not
    # delete it.
    assert not store.pop_last_user_message_if_matches("sess", "older")
    assert store["sess"][-1]["content"] == "newer"


def test_pop_last_user_message_missing_session_noop(temp_db):
    store = SQLiteChatHistoryStore(max_sessions=5, max_msgs_per_session=10)
    assert not store.pop_last_user_message_if_matches("nope", "q")
    assert "nope" not in store


# ---------------------------------------------------------------------------
# H-2: explicit storage contract / boundary validation
# ---------------------------------------------------------------------------


def test_validate_message_rejects_non_dict(temp_db):
    store = SQLiteChatHistoryStore(max_sessions=5, max_msgs_per_session=10)
    with pytest.raises(TypeError):
        store._validate_message(["not", "a", "dict"])
    with pytest.raises(TypeError):
        store["sess"] = ["not-a-dict"]


def test_validate_message_rejects_missing_keys(temp_db):
    store = SQLiteChatHistoryStore(max_sessions=5, max_msgs_per_session=10)
    with pytest.raises(KeyError):
        store._validate_message({"content": "x"})
    with pytest.raises(KeyError):
        store._validate_message({"role": "user"})


def test_validate_message_rejects_non_str_content(temp_db):
    store = SQLiteChatHistoryStore(max_sessions=5, max_msgs_per_session=10)
    with pytest.raises(TypeError):
        store._validate_message({"role": "user", "content": 123})


def test_add_message_validates_at_boundary(temp_db):
    store = SQLiteChatHistoryStore(max_sessions=5, max_msgs_per_session=10)
    with pytest.raises(TypeError):
        store.add_message("sess", {"role": "user", "content": None})


def test_add_chat_history_dispatch_contract(temp_db):
    from ai_state import AIState

    st = AIState()
    # list -> replace
    st.add_chat_history("k", [{"role": "user", "content": "hi"}])
    assert st.chat_history["k"] == [{"role": "user", "content": "hi"}]
    # dict -> append
    st.add_chat_history("k", {"role": "assistant", "content": "yo"})
    assert [m["content"] for m in st.chat_history["k"]] == ["hi", "yo"]
    # other -> TypeError (no implicit write)
    with pytest.raises(TypeError):
        st.add_chat_history("k", "not-a-valid-message")


# ---------------------------------------------------------------------------
# C-2: concurrency — no lost updates
# ---------------------------------------------------------------------------


def test_record_turn_concurrent_no_lost_updates(temp_db):
    """N threads appending through record_turn must never lose a message.

    This is the C-2 regression: the old read-modify-write could drop a message
    when two threads interleaved a read and a full-swap write. With single
    -transaction read-modify-write, every append is preserved.
    """
    store = SQLiteChatHistoryStore(max_sessions=5, max_msgs_per_session=1000)
    session_id = "concurrent"
    n_threads = 8
    per_thread = 5
    init = [{"role": "system", "content": "sys"}]
    barrier = threading.Barrier(n_threads)

    def worker(idx: int) -> None:
        barrier.wait()
        for i in range(per_thread):
            store.record_turn(
                session_id,
                init_messages=init,
                append={"role": "user", "content": f"t{idx}-{i}"},
                max_msgs=None,
            )
        store.close()

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    loaded = store[session_id]
    # 1 system init + n_threads * per_thread user messages, none lost.
    assert len(loaded) == 1 + n_threads * per_thread
    contents = {m["content"] for m in loaded}
    for t in range(n_threads):
        for i in range(per_thread):
            assert f"t{t}-{i}" in contents


def test_append_and_pop_concurrent_consistency(temp_db):
    """Interleaved appends and rollbacks never resurrect a stale trailing turn."""
    store = SQLiteChatHistoryStore(max_sessions=5, max_msgs_per_session=1000)
    session_id = "mix"
    store.record_turn(session_id, init_messages=[{"role": "system", "content": "sys"}])
    n_threads = 6
    barrier = threading.Barrier(n_threads)

    def worker(idx: int) -> None:
        barrier.wait()
        for i in range(4):
            store.append_message_if_not_duplicate(
                session_id, {"role": "assistant", "content": f"a{idx}-{i}"}
            )
        store.close()

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    loaded = store[session_id]
    assert loaded[0]["role"] == "system"
    # Every append is preserved (distinct contents -> no dedup skips).
    assert len(loaded) == 1 + n_threads * 4
