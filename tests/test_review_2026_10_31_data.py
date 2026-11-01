"""Regression tests for DATA-1..DATA-11 (scratch/review/data.md, 2026-10-31).

Each test covers the original abnormal/edge trigger AND the retained valid
behavior of the applied fix. Isolation rules:
 - config_store.CONFIG_FILE / APP_DATA_DIR are monkeypatched to tmp_path;
   the real %APPDATA% files are never touched.
 - no network; no single sleep longer than 0.5 s; every test < 5 s.

DATA-10 is a doc/comment consistency fix with no behavioral surface — see
scratch/review/data_resolution.md (NO ACTION(test)).
"""

import errno
import inspect
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import config_store
from messaging import MessageAnnouncer

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point config_store at a per-test tmp dir and reset in-process state.

    Also neutralizes the one-time legacy merge: with CONFIG_FILE.parent ==
    APP_DATA_DIR the merge guard would otherwise be eligible to read (and
    unlink) the repo-root config.json during tests.
    """
    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr(config_store, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(config_store, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config_store, "LEGACY_CONFIG_FILE", tmp_path / "legacy_config.json")

    orig_cache_data = config_store._CONFIG_CACHE["data"]
    orig_cache_key = config_store._CONFIG_CACHE["key"]
    orig_corrupted = config_store._CONFIG_CORRUPTED
    orig_merge_done = config_store._LEGACY_MERGE_DONE
    config_store._CONFIG_CACHE["data"] = None
    config_store._CONFIG_CACHE["key"] = None
    config_store._CONFIG_CORRUPTED = False
    config_store._LEGACY_MERGE_DONE = True

    yield cfg_file

    config_store._CONFIG_CACHE["data"] = orig_cache_data
    config_store._CONFIG_CACHE["key"] = orig_cache_key
    config_store._CONFIG_CORRUPTED = orig_corrupted
    config_store._LEGACY_MERGE_DONE = orig_merge_done


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point the chat-history DB at a per-test temp path and re-run init_db."""
    import utils.chat_history as chat_history_module
    from utils.chat_history import SQLiteChatHistoryStore

    db_file = tmp_path / "chat_history.db"
    monkeypatch.setattr(chat_history_module, "DB_PATH", db_file)
    monkeypatch.setattr(chat_history_module, "_db_initialized", False)
    chat_history_module.init_db()
    yield db_file
    SQLiteChatHistoryStore._reset_for_testing()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hold_cross_process_lock(lock_path: Path) -> int:
    """Acquire ``lock_path`` from this thread (first handle in the process)."""
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.fstat(fd).st_size < 1:
            os.write(fd, b"L")
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return fd


def _release_cross_process_lock(fd: int) -> None:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# DATA-1 — corrupt load path must negative-cache (no per-call re-backup/log)
# ---------------------------------------------------------------------------


def test_data1_corrupt_load_caches_defaults_and_backs_up_once(isolated_config, monkeypatch):
    from config import io as config_io

    config_file = isolated_config
    config_file.write_text("{ definitely not json", encoding="utf-8")

    mock_logger = MagicMock()
    monkeypatch.setattr(config_io, "logger", mock_logger)

    # Repeated loads (every credential getter calls load_config per request)
    # must all return defaults from the negative cache.
    results = [config_store.load_config() for _ in range(3)]
    for cfg in results:
        assert cfg == config_store.DEFAULT_CONFIG

    # Exactly ONE sanitizing backup was created by the first (and only) read;
    # subsequent loads must not re-run the backup/rotate step.
    backups = sorted(config_file.parent.glob("config.json.corrupt.*.bak"))
    assert len(backups) == 1, f"expected 1 corrupt backup, found {len(backups)}: {backups}"

    load_warnings = [
        c
        for c in mock_logger.warning.call_args_list
        if c.args and str(c.args[0]).startswith("Failed to load config from")
    ]
    assert len(load_warnings) == 1, "traceback must be logged exactly once, not per call"
    assert load_warnings[0].kwargs.get("exc_info") is True
    backup_warnings = [
        c
        for c in mock_logger.warning.call_args_list
        if c.args and str(c.args[0]).startswith("Corrupted config backed up")
    ]
    assert len(backup_warnings) == 1
    assert config_store.is_config_corrupted() is True

    # Retained: repairing the file invalidates the negative cache (mtime/size
    # change) — the next load returns file content and clears the flag.
    config_file.write_text(
        json.dumps({"mistral_model": "repaired", "api_credentials": {}}),
        encoding="utf-8",
    )
    repaired = config_store.load_config()
    assert repaired["mistral_model"] == "repaired"
    assert config_store.is_config_corrupted() is False


