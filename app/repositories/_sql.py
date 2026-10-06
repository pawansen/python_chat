"""Small SQL-building helpers shared by the repositories.

The Sequelize ``findOne/findAll/update`` calls in the Node repositories used
plain equality ``where`` objects and attribute lists (optionally
``[column, alias]`` pairs). These helpers render the same SQL with bound
parameters. Identifiers are validated against a strict pattern — they are
always developer constants, never request input.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def ident(name: str) -> str:
    if not isinstance(name, str) or not _IDENT.match(name):
        raise ValueError(f"Unsafe SQL identifier rejected: {name}")
    return f"`{name}`"


def attrs_sql(attributes: Iterable[Any], table_alias: str | None = None) -> str:
    parts = []
    prefix = f"{ident(table_alias)}." if table_alias else ""
    for attribute in attributes:
        if isinstance(attribute, list | tuple):
            column, alias = attribute
            parts.append(f"{prefix}{ident(column)} AS {ident(alias)}")
        else:
            parts.append(f"{prefix}{ident(attribute)}")
    return ", ".join(parts)


def where_sql(where: Mapping[str, Any], params: dict[str, Any], table_alias: str | None = None, prefix: str = "w_") -> str:
    """Equality ``where`` (Sequelize object syntax). ``None`` renders ``IS NULL``; lists render ``IN``."""
    clauses = []
    alias = f"{ident(table_alias)}." if table_alias else ""
    for index, (column, value) in enumerate(where.items()):
        name = f"{prefix}{index}"
        if value is None:
            clauses.append(f"{alias}{ident(column)} IS NULL")
        elif isinstance(value, list | tuple):
            clauses.append(f"{alias}{ident(column)} IN :{name}")
            params[name] = list(value)
        else:
            clauses.append(f"{alias}{ident(column)} = :{name}")
            params[name] = value
    return " AND ".join(clauses) if clauses else "1=1"


def set_sql(data: Mapping[str, Any], params: dict[str, Any], prefix: str = "s_") -> str:
    parts = []
    for index, (column, value) in enumerate(data.items()):
        name = f"{prefix}{index}"
        parts.append(f"{ident(column)} = :{name}")
        params[name] = value
    return ", ".join(parts)


def insert_sql(table: str, values: Mapping[str, Any], params: dict[str, Any], prefix: str = "i_") -> str:
    columns = []
    placeholders = []
    for index, (column, value) in enumerate(values.items()):
        name = f"{prefix}{index}"
        columns.append(ident(column))
        placeholders.append(f":{name}")
        params[name] = value
    return f"INSERT INTO {ident(table)} ({', '.join(columns)}) VALUES ({', '.join(placeholders)})"


def error(exc: BaseException) -> dict[str, str]:
    """Repository failure envelope ``{ error: <message> }``."""
    return {"error": str(getattr(exc, "orig", None) or exc)}


def is_error(value: Any) -> bool:
    return isinstance(value, dict) and "error" in value and len(value) == 1
