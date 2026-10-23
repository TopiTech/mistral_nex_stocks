"""株価取得プロバイダの関心事ごとの分割モジュール群。

公開 API は `services.stock_provider` 経由で提供される。
`yf_session_manager` と `_handle_yf_rate_limit` はテストで
`patch(\"services.stock_provider.X\")` (名前置換) されるため、各モジュールは
呼び出し時に `services.stock_provider` 属性として解決する。
`yf` / `time` への patch は共有モジュールオブジェクトに対するものなので
直接 import でよい。ロガー名は `services.stock_provider` に統一する。
"""

from __future__ import annotations

from typing import Any


def _shim() -> Any:
    """Late-bound access to the `services.stock_provider` facade.

    A top-level `import services.stock_provider` from a submodule would be
    circular (the facade imports the submodules), so the facade is resolved
    from `sys.modules` at call time. This keeps
    `patch("services.stock_provider.X")` working in tests.
    """
    import sys

    facade = sys.modules.get("services.stock_provider")
    if facade is None:  # direct submodule import without the facade
        import importlib

        facade = importlib.import_module("services.stock_provider")
    return facade