# ---------------------------------------------------------------------------
# DATA-2 — config_update_lock is thread-reentrant; cross-thread exclusion kept
# ---------------------------------------------------------------------------


def test_data2_nested_update_lock_reentrant_same_thread(isolated_config):
    t_outer = time.monotonic()
    with config_store.config_update_lock():
        # Nested acquisition by the owning thread must return immediately;
        # pre-fix it self-conflicted on the file lock (~10 s stall + RuntimeError).
        t_nested = time.monotonic()
        with config_store.config_update_lock():
            pass
        nested_elapsed = time.monotonic() - t_nested
        assert nested_elapsed < 1.0, f"nested re-entry took {nested_elapsed:.3f}s"

        # Master-key nesting (the AB-BA scenario from data.md): get_or_create_master_key()
        # nests _master_key_update_lock -> config_update_lock and runs
        # load_config/save_config while this thread already holds _CONFIG_LOCK.
        t_chain = time.monotonic()
        key1 = config_store.get_or_create_master_key()
        key2 = config_store.get_or_create_master_key()
        cfg = config_store.load_config()
        cfg["mistral_model"] = "nested-update"
        config_store.save_config(cfg)
        chain_elapsed = time.monotonic() - t_chain
    outer_elapsed = time.monotonic() - t_outer

    # Retained: the chain works (no RuntimeError), returns a stable key and
    # the save inside the lock is visible afterwards.
    assert key1, "master key must be produced"
    assert key1 == key2, "both calls must resolve the same master key"
    assert chain_elapsed < 4.0, f"nested master-key chain took {chain_elapsed:.3f}s"
    assert outer_elapsed < 4.0, f"outer update lock took {outer_elapsed:.3f}s"
    assert getattr(config_store._UPDATE_LOCK_STATE, "depth", 0) == 0, "depth must reset"

    t_again = time.monotonic()
    with config_store.config_update_lock():
        pass
    assert time.monotonic() - t_again < 1.0, "lock must be re-acquirable after release"
    assert config_store.load_config()["mistral_model"] == "nested-update"


def test_data2_cross_thread_mutual_exclusion_retained(isolated_config):
    a_held = threading.Event()
    a_errors: list[Exception] = []

    def _hold_for_200ms() -> None:
        try:
            with config_store.config_update_lock():
                a_held.set()
                time.sleep(0.2)
        except Exception as exc:
            a_errors.append(exc)

    holder = threading.Thread(target=_hold_for_200ms, name="data2-holder", daemon=True)
    holder.start()
    assert a_held.wait(timeout=3.0), "holder thread must acquire the update lock"

    # Retained: reentrancy is per-thread — a DIFFERENT thread still blocks
    # while the owner holds, then proceeds after release (bounded, no stall).
    t0 = time.monotonic()
    with config_store.config_update_lock():
        pass
    acquire_elapsed = time.monotonic() - t0

    holder.join(timeout=3.0)
    assert not holder.is_alive()
    assert a_errors == []
    assert acquire_elapsed >= 0.1, "cross-thread acquisition must block while held"
    assert acquire_elapsed < 3.0, "cross-thread acquisition must proceed after release"


# ---------------------------------------------------------------------------
# DATA-3 — non-ASCII Bearer token rejected (401), never TypeError (500)
# ---------------------------------------------------------------------------


