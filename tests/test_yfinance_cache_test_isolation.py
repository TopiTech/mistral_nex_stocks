"""Regression tests for the yfinance SQLite cache leak in the test environment.

Root cause (R-20260817-1): production replaces yfinance's SQLite-backed peewee
caches (timezone ``_TZ_KV``, cookie DB, ISIN DB) with in-memory drop-ins inside
``bootstrap()`` → ``app_state.initialize_yfinance_cache()`` *before* any yfinance
use. The test suite skips bootstrap (``MNS_SKIP_BOOTSTRAP=1``), so tests that
exercise real yfinance paths (index history fetches, ``yf.Ticker`` construction)
opened yfinance's module-level SQLite connections, which were never closed and
emitted ``ResourceWarning: unclosed database`` at GC.

tests/conftest.py now mirrors the production cache isolation at import time.
These tests pin that contract so removing the conftest patch regresses loudly.
"""

import sqlite3

import yfinance.cache as yfc

from app_state import _InMemoryCookieCache, _InMemoryYfCache


def test_tz_cache_is_in_memory_in_test_env():
    """The yfinance timezone cache must be the in-memory drop-in, never the
    SQLite-backed peewee implementation used in production bypasses."""
    tz_cache = yfc._TzCacheManager._tz_cache
    assert isinstance(tz_cache, _InMemoryYfCache)
    # It must behave like a cache (store + lookup round-trip) and not hold a
    # sqlite3 connection.
    tz_cache.store("test_tz", "America/New_York")
    assert tz_cache.lookup("test_tz") == "America/New_York"


def test_cookie_cache_is_in_memory_in_test_env():
    """The yfinance cookie cache must be the in-memory drop-in with the same
    lookup contract the real cookie cache exposes (``{"cookie": ..., "age": ...}``)."""
    cookie_cache = yfc._CookieCacheManager._Cookie_cache
    assert isinstance(cookie_cache, _InMemoryCookieCache)
    cookie_cache.store("test_cookie", "A3-cookie-value")
    looked = cookie_cache.lookup("test_cookie")
    assert isinstance(looked, dict)
    assert looked.get("cookie") == "A3-cookie-value"


def test_isin_cache_is_in_memory_in_test_env():
    """The yfinance ISIN cache must be the in-memory drop-in."""
    isin_cache = yfc._ISINCacheManager._isin_cache
    assert isinstance(isin_cache, _InMemoryYfCache)


def test_yfinance_cache_lookup_opens_no_sqlite_connection():
    """A yfinance TZ-cache lookup must not open any sqlite3 connection.

    Regression guard for the ResourceWarning seen on full-suite runs: the real
    peewee-backed TZ cache lazily opens a SQLite database on first lookup and
    never closes it. With the in-memory isolation applied by conftest, a lookup
    must leave the process with no new open sqlite3.Connection objects.
    """
    tz_cache = yfc._TzCacheManager._tz_cache
    assert isinstance(tz_cache, _InMemoryYfCache)

    before = _count_open_sqlite_connections()
    tz_cache.lookup("__probe_no_sqlite__")
    after = _count_open_sqlite_connections()
    assert after == before, "yfinance TZ cache lookup opened a sqlite3 connection"


def _count_open_sqlite_connections() -> int:
    import gc

    gc.collect()
    count = 0
    for obj in gc.get_objects():
        if isinstance(obj, sqlite3.Connection):
            try:
                obj.execute("SELECT 1")
                count += 1
            except sqlite3.ProgrammingError:
                pass  # closed connection
    return count
