"""Joi/celebrate-compatible validation on top of Pydantic v2.

Every request schema in :mod:`app.schemas.chat` is a Pydantic v2 model whose
field types are built here. The field validators reproduce Joi 17 semantics
(as used through celebrate 15) so clients get byte-identical 400 responses:

* ``convert`` mode: ``Joi.number()`` accepts numeric strings (``" 12 "`` ->
  ``12``, ``"1e3"`` -> ``1000``), rejects booleans, ``""``, ``null``, hex;
* ``Joi.string()`` never coerces and rejects ``""`` unless ``allow("")``;
* ``valid(...)`` is checked before the type, so a wrong type on an enum field
  reports ``any.only``;
* unknown keys are rejected (``"x" is not allowed``) after all known keys;
* ``abortEarly``: only the first error (schema order) is reported.

The validated value replaces the request segment (celebrate does the same),
keeping the client's key order and only the keys the client sent.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, ValidationError
from pydantic_core import PydanticCustomError

from app.core.errors import CelebrateValidationError
from app.core.js import MAX_SAFE_INTEGER, js_string, num_out

_NUMERIC_STRING = re.compile(r"^\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?\s*$", re.IGNORECASE)

TEMPLATES = {
    "any.required": "is required",
    "number.base": "must be a number",
    "number.unsafe": "must be a safe number",
    "number.infinity": "cannot be infinity",
    "string.base": "must be a string",
    "string.empty": "is not allowed to be empty",
    "object.unknown": "is not allowed",
    "object.base": "must be of type object",
}


class _AllowMarker:
    """Distinguishes "allow(value)" members (e.g. "" or None) from real values."""


def _err(kind: str, **ctx: Any) -> PydanticCustomError:
    return PydanticCustomError(kind, TEMPLATES.get(kind, kind), ctx or None)


def _render_valids(valids: Iterable[Any]) -> str:
    parts = []
    for item in valids:
        if item is None:
            parts.append("null")
        elif isinstance(item, str):
            parts.append(item)
        else:
            parts.append(js_string(item))
    return "[" + ", ".join(parts) + "]"


def _matches(value: Any, candidates: Iterable[Any]) -> bool:
    for candidate in candidates:
        if candidate is None:
            if value is None:
                return True
            continue
        if isinstance(candidate, str) and isinstance(value, str) and candidate == value:
            return True
        if (
            isinstance(candidate, int | float)
            and not isinstance(candidate, bool)
            and isinstance(value, int | float)
            and not isinstance(value, bool)
            and candidate == value
        ):
            return True
    return False


def _only_error(valids: tuple[Any, ...]) -> PydanticCustomError:
    rendered = _render_valids(valids)
    prefix = "" if len(valids) == 1 else "one of "
    return PydanticCustomError("any.only", "must be " + prefix + "{valids}", {"valids": rendered})


def _coerce_number(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        raise _err("number.base")
    if isinstance(value, int | float):
        if isinstance(value, float) and math.isnan(value):
            raise _err("number.base")
        if isinstance(value, float) and math.isinf(value):
            raise _err("number.infinity")
        if abs(value) > MAX_SAFE_INTEGER:
            raise _err("number.unsafe")
        return num_out(value)
    if isinstance(value, str):
        if not _NUMERIC_STRING.match(value):
            raise _err("number.base")
        parsed = float(value.strip())
        if math.isinf(parsed):
            raise _err("number.infinity")
        if abs(parsed) > MAX_SAFE_INTEGER:
            raise _err("number.unsafe")
        if parsed == 0:
            parsed = 0.0  # "-0" -> 0
        return num_out(parsed)
    raise _err("number.base")


def joi_number(*, allow: tuple[Any, ...] = (), valid: tuple[Any, ...] | None = None) -> Any:
    """``Joi.number()`` [``.allow(...)``]."""

    def validate(value: Any) -> Any:
        if allow and _matches(value, allow):
            return value
        if valid is not None:
            converted = value
            try:
                converted = _coerce_number(value)
            except PydanticCustomError:
                pass
            if _matches(converted, valid):
                return converted
            raise _only_error(valid + allow)
        return _coerce_number(value)

    return Annotated[Any, BeforeValidator(validate)]


def joi_string(*, allow: tuple[Any, ...] = (), valid: tuple[Any, ...] | None = None) -> Any:
    """``Joi.string()`` [``.valid(...)``] [``.allow(...)``]."""

    def validate(value: Any) -> Any:
        if allow and _matches(value, allow):
            return value
        if valid is not None:
            if _matches(value, valid):
                return value
            raise _only_error(valid + allow)
        if not isinstance(value, str):
            raise _err("string.base")
        if value == "":
            raise _err("string.empty")
        return value

    return Annotated[Any, BeforeValidator(validate)]


def joi_any() -> Any:
    """``Joi.optional().allow("")`` / ``Joi.any()`` — anything goes."""
    return Annotated[Any, BeforeValidator(lambda value: value)]


class _Absent:
    def __repr__(self) -> str:
        return "<absent>"


ABSENT: Any = _Absent()


class JoiModel(BaseModel):
    """Base for celebrate segment schemas: unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    # Extra conditional rules (Joi ``.when``) evaluated in schema order:
    # list of (field_name, predicate(raw_dict) -> error_kind | None)
    __joi_conditions__: tuple[tuple[str, Callable[[dict[str, Any]], str | None]], ...] = ()


def _field_order(model: type[JoiModel]) -> list[str]:
    return list(model.model_fields.keys())


def validate_segment(model: type[JoiModel], segment: str, value: Any) -> Any:
    """Validate one request segment; returns the converted value or raises CelebrateValidationError."""
    if value is None:
        # Joi.object() accepts undefined: celebrate lets body-less requests through.
        return None
    if not isinstance(value, dict):
        raise CelebrateValidationError(segment, [""], '"value" must be of type object')

    errors: list[tuple[int, str, str]] = []  # (order, key, message)
    order = _field_order(model)
    position = {name: index for index, name in enumerate(order)}

    try:
        validated = model.model_validate(value)
        validated_values = {name: getattr(validated, name) for name in order if name in value}
    except ValidationError as exc:
        validated_values = {}
        for error in exc.errors(include_url=False):
            loc = error.get("loc") or ()
            key = js_string(loc[0]) if loc else ""
            kind = error.get("type", "")
            if kind == "missing":
                message = TEMPLATES["any.required"]
            elif kind == "extra_forbidden":
                message = TEMPLATES["object.unknown"]
            else:
                message = error.get("msg", kind)
            rank = position.get(key, len(order) + list(value.keys()).index(key) if key in value else len(order) * 2)
            errors.append((rank, key, f'"{key}" {message}'))

    for name, predicate in model.__joi_conditions__:
        outcome = predicate(value)
        if outcome:
            errors.append((position.get(name, len(order)), name, f'"{name}" {TEMPLATES.get(outcome, outcome)}'))

    if errors:
        errors.sort(key=lambda item: item[0])
        _, key, message = errors[0]
        raise CelebrateValidationError(segment, [_escape_html(key)], message)

    # Keep the client's key order; only keys the client sent (Joi returns the input shape).
    return {key: validated_values.get(key, raw) for key, raw in value.items()}


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def optional(annotation: Any) -> Any:
    """Marker used in schemas for readability: optional fields default to ABSENT."""
    return annotation