def test_data3_non_ascii_bearer_rejected_not_typeerror(monkeypatch):
    import credential_manager

    monkeypatch.setattr(
        credential_manager, "get_or_create_extension_api_token", lambda: "tok-expected-123"
    )

    # Abnormal: attacker-controlled non-ASCII header values used to raise
    # TypeError from secrets.compare_digest -> unhandled 500 instead of 401.
    for header in ("Bearer あいう", "Bearer \U0001f600x", "Bearer \u200b"):
        req = SimpleNamespace(headers={"Authorization": header})
        ok, reason = credential_manager.require_extension_token(req)
        assert ok is False, f"non-ASCII token must be rejected: {header!r}"
        assert reason == "invalid or missing extension token"

    # Retained: strict constant-time comparison for ASCII inputs.
    good = SimpleNamespace(headers={"Authorization": "Bearer tok-expected-123"})
    assert credential_manager.require_extension_token(good) == (True, "")
    bad = SimpleNamespace(headers={"Authorization": "Bearer wrong-token"})
    assert credential_manager.require_extension_token(bad) == (
        False,
        "invalid or missing extension token",
    )
    missing = SimpleNamespace(headers={})
    assert credential_manager.require_extension_token(missing) == (
        False,
        "invalid or missing extension token",
    )


# ---------------------------------------------------------------------------
# DATA-4 — POSIX write lock is bounded (constants + path logic; os.name not faked)
# ---------------------------------------------------------------------------


def test_data4_posix_write_lock_bounded_constants_and_lock_file_path(isolated_config):
    from config import atomic_write

    # Bounded retry budget exists as module-level constants (mirrors the
    # bounded Windows sibling; worst case stays well under save_config's
    # fail-fast expectations).
    assert atomic_write._FCNTL_LOCK_MAX_RETRIES > 0
    assert atomic_write._FCNTL_LOCK_MAX_RETRIES <= 200
    assert 0 < atomic_write._FCNTL_LOCK_RETRY_DELAY_SEC <= 0.5
    worst_case = atomic_write._FCNTL_LOCK_MAX_RETRIES * atomic_write._FCNTL_LOCK_RETRY_DELAY_SEC
    assert worst_case <= 10.0, f"POSIX lock budget {worst_case}s must stay bounded"

    # The POSIX branch must compile and use non-blocking flock with a bounded
    # budget -> RuntimeError, never an unbounded blocking flock(LOCK_EX).
    src = inspect.getsource(atomic_write._write_and_replace_with_fcntl_lock)
    compile(src, "config/atomic_write.py", "exec")
    assert "LOCK_NB" in src, "POSIX flock must be LOCK_EX|LOCK_NB (DATA-4)"
    assert "fcntl.LOCK_EX)" not in src, "unbounded blocking flock is forbidden"
    assert "config write lock busy" in src, "budget exhaustion must fail fast"

    dispatch_src = inspect.getsource(atomic_write._write_and_replace_with_lock)
    assert "_write_and_replace_with_fcntl_lock" in dispatch_src

    # Lock-file path logic: CONFIG_FILE + ".lock" lives next to the config.
    config_file = isolated_config
    expected_lock = config_file.with_suffix(config_file.suffix + ".lock")
    assert expected_lock == config_file.parent / "config.json.lock"

    config_file.write_text(
        json.dumps({"mistral_model": "m", "api_credentials": {}}), encoding="utf-8"
    )
    config_store.save_config({"mistral_model": "m2"})
    assert expected_lock.exists(), "save must acquire the derived .lock file"
    assert json.loads(config_file.read_text(encoding="utf-8"))["mistral_model"] == "m2"


# ---------------------------------------------------------------------------
# DATA-5 — save_config rejects non-dict payloads instead of writing defaults
# ---------------------------------------------------------------------------


def test_data5_save_rejects_non_dict_payloads(isolated_config):
    config_file = isolated_config
    original = json.dumps({"mistral_model": "orig-model", "api_credentials": {}})
    config_file.write_text(original, encoding="utf-8")

    # Abnormal: passing a non-dict used to silently overwrite the user's real
    # config with DEFAULT_CONFIG. Now it is a caller error, file untouched.
    for payload in (None, "x", [1, 2], 42):
        with pytest.raises(TypeError, match="save_config expects dict"):
            config_store.save_config(payload)
        assert config_file.read_text(encoding="utf-8") == original, (
            f"failed save for {payload!r} must leave config.json unchanged"
        )

    # Retained: dict payloads still save (with a .bak rollback point) and an
    # empty dict still writes the defaults (documented, valid).
    config_store.save_config({"mistral_model": "updated"})
    saved = json.loads(config_file.read_text(encoding="utf-8"))
    assert saved["mistral_model"] == "updated"
    bak_path = config_file.with_suffix(config_file.suffix + ".bak")
    bak = json.loads(bak_path.read_text(encoding="utf-8"))
    assert bak["mistral_model"] == "orig-model"

    config_store.save_config({})
    defaults_saved = json.loads(config_file.read_text(encoding="utf-8"))
    assert defaults_saved["mistral_model"] == config_store.DEFAULT_CONFIG["mistral_model"]


