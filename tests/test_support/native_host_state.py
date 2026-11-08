"""Helpers for isolating native-host process-global state in tests."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


@contextmanager
def isolated_native_host_rate_limit(nh_module: Any) -> Iterator[Any]:
    """Reset the native host's rate-limit windows, in memory and on disk.

    NH-01 (2026-11-08 review): the sliding windows are mirrored into a
    cross-process ledger file because ``sendNativeMessage`` starts a fresh host
    process per message. Clearing only the in-memory lists therefore leaves the
    previous test's events in the ledger and makes the assertions
    order-dependent, so both views are reset here.
    """
    saved_ipc = nh_module._rate_limit_timestamps.copy()
    saved_token = nh_module._token_action_timestamps.copy()
    saved_disabled = nh_module._rate_limit_ledger_disabled
    ledger = nh_module._RATE_LIMIT_LEDGER_PATH
    try:
        nh_module._rate_limit_ledger_disabled = False
        ledger.unlink(missing_ok=True)
        with nh_module._rate_limit_lock:
            nh_module._rate_limit_timestamps.clear()
            nh_module._token_action_timestamps.clear()
        yield ledger
    finally:
        with nh_module._rate_limit_lock:
            nh_module._rate_limit_timestamps[:] = saved_ipc
            nh_module._token_action_timestamps[:] = saved_token
        nh_module._rate_limit_ledger_disabled = saved_disabled
        ledger.unlink(missing_ok=True)
