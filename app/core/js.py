"""JavaScript value semantics used by the ported business logic.

The Node service relies on JS coercion rules in many places (``parseInt``,
``Number``, ``String``, truthiness, loose equality, ``undefined`` vs ``null``).
Python's rules differ (``[]``/``{}`` are falsy, ``str(True)`` is ``"True"``,
``int("12abc")`` raises), so every port of such an expression goes through
these helpers to keep request/response and room-key behaviour identical.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Any


class _Undefined:
    """Sentinel for JS ``undefined``.

    Object keys holding ``UNDEFINED`` are dropped by :mod:`app.core.jsonx`
    exactly like ``JSON.stringify`` drops ``undefined`` properties, and array
    slots become ``null``.
    """

    _instance: _Undefined | None = None

    def __new__(cls) -> _Undefined:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "undefined"


UNDEFINED: Any = _Undefined()

MAX_SAFE_INTEGER = 2**53 - 1

_PARSE_INT_RE = re.compile(r"^[\s ﻿]*([+-]?)(\d+)")
_NUMBER_RE = re.compile(r"^[+-]?(?:\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)$")


def is_undefined(value: Any) -> bool:
    return value is UNDEFINED


def nullish(value: Any) -> bool:
    """``value == null`` in JS (null or undefined)."""
    return value is None or value is UNDEFINED


def truthy(value: Any) -> bool:
    """JS truthiness: only false, 0, NaN, "", null and undefined are falsy."""
    if value is None or value is UNDEFINED or value is False:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return not (value == 0 or (isinstance(value, float) and math.isnan(value)))
    if isinstance(value, str):
        return value != ""
    return True  # objects/arrays (even empty) are truthy


def or_(value: Any, fallback: Any) -> Any:
    """JS ``value || fallback``."""
    return value if truthy(value) else fallback


def coalesce(value: Any, fallback: Any) -> Any:
    """JS ``value ?? fallback``."""
    return fallback if nullish(value) else value


def num_out(value: float | int) -> float | int:
    """Normalise a JS number for Python use: integral finite floats become int."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float) and math.isfinite(value) and value.is_integer() and abs(value) <= MAX_SAFE_INTEGER:
        return int(value)
    return value


def parse_int(value: Any) -> int | float:
    """JS ``parseInt(value)`` (radix 10). Returns ``math.nan`` when not parseable."""
    if isinstance(value, bool) or value is None or value is UNDEFINED:
        return math.nan
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return math.nan
        # parseInt(String(1e21)) -> 1 ; for ordinary floats truncate toward zero
        text = js_string(value)
    else:
        text = js_string(value)
    match = _PARSE_INT_RE.match(text)
    if not match:
        return math.nan
    sign, digits = match.groups()
    result = int(digits)
    return -result if sign == "-" else result


def number(value: Any) -> int | float:
    """JS ``Number(value)``."""
    if value is None:
        return 0
    if value is UNDEFINED:
        return math.nan
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return num_out(value)
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    if isinstance(value, str):
        text = value.strip(" \t\n\r\v\f ﻿  ")
        if text == "":
            return 0
        lowered = text.lower()
        if lowered.startswith(("0x", "0o", "0b")) and len(text) > 2:
            base = {"x": 16, "o": 8, "b": 2}[lowered[1]]
            try:
                return int(text[2:], base)
            except ValueError:
                return math.nan
        if text in ("Infinity", "+Infinity"):
            return math.inf
        if text == "-Infinity":
            return -math.inf
        if not _NUMBER_RE.match(text):
            return math.nan
        return num_out(float(text))
    if isinstance(value, list):
        if not value:
            return 0
        if len(value) == 1:
            return number(js_string(value[0]))
        return math.nan
    return math.nan


def is_nan(value: Any) -> bool:
    return isinstance(value, float) and math.isnan(value)


def to_int_or_none(value: Any) -> int | None:
    """``parseInt`` that maps NaN to ``None`` (for room keys / ids)."""
    result = parse_int(value)
    return None if is_nan(result) else int(result)


