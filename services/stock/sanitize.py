"""ファンダメンタル値のサニタイズ。`services.stock_provider` からの純粋な移動。"""

from __future__ import annotations

import json
import math
import numbers
from typing import Any

import pandas as pd

from utils.normalization import finite_float, is_bool_like

_DROP_FUNDAMENTAL_VALUE = object()
_MAX_FUNDAMENTAL_DEPTH = 32


def _finite_float(value: Any, default: float | None = None) -> float | None:
    """Convert provider values while rejecting non-finite numbers."""
    return finite_float(value, default)


def _sanitize_fundamental_key(key: Any) -> Any:
    """Normalize dict keys so nested fundamental payloads are always strict-JSON serializable."""
    if isinstance(key, str):
        try:
            json.dumps(key, allow_nan=False)
            return key
        except (TypeError, ValueError, OverflowError):
            return _DROP_FUNDAMENTAL_VALUE
    if key is None or is_bool_like(key):
        return str(key).lower() if is_bool_like(key) else "null"
    if isinstance(key, numbers.Integral):
        return int(key)
    if isinstance(key, numbers.Real):
        try:
            numeric = float(key)
        except (TypeError, ValueError, OverflowError):
            return _DROP_FUNDAMENTAL_VALUE
        return str(numeric) if math.isfinite(numeric) else _DROP_FUNDAMENTAL_VALUE
    try:
        key_str = str(key)
        json.dumps(key_str, allow_nan=False)
        return key_str
    except Exception:
        return _DROP_FUNDAMENTAL_VALUE


def _sanitize_fundamental_value(
    value: Any, *, _seen: set[int] | None = None, _depth: int = 0
) -> Any:
    """Return a recursively JSON-safe value or a sentinel when it must be dropped."""
    if value is None or is_bool_like(value):
        return _DROP_FUNDAMENTAL_VALUE
    if isinstance(value, (dict, list, tuple)):
        if _depth >= _MAX_FUNDAMENTAL_DEPTH:
            return _DROP_FUNDAMENTAL_VALUE
        if _seen is None:
            _seen = set()
        obj_id = id(value)
        if obj_id in _seen:
            return _DROP_FUNDAMENTAL_VALUE
        _seen.add(obj_id)
        try:
            if isinstance(value, dict):
                clean_dict: dict[Any, Any] = {}
                for key, item in value.items():
                    clean_key = _sanitize_fundamental_key(key)
                    if clean_key is _DROP_FUNDAMENTAL_VALUE:
                        continue
                    clean_item = _sanitize_fundamental_value(
                        item, _seen=_seen, _depth=_depth + 1
                    )
                    if clean_item is not _DROP_FUNDAMENTAL_VALUE:
                        clean_dict[clean_key] = clean_item
                return clean_dict
            clean_list: list[Any] = []
            for item in value:
                clean_item = _sanitize_fundamental_value(
                    item, _seen=_seen, _depth=_depth + 1
                )
                if clean_item is not _DROP_FUNDAMENTAL_VALUE:
                    clean_list.append(clean_item)
            return clean_list
        finally:
            _seen.discard(obj_id)

    try:
        missing = pd.isna(value)
        if is_bool_like(missing) and bool(missing):
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
    sanitized = _sanitize_fundamental_value(data)
    return sanitized if isinstance(sanitized, dict) else {}