# ---------------------------------------------------------------------------
# DATA-6 — .bak keeps mns_master_key, strips other secrets, adds _restore_note
# ---------------------------------------------------------------------------


def test_data6_bak_retains_master_key_and_strips_secrets(isolated_config):
    config_file = isolated_config
    initial = {
        "mistral_model": "m1",
        "api_credentials": {"mistral_api_key": {"scheme": "dpapi", "value": "blob"}},
        "mns_master_key": {"scheme": "keyring", "value": ""},
        "flask_secret_key": "flask-plain-secret",
        "extension_api_token": "ext-plain-token",
    }
    config_file.write_text(json.dumps(initial), encoding="utf-8")
    bak_path = config_file.with_suffix(config_file.suffix + ".bak")

    def _assert_bak(expected_model: str) -> None:
        raw = bak_path.read_text(encoding="utf-8")
        bak = json.loads(raw)  # valid JSON rollback point
        # Retained: the Fernet trust anchor survives — restoring this file
        # must not orphan encrypted data (mns_master_key kept).
        assert bak["mns_master_key"] == {"scheme": "keyring", "value": ""}
        # Stripped secrets stay stripped (intentional sanitization)...
        assert "flask_secret_key" not in bak
        assert "extension_api_token" not in bak
        assert bak["api_credentials"] == {}
        assert "flask-plain-secret" not in raw
        assert "ext-plain-token" not in raw
        # ...and the restore guidance is present (additive, non-secret key).
        note = bak["_restore_note"]
        assert isinstance(note, str)
        assert "mns_master_key" in note and "retained" in note
        assert "flask_secret_key" in note and "extension_api_token" in note
        # Non-secret preferences remain restorable.
        assert bak["mistral_model"] == expected_model

    # Two consecutive saves (data.md: force two save_config calls → parse .bak).
    config_store.save_config(dict(initial, mistral_model="m2"))
    _assert_bak("m1")
    live = json.loads(config_file.read_text(encoding="utf-8"))
    assert live["mns_master_key"] == {"scheme": "keyring", "value": ""}

    config_store.save_config(dict(initial, mistral_model="m3"))
    _assert_bak("m2")


# ---------------------------------------------------------------------------
# DATA-7 — save over a corrupt config fails closed with a salvage backup
# ---------------------------------------------------------------------------


def test_data7_save_over_corrupt_fails_closed_with_salvage(isolated_config):
    config_file = isolated_config
    garbage = "{ definitely-not-json"
    config_file.write_text(garbage, encoding="utf-8")
    # Fresh process: corruption flag is lazy — only load_config used to set it.
    assert config_store.is_config_corrupted() is False

    with pytest.raises(RuntimeError, match="Refusing to save config over corrupted"):
        config_store.save_config({"mistral_model": "x"})

    # The only recoverable copy was preserved byte-for-byte AND salvaged into
    # a sanitized, parseable .corrupt backup before any refusal.
    assert config_file.read_text(encoding="utf-8") == garbage
    corrupt_baks = sorted(config_file.parent.glob("config.json.corrupt.*.bak"))
    assert len(corrupt_baks) == 1, f"expected 1 salvage backup, found {corrupt_baks}"
    salvage = json.loads(corrupt_baks[0].read_text(encoding="utf-8"))
    assert salvage["api_credentials"] == {}
    assert "mns_master_key" not in salvage
    assert "flask_secret_key" not in salvage

    # Fail-closed contract: flag now set — subsequent saves refuse too, and
    # they must not create a second salvage backup.
    assert config_store.is_config_corrupted() is True
    with pytest.raises(RuntimeError, match="Refusing to save config over corrupted"):
        config_store.save_config({"mistral_model": "y"})
    assert len(list(config_file.parent.glob("config.json.corrupt.*.bak"))) == 1

    # Retained: repairing the file self-heals the flag and saves resume.
    config_file.write_text(
        json.dumps({"mistral_model": "ok", "api_credentials": {}}),
        encoding="utf-8",
    )
    assert config_store.is_config_corrupted() is False
    config_store.save_config({"mistral_model": "post-repair"})
    repaired = json.loads(config_file.read_text(encoding="utf-8"))
    assert repaired["mistral_model"] == "post-repair"


