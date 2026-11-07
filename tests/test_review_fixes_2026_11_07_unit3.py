"""Regression tests for the 2026-11-07 exhaustive review fixes (unit 3).

Data-layer fixes pinned here:

- H2: the portfolio persist-failure rollback compared against a live aliased
  object, so a concurrent same-symbol request that popped/mutated/re-added
  the very same dict was erased by the rollback while its (already persisted)
  write survived on disk — silent data loss.
- H3: ``load_user_stocks(force=True)`` (bg sync, every cycle) published the
  on-disk state over in-memory mutations while ``save_user_stocks()``
  deliberately releases ``user_stocks_lock`` during disk I/O; a queued second
  save then persisted the reverted state, losing a mutation that already
  returned 200.
- M16: both negative/initial config cache entries were keyed by a path stat
  taken AFTER the write/backup I/O, so a cross-process replacement inside the
  handler keyed defaults under the valid file's stat (credentials invisible
  until the next file change; corruption flag never self-healed).
- M17: a NaN/Infinity ``extension_api_token_created`` disabled BOTH the token
  rotation and the self-heal backfill, so a leaked extension token never
  rotated.
- M18: ``DiskCacheLockTimeout`` was only raised for lock timeouts; a plain
  OSError from opening ``.cache.lock`` (read-only FS, AV lock) propagated out
  of ``has/delete/clear/cleanup/stats/set`` and 500'd e.g. /api/metrics.
- M26: a deeply nested (but parseable) config JSON raised RecursionError in
  ``copy.deepcopy``, escaping the corruption exception tuple — every config
  read 500'd with no corrupt backup; ``unprotect_data`` raised AttributeError
  for a truthy non-string fernet value instead of returning "".
- L3: tmp files leaked by hard process kills between the fsync'd tmp write
  and ``os.replace`` were never swept by any cleanup (globbing ``*.json``
  only), accumulating across crashes.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import config_store
import config_utils
import credential_manager
import crypto_utils
from app import create_app
from app_state import app_state
from utils import storage
from utils.disk_cache import StockDiskCache
from utils.storage import UserStocksPersistError

ORIGIN = {"Origin": "http://localhost:5000"}


@pytest.fixture(scope="module")
def app():
    application = create_app()
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def _restore_user_stocks():
    with app_state.market.user_stocks_lock:
        us = dict(app_state.market.user_us)
        jp = dict(app_state.market.user_jp)
        idx = dict(app_state.market.user_idx)
        pending = app_state.market.user_stocks_save_pending
    yield
    with app_state.market.user_stocks_lock:
        (
            app_state.market.user_us,
            app_state.market.user_jp,
            app_state.market.user_idx,
        ) = us, jp, idx
        app_state.market.user_stocks_save_pending = pending


class _ConfigPaths:
    """Isolate config_store paths + cache for one test."""

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.config_file = tmp_path / "config.json"

    def __enter__(self):
        self._patches = [
            patch.object(config_store, "CONFIG_FILE", self.config_file),
            patch.object(config_utils, "CONFIG_FILE", self.config_file),
            patch.object(config_store, "APP_DATA_DIR", self.tmp_path),
            patch.object(config_store, "_LEGACY_MERGE_DONE", True),
        ]
        for p in self._patches:
            p.start()
        config_store._CONFIG_CACHE["data"] = None
        config_store._CONFIG_CACHE["key"] = None
        config_store._CONFIG_CORRUPTED = False
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        config_store._CONFIG_CACHE["data"] = None
        config_store._CONFIG_CACHE["key"] = None
        config_store._CONFIG_CORRUPTED = False
        return False


# ---------------------------------------------------------------------------
# H2: rollback vs aliased in-place concurrent update
# ---------------------------------------------------------------------------


def test_portfolio_rollback_preserves_aliased_inplace_concurrent_update(client):
    """Rollback must not erase a concurrent update that mutated OUR object.

    The route pops the stored object, mutates it in place, and re-adds it; a
    concurrent same-symbol request pops/re-adds the very same object, so
    ``container.get(symbol) == val`` compared positively while the container
    actually held the *concurrent* (already persisted) contents. The frozen
    snapshot distinguishes them; identity-based content comparison could not.
    """
    symbol = "ZZU3H2A"
    original = {"name": "Port Co", "shares": 5, "avg_price": 10.0}
    with app_state.market.user_stocks_lock:
        app_state.market.user_us[symbol] = dict(original)

    def failing_save_with_aliasing_concurrent_update() -> None:
        # Mirror the ROUTE'S OWN concurrent flow for the same symbol:
        # pop our object, mutate in place, re-add, persist, then fail ours.
        with app_state.market.user_stocks_lock:
            live = app_state.market.user_us.pop(symbol)
            assert isinstance(live, dict)
            live["shares"] = 999
            live["avg_price"] = 1.0
            app_state.market.user_us[symbol] = live
        raise UserStocksPersistError("disk full")

    with patch(
        "routes.stocks.portfolio.save_user_stocks",
        side_effect=failing_save_with_aliasing_concurrent_update,
    ):
        resp = client.post(
            "/api/stocks/portfolio",
            json={"market": "us", "symbol": symbol, "shares": 10, "avg_price": 2.0},
            headers=ORIGIN,
        )
    assert resp.status_code == 503
    with app_state.market.user_stocks_lock:
        pushed = app_state.market.user_us.get(symbol)
    assert pushed == {"name": "Port Co", "shares": 999, "avg_price": 1.0}


def test_portfolio_rollback_restores_previous_value_on_failure(client):
    """The snapshot fix keeps the original contract (same as the pinned API-7
    test): with no concurrent writer, a failed persist rolls this request's
    update back to the previous stored value."""
    symbol = "ZZU3H2B"
    previous = {"name": "B", "shares": 1, "avg_price": 1.0}
    with app_state.market.user_stocks_lock:
        app_state.market.user_us[symbol] = dict(previous)
    with patch(
        "routes.stocks.portfolio.save_user_stocks",
        side_effect=UserStocksPersistError("disk full"),
    ):
        resp = client.post(
            "/api/stocks/portfolio",
            json={"market": "us", "symbol": symbol, "shares": 3, "avg_price": 3.0},
            headers=ORIGIN,
        )
    assert resp.status_code == 503
    with app_state.market.user_stocks_lock:
        assert app_state.market.user_us.get(symbol) == previous


# ---------------------------------------------------------------------------
# H3: save-in-flight guard keeps force-loads from publishing stale disk state
# ---------------------------------------------------------------------------


def test_force_load_skips_publish_while_save_is_in_flight(client, tmp_path, monkeypatch):
    """A blocked save must not have its memory state reverted by a sync load."""
    user_file = tmp_path / "user_stocks.json"
    monkeypatch.setattr(storage, "USER_STOCKS_FILE", str(user_file))
    with app_state.market.user_stocks_lock:
        app_state.market.user_us["ZZU3H3"] = {"name": "H3", "shares": 1, "avg_price": 1.0}

    # Seed the disk with the symbol still present.
    storage.save_user_stocks()
    assert user_file.exists()

    real_writer = storage._write_user_stocks_with_lock
    entered = threading.Event()
    release = threading.Event()
    delegated_result: list[bool] = []

    def blocking_writer(encoded_data, tmp_file, final_path, lock_file):
        entered.set()
        assert release.wait(timeout=30)
        real_writer(encoded_data, tmp_file, final_path, lock_file)
        delegated_result.append(True)

    with patch.object(storage, "_write_user_stocks_with_lock", side_effect=blocking_writer):
        response: dict = {}

        def do_delete():
            with client:
                response["resp"] = client.post(
                    "/api/stocks/delete",
                    json={"symbol": "ZZU3H3", "market": "us"},
                    headers=ORIGIN,
                )

        worker = threading.Thread(target=do_delete)
        worker.start()
        assert entered.wait(timeout=30)

        # The save is mid-flight: memory no longer has the symbol (route
        # popped it) but the disk write has not happened yet. A force load
        # must NOT publish the stale disk state back.
        assert app_state.market.user_stocks_save_pending >= 1
        storage.load_user_stocks(force=True)
        with app_state.market.user_stocks_lock:
            assert "ZZU3H3" not in app_state.market.user_us

        release.set()
        worker.join(timeout=30)
        assert not worker.is_alive()

    assert response["resp"].status_code == 200
    # The write completed: both memory and disk lack the symbol; pending reset.
    with app_state.market.user_stocks_lock:
        assert "ZZU3H3" not in app_state.market.user_us
    assert user_file.exists()
    assert delegated_result == [True]
    assert app_state.market.user_stocks_save_pending == 0


# ---------------------------------------------------------------------------
# M16: config cache keys must describe the content they cache
# ---------------------------------------------------------------------------


def test_default_create_cache_key_is_the_written_stat(tmp_path):
    """The initial-defaults cache entry must follow OUR write, and a
    cross-process replacement after it must invalidate the entry."""
    with _ConfigPaths(tmp_path):
        assert not config_store.CONFIG_FILE.exists()
        cfg = config_store.load_config()
        assert config_store.CONFIG_FILE.exists()
        assert cfg["mistral_model"] == config_store.DEFAULT_CONFIG["mistral_model"]

        # Simulate another process atomically replacing the config (bypasses
        # this process's locks, exactly like the native-host token rotation).
        other = {k: v for k, v in config_store.DEFAULT_CONFIG.items()}
        other["mistral_model"] = "other-process-model"
        other_tmp = tmp_path / "other.tmp"
        other_tmp.write_text(json.dumps(other, ensure_ascii=False), encoding="utf-8")
        os.replace(other_tmp, config_store.CONFIG_FILE)

        # Cache must MISS (stat changed) and serve the peer's write.
        cfg2 = config_store.load_config()
        assert cfg2["mistral_model"] == "other-process-model"


def test_corrupt_negative_cache_key_survives_concurrent_repair(tmp_path):
    """Defaults cached during corrupt handling must be keyed to the corrupt
    stat (captured pre-read), not to a file another process swapped in during
    the backup I/O."""
    with _ConfigPaths(tmp_path):
        config_store.CONFIG_FILE.write_text("{not-valid-json", encoding="utf-8")
        pre_backup_stat = config_store.CONFIG_FILE.stat()

        def sanitize_that_also_simulates_peer_replace(source, dest):
            from config.corruption import _sanitize_and_backup_corrupt_config as _real

            _real(source, dest)
            other = {k: v for k, v in config_store.DEFAULT_CONFIG.items()}
            other["mistral_model"] = "peer-repaired-model"
            other_tmp = tmp_path / "peer.tmp"
            other_tmp.write_text(json.dumps(other, ensure_ascii=False), encoding="utf-8")
            os.replace(other_tmp, config_store.CONFIG_FILE)

        with patch(
            "config.io._sanitize_and_backup_corrupt_config",
            side_effect=sanitize_that_also_simulates_peer_replace,
        ):
            _first = config_store.load_config()
        assert config_store._CONFIG_CORRUPTED is True
        assert list((tmp_path).glob("*.corrupt.*.bak"))

        # The negative cache must be keyed to the CORRUPT stat, so the repair
        # (a different stat) forces a re-read instead of masking itself.
        cached_key = config_store._CONFIG_CACHE["key"]
        assert cached_key in (
            (pre_backup_stat.st_mtime_ns, pre_backup_stat.st_size),
            "missing",
        )
        cfg = config_store.load_config()
        assert cfg["mistral_model"] == "peer-repaired-model"
        assert config_store._CONFIG_CORRUPTED is False


def test_save_config_returns_written_stat_key(tmp_path):
    """save_config surfaces the lock-held post-replace stat (M16 plumbing)."""
    with _ConfigPaths(tmp_path):
        returned = config_store.save_config(dict(config_store.DEFAULT_CONFIG))
        assert returned is not None
        st = config_store.CONFIG_FILE.stat()
        assert returned == (st.st_mtime_ns, st.st_size)


# ---------------------------------------------------------------------------
# M17: NaN/Infinity token timestamps must heal, not disable rotation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf")])
def test_extension_token_nonfinite_created_is_backfilled(tmp_path, monkeypatch, bad_value):
    from cryptography.fernet import Fernet

    raw_key = Fernet.generate_key().decode()
    token = "T" * 40
    entry = crypto_utils.protect_data(token, "extension_api_token", master_key=raw_key)

    with _ConfigPaths(tmp_path):
        config_store.CONFIG_FILE.write_text(
            json.dumps(
                {
                    "extension_api_token": entry,
                    "extension_api_token_created": bad_value,
                }
            ),
            encoding="utf-8",
        )
        with patch.object(config_store, "get_or_create_master_key", return_value=raw_key):
            returned = credential_manager.get_or_create_extension_api_token()
        assert returned == token

        reloaded = json.loads(config_store.CONFIG_FILE.read_text(encoding="utf-8"))
        created = reloaded.get("extension_api_token_created")
        assert created is not None
        assert math.isfinite(float(created))
        assert float(created) > 0
        assert config_store._CONFIG_CORRUPTED is False


# ---------------------------------------------------------------------------
# M18: lock-acquisition failures must degrade like a lock timeout
# ---------------------------------------------------------------------------


def test_disk_cache_degrades_on_lock_open_failure(tmp_path):
    cache = StockDiskCache(tmp_path / "cache", max_entries=8, default_ttl=30)
    cache.set("seed", {"v": 1})  # ensure dir + entry exist
    assert cache.has("seed") is True

    with patch("utils.disk_cache.os.open", side_effect=PermissionError(13, "denied")):
        assert cache.has("seed") is False
        assert cache.delete("seed") is False
        assert cache.cleanup() == 0
        stats = cache.stats()
        assert isinstance(stats, dict)
        assert stats.get("disk_cache_entries", 0) == 0
        cache.set("seed2", {"v": 2})  # must not raise

    # After the failure clears, the cache operates normally.
    assert cache.has("seed") is True


# ---------------------------------------------------------------------------
# M26: parseable-but-degenerate configs and malformed crypto entries
# ---------------------------------------------------------------------------


def test_deeply_nested_config_degrades_to_defaults_with_backup(tmp_path):
    with _ConfigPaths(tmp_path):
        # A structure json.load can parse (C decoder) but copy.deepcopy cannot
        # (Python recursion limit) — ~20k nesting levels.
        config_store.CONFIG_FILE.write_text(
            '{"deep":' + '{"k":' * 20000 + '0' + '}' * 20000, encoding="utf-8"
        )
        cfg = config_store.load_config()
        # Falls back to defaults with a corrupt backup; never raises.
        assert cfg["mistral_model"] == config_store.DEFAULT_CONFIG["mistral_model"]
        assert list((tmp_path).glob("*.corrupt.*.bak"))


def test_unprotect_data_returns_empty_for_truthy_non_string_value():
    assert crypto_utils.unprotect_data({"scheme": "fernet", "value": 123}) == ""
    assert crypto_utils.unprotect_data({"scheme": "fernet", "value": True}) == ""


# ---------------------------------------------------------------------------
# L3: leaked tmp files are eventually swept
# ---------------------------------------------------------------------------


def test_disk_cache_sweeps_stale_tmp_files(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    cache = StockDiskCache(cache_dir, max_entries=8, default_ttl=30)
    old_tmp = cache_dir / "entry.OLDHEX.tmp"
    old_tmp.write_text("x", encoding="utf-8")
    fresh_tmp = cache_dir / "entry.FRESHHEX.tmp"
    fresh_tmp.write_text("y", encoding="utf-8")
    past = time.time() - 700.0
    os.utime(old_tmp, (past, past))

    removed = cache._remove_stale_tmp_files(time.time())
    assert removed == 1
    assert not old_tmp.exists()
    assert fresh_tmp.exists()


def test_storage_sweeps_stale_user_stocks_tmp_files(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "USER_STOCKS_FILE", str(tmp_path / "user_stocks.json"))
    old_tmp = tmp_path / "user_stocks.deadbeef.tmp"
    old_tmp.write_text("x", encoding="utf-8")
    fresh_tmp = tmp_path / "user_stocks.cafef00d.tmp"
    fresh_tmp.write_text("y", encoding="utf-8")
    past = time.time() - 700.0
    os.utime(old_tmp, (past, past))

    storage._remove_stale_user_stocks_tmp_files()
    assert not old_tmp.exists()
    assert fresh_tmp.exists()
