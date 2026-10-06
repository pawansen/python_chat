"""``JSON.stringify``-compatible serialisation.

Used for every HTTP response body, every Socket.IO payload, every value cached
in Redis and every string that is hashed into a cache key, so the Python
service produces byte-identical JSON to the Node service:

* no whitespace, non-ASCII left as-is (``JSON.stringify`` default);
* ``undefined`` object properties dropped, ``undefined`` array slots -> null;
* ``NaN``/``Infinity`` -> null; integral floats printed without ``.0``;
* ``datetime`` -> ``Date.toISOString()`` (UTC, millisecond precision);
* ``Decimal`` -> number (DynamoDB numbers), ``date`` -> ``YYYY-MM-DD``;
* ``bytes`` -> Node ``Buffer.toJSON`` shape.

The module also exposes ``dumps``/``loads`` so it can be handed to
python-socketio as its ``json`` implementation.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.core.js import UNDEFINED, js_string, num_out, to_iso


def to_jsonable(value: Any, *, in_array: bool = False) -> Any:
    """Recursively convert to plain JSON types following JS semantics."""
    if value is UNDEFINED:
        return None if in_array else UNDEFINED
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return num_out(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, datetime):
        return to_iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if item is UNDEFINED or callable(item):
                continue
            converted = to_jsonable(item)
            if converted is UNDEFINED:
                continue
            out[key if isinstance(key, str) else js_string(key)] = converted
        return out
    if isinstance(value, list | tuple):
        return [to_jsonable(item, in_array=True) for item in value]
    if isinstance(value, set | frozenset):
        return {}
    if isinstance(value, bytes | bytearray):
        return {"type": "Buffer", "data": list(value)}
    if hasattr(value, "model_dump"):
        return to_jsonable(value.model_dump())
    return js_string(value)


def stringify(value: Any) -> str:
    """``JSON.stringify(value)``. Top-level ``undefined`` yields ``"null"``."""
    converted = to_jsonable(value)
    if converted is UNDEFINED:
        return "null"
    return json.dumps(converted, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def dumps(value: Any, *args: Any, **kwargs: Any) -> str:  # python-socketio json interface
    return stringify(value)


def loads(text: str | bytes, *args: Any, **kwargs: Any) -> Any:
    return json.loads(text)


def parse(text: str | bytes) -> Any:
    """``JSON.parse``."""
    return json.loads(text)