# ---------------------------------------------------------------------------
# DATA-8 — readers do not stall behind another thread's cross-process wait
# ---------------------------------------------------------------------------


def test_data8_reader_not_blocked_during_cross_process_lock_wait(isolated_config):
    config_file = isolated_config
    config_file.write_text(
        json.dumps({"mistral_model": "bg-read", "api_credentials": {}}),
        encoding="utf-8",
    )
    config_store._CONFIG_CACHE["data"] = None
    config_store._CONFIG_CACHE["key"] = None

    update_lock = config_file.with_suffix(config_file.suffix + ".update.lock")
    holder_errors: list[Exception] = []
    holder_ready = threading.Event()
    release_hold = threading.Event()

    def _hold() -> None:
        fd = -1
        try:
            fd = _hold_cross_process_lock(update_lock)
            holder_ready.set()
            release_hold.wait(timeout=4.0)
        except Exception as exc:
            holder_errors.append(exc)
        finally:
            if fd >= 0:
                _release_cross_process_lock(fd)

    updater_errors: list[Exception] = []
    updater_acquired = threading.Event()

    def _updater() -> None:
        try:
            with config_store.config_update_lock():
                updater_acquired.set()
        except Exception as exc:
            updater_errors.append(exc)

    holder = threading.Thread(target=_hold, name="data8-holder", daemon=True)
    holder.start()
    assert holder_ready.wait(timeout=3.0), "holder must acquire .update.lock first"

    # The updater now spins in config_update_lock()'s cross-process file-lock
    # wait (it cannot acquire while the holder's handle owns .update.lock).
    updater = threading.Thread(target=_updater, name="data8-updater", daemon=True)
    updater.start()
    time.sleep(0.15)

    # DATA-8: while another thread waits on the cross-process lock, load_config
    # must proceed promptly — pre-fix, _CONFIG_LOCK was held across the whole
    # file-lock wait, stalling every reader for the full hold window.
    t_read = time.monotonic()
    cfg = config_store.load_config()
    read_elapsed = time.monotonic() - t_read

    release_hold.set()
    holder.join(timeout=3.0)
    updater.join(timeout=6.0)

    assert not holder.is_alive()
    assert not updater.is_alive()
    assert holder_errors == []
    assert updater_errors == []
    assert updater_acquired.is_set(), "updater must eventually acquire (bounded wait)"
    assert read_elapsed < 2.0, (
        f"load_config stalled {read_elapsed:.2f}s behind another thread's "
        "cross-process lock wait (DATA-8)"
    )
    assert cfg["mistral_model"] == "bg-read"


# ---------------------------------------------------------------------------
# DATA-9 — announce overflow terminates the listener with a None sentinel
# ---------------------------------------------------------------------------


def test_data9_announce_race_overflow_terminates_listener_with_sentinel():
    """The put_nowait race window must not silently drop an event."""
    ann = MessageAnnouncer()
    q = ann.listen(maxsize=1)
    real_put = q.put_nowait
    state = {"raced": False}

    def racy_put(item):
        # Simulate the race: another broadcaster fills the queue between
        # announce()'s snapshot (qsize 0 < maxsize) and its put_nowait(msg).
        if not state["raced"]:
            state["raced"] = True
            real_put("interloper")
        real_put(item)

    q.put_nowait = racy_put

    ann.announce("payload")

    # The listener must be removed and terminated with the None sentinel so
    # the client reconnects and replays — not kept alive with a hole.
    assert ann.listener_count() == 0
    assert q not in ann.listeners
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    assert items == [None], f"expected exactly the None sentinel, got {items!r}"
    stats = ann.stats()
    assert stats["listeners"] == 0
    assert stats["announced"] == 1
    assert stats["dropped"] == 1


