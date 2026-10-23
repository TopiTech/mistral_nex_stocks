# bg/sync/__init__.py
"""Background sync worker split by concern (state / fetch / process / control).

Public API is provided via the `bg.sync_worker` facade. Test-patchable names
(`acquire_yfinance_slot`, `load_user_stocks`, `ensure_stock_placeholder_in_caches`)
are resolved at call time through the facade so
`patch("bg.sync_worker.X")` keeps working; see `_shim()`.
"""

from __future__ import annotations

import sys
from typing import Any


def _shim() -> Any:
    """Late-bound access to the `bg.sync_worker` facade.

    A top-level `import bg.sync_worker` from a submodule would be circular
    (the facade imports the submodules), so the facade is resolved from
    `sys.modules` at call time. This keeps `patch("bg.sync_worker.X")`
    working in tests. Submodules are only ever reached through the facade,
    so no fallback is needed.
    """
    return sys.modules["bg.sync_worker"]
