"""ファンダメンタル値のサニタイズ。`services.stock_provider` からの純粋な移動。"""

from __future__ import annotations

import json
import math
import numbers
from typing import Any

import pandas as pd

_DROP_FUNDAMENTAL_VALUE = object()


def _finite_float(value: Any, default: float | None = None) -> float | None:
    """Convert provider values while rejecting non-finite numbers."""
    if value is None or isinstance(value, bool) or type(value).__name__ in ("bool_", "bool"):
        return default
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return numeric if math.isfinite(numeric) else default


def _sanitize_fundamental_value(value: Any) -> Any:
    """Return a recursively JSON-safe value or a sentinel when it must be dropped."""
    if value is None or isinstance(value, bool):
        return _DROP_FUNDAMENTAL_VALUE
    if isinstance(value, dict):
        clean_dict: dict[Any, Any] = {}
        for key, item in value.items():
            clean_item = _sanitize_fundamental_value(item)
            if clean_item is not _DROP_FUNDAMENTAL_VALUE:
                clean_dict[key] = clean_item
        return clean_dict
    if isinstance(value, (list, tuple)):
        clean_list: list[Any] = []
        for item in value:
            clean_item = _sanitize_fundamental_value(item)
            if clean_item is not _DROP_FUNDAMENTAL_VALUE:
                clean_list.append(clean_item)
        return clean_list

    try:
        missing = pd.isna(value)
        if isinstance(missing, (bool,)) and missing:
            return _DROP_FUNDAMENTAL_VALUE
        # NumPy bool_ is intentionally checked without importing NumPy solely
        # for this helper.  Calling bool() is safe after excluding array-like
        # results above.
        if type(missing).__name__ == "bool_" and bool(missing):
            return _DROP_FUNDAMENTAL_VALUE
    except (TypeError, ValueError):
        pass

    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Real):
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError):
            return _DROP_FUNDAMENTAL_VALUE
        return numeric if math.isfinite(numeric) else _DROP_FUNDAMENTAL_VALUE

    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        return _DROP_FUNDAMENTAL_VALUE
    return value


def sanitize_fundamental_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Drop invalid metrics and recursively normalize values for strict JSON output."""
    if not isinstance(data, dict):
        return {}
    clean: dict[str, Any] = {}
    for k, v in data.items():
        clean_value = _sanitize_fundamental_value(v)
        if clean_value is not _DROP_FUNDAMENTAL_VALUE:
            clean[k] = clean_value
    return clean