def test_data9_announce_overflow_retained_paths():
    """Retained: normal delivery and the pre-check overflow path still work."""
    ann = MessageAnnouncer()

    # Normal path: delivery works and the listener stays registered.
    q_normal = ann.listen()
    ann.announce("first")
    assert ann.listener_count() == 1
    assert q_normal.get_nowait() == "first"
    assert ann.stats() == {"listeners": 1, "announced": 1, "dropped": 0}

    # Pre-check path: a queue already full at the snapshot is terminated
    # with the sentinel (oldest evicted), never left registered with a hole.
    q_full = ann.listen(maxsize=1)
    q_full.put_nowait("stale")
    ann.announce("second")
    assert ann.listener_count() == 1  # only q_normal remains
    assert q_full not in ann.listeners
    assert q_normal.get_nowait() == "second"  # surviving listener still receives
    items = []
    while not q_full.empty():
        items.append(q_full.get_nowait())
    assert items == [None], f"expected exactly the None sentinel, got {items!r}"
    stats = ann.stats()
    assert stats["listeners"] == 1
    # q_full was removed pre-snapshot; q_normal was still targeted for "second".
    assert stats["announced"] == 2
    assert stats["dropped"] == 1


# ---------------------------------------------------------------------------
# DATA-11 — record_turn trim stays total for max_msgs <= 1 (no -0 slice dup)
# ---------------------------------------------------------------------------


def test_data11_record_turn_degenerate_max_msgs_keeps_first_only(temp_db):
    from utils.chat_history import SQLiteChatHistoryStore

    store = SQLiteChatHistoryStore(max_sessions=5, max_msgs_per_session=100)
    init = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
    ]

    # max_msgs=1: pre-fix, `msgs[-(1 - 1):]` == `msgs[:]` kept everything AND
    # duplicated the first message. Now it collapses to the first message.
    out1 = store.record_turn(
        "s1",
        init_messages=init,
        append={"role": "user", "content": "c"},
        max_msgs=1,
    )
    assert len(out1) == 1
    assert out1[0]["content"] == "sys"
    assert [m["content"] for m in out1].count("sys") == 1, "first message must not duplicate"
    assert store["s1"] == out1

    # max_msgs=0: degenerate budget also stays total — first message only.
    out0 = store.record_turn(
        "s2",
        init_messages=init,
        append={"role": "user", "content": "c"},
        max_msgs=0,
    )
    assert len(out0) == 1
    assert out0[0]["content"] == "sys"
    assert [m["content"] for m in out0].count("sys") == 1


def test_data11_record_turn_retained_normal_trim_and_no_limit(temp_db):
    from utils.chat_history import SQLiteChatHistoryStore

    store = SQLiteChatHistoryStore(max_sessions=5, max_msgs_per_session=100)
    init = [{"role": "system", "content": "sys"}]

    # max_msgs=None: no trimming at all.
    out_none = store.record_turn(
        "n1",
        init_messages=init,
        append={"role": "user", "content": "u"},
        max_msgs=None,
    )
    assert len(out_none) == 2
    assert store["n1"] == out_none

    # max_msgs=4: retained behavior — first message preserved + last 3 kept.
    store.record_turn("n2", init_messages=init, max_msgs=4)
    for i in range(5):
        out = store.record_turn(
            "n2",
            init_messages=init,
            append={"role": "user", "content": f"m{i}"},
            max_msgs=4,
        )
    assert len(out) == 4
    assert out[0]["role"] == "system"
    assert out[-1]["content"] == "m4"
    assert store["n2"] == out


# ---------------------------------------------------------------------------
# Cross-cutting audit finding A-2 (twin of DATA-4): the POSIX user_stocks
# write lock and the native-host startup lock must be bounded
# (LOCK_EX|LOCK_NB + budget + fail-fast), never unbounded blocking
# flock(LOCK_EX).
# ---------------------------------------------------------------------------


def test_a2_storage_posix_write_lock_is_bounded():
    """Source contract: bounded LOCK_NB retries, no unbounded blocking LOCK_EX."""
    from utils import storage

    src = inspect.getsource(storage._write_user_stocks_with_lock)
    compile(src, "utils/storage.py", "exec")
    assert "LOCK_NB" in src, "POSIX flock must be LOCK_EX|LOCK_NB (A-2/DATA-4)"
    assert "fcntl.LOCK_EX)" not in src, "unbounded blocking flock is forbidden"
    assert "user_stocks write lock busy" in src, "budget exhaustion must fail fast"
    assert storage._STORAGE_FCNTL_LOCK_MAX_RETRIES > 0
    assert 0 < storage._STORAGE_FCNTL_LOCK_RETRY_DELAY_SEC <= 0.5


