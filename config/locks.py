"""プロセス間ロック。`config_store` からの純粋な移動。

共有状態 (`_CONFIG_LOCK` / `CONFIG_FILE`) は `config_store` 属性として
参照する (テスト隔離のため束縛を複製しない)。
"""

from __future__ import annotations

import hashlib
import os
import time
from contextlib import contextmanager
from typing import Any

import config_store
from config.paths import _ensure_runtime_dir


@contextmanager
def config_update_lock():
    """Serialize load-modify-save updates across threads and processes."""
    with config_store._CONFIG_LOCK:
        _ensure_runtime_dir()
        update_lock_file = config_store.CONFIG_FILE.with_suffix(
            config_store.CONFIG_FILE.suffix + ".update.lock"
        )
        fd = os.open(str(update_lock_file), os.O_CREAT | os.O_RDWR, 0o600)
        locked = False
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(fd).st_size < 1:
                    try:
                        os.write(fd, b"L")
                        os.lseek(fd, 0, os.SEEK_SET)
                    except OSError:
                        pass
                os.lseek(fd, 0, os.SEEK_SET)
                for attempt in range(20):
                    try:
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                        locked = True
                        break
                    except OSError as err:
                        if attempt == 19:
                            raise RuntimeError("config update lock is busy") from err
                        time.sleep(0.05 * (attempt + 1))
            else:
                import fcntl  # pylint: disable=import-error

                for fl_attempt in range(100):
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
                        locked = True
                        break
                    except OSError as fl_err:
                        import errno as _errno

                        if getattr(fl_err, "errno", None) not in (
                            _errno.EAGAIN,
                            _errno.EWOULDBLOCK,
                        ):
                            raise
                        if fl_attempt == 99:
                            raise RuntimeError("config update lock is busy") from fl_err
                        time.sleep(0.05 * (1 + fl_attempt % 5))
                if not locked:
                    raise RuntimeError("config update lock is busy")
            yield
        finally:
            if locked:
                try:
                    if os.name == "nt":
                        import msvcrt

                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
                    else:
                        import fcntl  # pylint: disable=import-error

                        fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]
                except OSError:
                    pass
            try:
                os.close(fd)
            except OSError:
                pass


@contextmanager
def _master_key_update_lock():
    """Serialize master-key initialization across local processes AND threads.

    On Windows, a kernel mutex serializes the transaction against other
    master-key initializations, and ``config_update_lock()`` is nested inside
    it so the load/generate/save sequence is also mutually exclusive with
    credential/config RMW transactions that only hold ``config_update_lock()``
    (a kernel mutex and a byte-range lock are different mechanisms: without
    the nested acquisition the two transactions can interleave and silently
    lose an update). POSIX already serializes both through the same
    ``config_update_lock()``.

    Callers must preserve the documented ordering — acquire the master-key
    lock BEFORE ``config_update_lock()`` (see
    ``credential_manager.get_or_create_flask_secret_key`` and
    ``services.ai_portfolio_service._get_portfolio_master_key``) so the
    nesting never runs in the reverse direction.
    """
    if os.name != "nt":
        with config_update_lock():
            yield
        return

    import ctypes
    from ctypes import wintypes

    mutex_name = (
        "Local\\MistralNeXStocksMasterKey-"
        + hashlib.sha256(str(config_store.CONFIG_FILE.resolve()).encode("utf-8", errors="ignore")).hexdigest()
    )
    windll_obj: Any = getattr(ctypes, "windll", None)
    kernel32: Any = getattr(windll_obj, "kernel32", None) if windll_obj is not None else None
    if kernel32 is None:
        raise RuntimeError("Windows API (ctypes.windll) is not available on this platform")
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateMutexW(None, False, mutex_name)
    if not handle:
        raise RuntimeError("Failed to create master key initialization mutex")
    acquired = False
    try:
        wait_result = kernel32.WaitForSingleObject(handle, 15_000)
        # WAIT_OBJECT_0 (0) and WAIT_ABANDONED (0x80) both grant ownership.
        if wait_result in (0, 0x80):
            acquired = True
            if wait_result == 0x80:
                config_store.logger.warning(
                    "Master key mutex was abandoned by previous process; acquired ownership"
                )
        else:
            raise RuntimeError(
                f"Timed out waiting for master key initialization (wait_result={wait_result})"
            )
        with config_update_lock():
            yield
    finally:
        if acquired:
            try:
                kernel32.ReleaseMutex(handle)
            except Exception as exc:
                config_store.logger.debug("Failed to release master key mutex: %s", exc)
        try:
            kernel32.CloseHandle(handle)
        except Exception as exc:
            config_store.logger.debug("Failed to close master key mutex handle: %s", exc)