def js_string(value: Any) -> str:
    """JS ``String(value)`` / template-literal interpolation."""
    if value is None:
        return "null"
    if value is UNDEFINED:
        return "undefined"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        if value.is_integer() and abs(value) < 1e21:
            return str(int(value))
        text = repr(value)
        if "e" in text:
            mantissa, exponent = text.split("e")
            sign = "-" if exponent.startswith("-") else "+"
            return f"{mantissa}e{sign}{exponent.lstrip('+-').lstrip('0') or '0'}"
        return text
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)")
    if isinstance(value, list):
        return ",".join("" if nullish(v) else js_string(v) for v in value)
    if isinstance(value, dict):
        return "[object Object]"
    return str(value)


def room(value: Any) -> str:
    """Socket.IO room key exactly as the Node v2-compat shim produced it.

    ``socketRoomCompat.js`` coerced numeric room ids to strings; strings were
    passed through. ``parseInt`` failures (NaN) therefore addressed room "NaN".
    """
    return js_string(value)


def loose_eq(a: Any, b: Any) -> bool:
    """JS ``a == b`` for the primitive cases the service compares."""
    if nullish(a) or nullish(b):
        return nullish(a) and nullish(b)
    if isinstance(a, bool):
        a = 1 if a else 0
    if isinstance(b, bool):
        b = 1 if b else 0
    if isinstance(a, int | float) and isinstance(b, str):
        return a == number(b)
    if isinstance(a, str) and isinstance(b, int | float):
        return number(a) == b
    return a == b


def strict_eq(a: Any, b: Any) -> bool:
    """JS ``a === b`` for primitives (int 1 === float 1.0 is true in JS)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return a == b
    if type(a) is not type(b):
        return False
    return a == b


def get(obj: Any, key: str, default: Any = UNDEFINED) -> Any:
    """``obj?.[key]`` — returns UNDEFINED for missing keys / nullish objects."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return default


def now_utc() -> datetime:
    return datetime.now(UTC)


def date_now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def iso_now() -> str:
    """``new Date().toISOString()``."""
    return to_iso(now_utc())


def to_iso(value: datetime) -> str:
    """``Date.prototype.toISOString`` (millisecond precision, Z suffix)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6})\d*)?)?)?\s*(Z|[+-]\d{2}:?\d{2})?$"
)


def parse_date(value: Any) -> datetime | None:
    """``new Date(value)`` for the shapes this service stores (ISO strings / datetimes)."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, int | float) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value / 1000, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    match = _ISO_RE.match(value.strip())
    if not match:
        return None
    year, month, day, hour, minute, second, frac, tz = match.groups()
    try:
        micro = int((frac or "0").ljust(6, "0")[:6])
        result = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0), int(second or 0), micro)
    except ValueError:
        return None
    if hour is None and tz is None:
        # date-only ISO strings are parsed as UTC by JS
        return result.replace(tzinfo=UTC)
    if tz in (None, "Z"):
        # Node runs with TZ unset on the servers (UTC); local == UTC.
        return result.replace(tzinfo=UTC)
    sign = 1 if tz[0] == "+" else -1
    digits = tz[1:].replace(":", "")
    from datetime import timedelta, timezone

    offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4]))
    return result.replace(tzinfo=timezone(sign * offset)).astimezone(UTC)


def date_ms(value: Any) -> float:
    """``new Date(value).getTime()`` — NaN when invalid."""
    parsed = parse_date(value)
    if parsed is None:
        return math.nan
    return parsed.timestamp() * 1000


# The phone/number "sanitiser" regex used across aws.service.js:
#   /[\s+~`!@#$%^&*(){}\[\];:"'<,.>?\/\\|_+=-]/g
_PHONE_STRIP_RE = re.compile(r"[\s+~`!@#$%^&*(){}\[\];:\"'<,.>?/\\|_+=\-]")
# The filename sanitiser used for uploads: same set (without "+" duplicate)
_FILENAME_STRIP_RE = re.compile(r"[\s~`!@#$%^&*(){}\[\];:\"'<,.>?/\\|_+=\-]")


def strip_phone(value: Any) -> str:
    return _PHONE_STRIP_RE.sub("", value if isinstance(value, str) else js_string(value))


def sanitize_filename(value: str) -> str:
    return _FILENAME_STRIP_RE.sub("_", value)


_HTML_TAG_RE = re.compile(r"<[^>]*>")
_MULTI_WS_RE = re.compile(r"\s\s+")


def strip_html(value: str) -> str:
    """``content.replace(/<[^>]*>/g, " ").replace(/\\s\\s+/g, " ")``."""
    return _MULTI_WS_RE.sub(" ", _HTML_TAG_RE.sub(" ", value))