def test_a2_storage_posix_eagain_budget_exhaustion_fails_closed(tmp_path, monkeypatch):
    """Repeated EAGAIN must stop exactly at the (shrinkable) budget and roll
    back via UserStocksPersistError — bounded, fail-closed, not a hang."""
    import builtins

    from utils import storage

    monkeypatch.setattr(storage, "_STORAGE_FCNTL_LOCK_MAX_RETRIES", 3)
    monkeypatch.setattr(storage, "_STORAGE_FCNTL_LOCK_RETRY_DELAY_SEC", 0)

    fake_fcntl = SimpleNamespace(
        LOCK_EX=1,
        LOCK_UN=2,
        LOCK_NB=8,
        flock=MagicMock(side_effect=BlockingIOError(errno.EWOULDBLOCK, "busy")),
    )
    real_import = builtins.__import__

    def import_without_lock(name, *args, **kwargs):
        if name == "fcntl":
            return fake_fcntl
        return real_import(name, *args, **kwargs)

    target = tmp_path / "user_stocks.json"
    target.write_text('{"old": true}', encoding="utf-8")
    tmp_file = target.with_suffix(".testtmp")
    lock_file = target.with_suffix(".lock")

    with patch.object(storage.os, "name", "posix"):
        with patch("builtins.__import__", side_effect=import_without_lock):
            with pytest.raises(storage.UserStocksPersistError) as excinfo:
                storage._write_user_stocks_with_lock(
                    '{"new": true}', tmp_file, target, lock_file
                )

    cause = excinfo.value.__cause__
    assert isinstance(cause, BlockingIOError)
    assert "user_stocks write lock busy" in str(cause)
    # 3 bounded acquire attempts + 1 LOCK_UN attempt in the finally block —
    # proves the loop stops at the budget instead of retrying forever.
    assert fake_fcntl.flock.call_count == 4
    # Fail-closed: the old file must remain untouched.
    assert target.read_text(encoding="utf-8") == '{"old": true}'
    assert not tmp_file.exists()


def test_a2_startup_lock_posix_branch_is_bounded():
    """Source contract for the native-host startup lock (same DATA-4 twin)."""
    from native_host import start_backend as sb

    src = inspect.getsource(sb._startup_lock)
    compile(src, "native_host/start_backend.py", "exec")
    assert "LOCK_NB" in src, "startup flock must be LOCK_EX|LOCK_NB (A-2)"
    assert "fcntl.LOCK_EX)" not in src, "unbounded blocking flock is forbidden"
    assert "startup lock busy" in src, "budget exhaustion must raise OSError"
    assert sb._STARTUP_LOCK_MAX_RETRIES > 0
    assert 0 < sb._STARTUP_LOCK_RETRY_DELAY_SEC <= 0.5


def test_a2_startup_lock_posix_budget_exhaustion_raises_oserror(tmp_path, monkeypatch):
    """Budget exhaustion surfaces as OSError (the same failure class the
    Windows LK_LOCK timeout already produces and native_host already handles)."""
    import builtins

    from native_host import start_backend as sb

    monkeypatch.setattr(sb, "STARTUP_LOCK_FILE", tmp_path / ".backend.start.lock")
    monkeypatch.setattr(sb, "_STARTUP_LOCK_MAX_RETRIES", 2)
    monkeypatch.setattr(sb, "_STARTUP_LOCK_RETRY_DELAY_SEC", 0)

    fake_fcntl = SimpleNamespace(
        LOCK_EX=1,
        LOCK_UN=2,
        LOCK_NB=8,
        flock=MagicMock(side_effect=BlockingIOError(errno.EWOULDBLOCK, "busy")),
    )
    real_import = builtins.__import__

    def import_without_lock(name, *args, **kwargs):
        if name == "fcntl":
            return fake_fcntl
        return real_import(name, *args, **kwargs)

    with patch.object(os, "name", "posix"):
        with patch("builtins.__import__", side_effect=import_without_lock):
            with pytest.raises(OSError, match="startup lock busy"):
                with sb._startup_lock():
                    raise AssertionError("body must not run without the lock")

    assert fake_fcntl.flock.call_count == 2  # bounded: 2 acquire attempts, no yield

