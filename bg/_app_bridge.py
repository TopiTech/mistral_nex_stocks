# bg/_app_bridge.py
"""Single owner of the ``app_bg`` mirror bridge (M-3).

``app_bg`` imports ``bg.*``, so importing it eagerly from any ``bg`` module
would be circular. The lazy import is therefore confined to this one module,
which both reads and writes the ``app_bg.__dict__`` mirror. Every other module
imports ``_get_app_bg_attr`` / ``_set_app_bg_attr`` from here instead of
re-implementing the lookup (previously duplicated across ``bg/sync/state.py``,
``bg/common.py`` and ``bg/leader_election.py``).
"""

from __future__ import annotations

from typing import Any


def _get_app_bg_attr(name: str, fallback: Any) -> Any:
    """Return ``app_bg.<name>`` when present, else *fallback*.

    *fallback* is a **value**, not a callable: it is bound before the call, so a
    side-effecting expression must never be passed here (M-2). For a default
    that has a side effect or a cost (e.g. ``is_leader()``), use the lazy
    pattern so the read itself stays side-effect-free::

        value = _get_app_bg_attr(name, None)
        if value is None:
            value = compute_default()
    """
    try:
        import app_bg as app_bg_mod
    except ImportError:
        return fallback
    return app_bg_mod.__dict__.get(name, fallback)


def _set_app_bg_attr(name: str, val: Any) -> None:
    """Write ``app_bg.<name> = val`` (no-op when ``app_bg`` is unavailable)."""
    try:
        import app_bg as app_bg_mod
    except ImportError:
        return
    app_bg_mod.__dict__[name] = val
