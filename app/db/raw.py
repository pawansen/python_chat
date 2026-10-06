"""Thin helpers over SQLAlchemy ``text()`` for the service's raw SQL.

Port of ``sequelize.query(sql, { replacements, type })``:

* named ``:params`` are always bound (never interpolated);
* list/tuple values expand like Sequelize array replacements (``IN (:ids)``);
* SELECT results are plain dicts in column order (``QueryTypes.SELECT``).

Every helper accepts an optional ``conn`` so callers can run inside a
transaction (``async with transaction() as conn``).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.session import get_engine

_NAMED = re.compile(r"(?<![:\w]):([A-Za-z_][A-Za-z0-9_]*)")


def _prepare(sql: str, params: Mapping[str, Any] | None):
    from app.core.js import UNDEFINED

    supplied = dict(params or {})
    for name in _NAMED.findall(sql):
        if name not in supplied or supplied[name] is UNDEFINED:
            # Sequelize v6: a replacement that is missing/undefined throws before querying.
            raise ValueError(f'Named replacement ":{name}" has no entry in the replacement map.')
    statement = text(sql)
    # Only pass the params the statement references (Sequelize ignores extras).
    used = {
        key: value
        for key, value in dict(params or {}).items()
        if re.search(r"(?<![:\w]):" + re.escape(key) + r"(?!\w)", sql)
    }
    expanding_keys = [key for key, value in used.items() if isinstance(value, list | tuple)]
    if expanding_keys:
        # Sequelize style is ``IN (:ids)``; SQLAlchemy's expanding bind renders
        # its own parentheses, so strip the literal ones around the placeholder.
        for key in expanding_keys:
            sql = re.sub(r"\(\s*:" + re.escape(key) + r"\s*\)", ":" + key, sql)
            used[key] = list(used[key])
        statement = text(sql).bindparams(*[bindparam(key, expanding=True) for key in expanding_keys])
    return statement, used


async def select(sql: str, params: Mapping[str, Any] | None = None, *, conn: AsyncConnection | None = None) -> list[dict]:
    statement, used = _prepare(sql, params)
    if conn is not None:
        result = await conn.execute(statement, used)
        return [dict(row._mapping) for row in result]
    async with get_engine().connect() as connection:
        result = await connection.execute(statement, used)
        return [dict(row._mapping) for row in result]


async def select_one(sql: str, params: Mapping[str, Any] | None = None, *, conn: AsyncConnection | None = None) -> dict | None:
    rows = await select(sql, params, conn=conn)
    return rows[0] if rows else None


async def execute(sql: str, params: Mapping[str, Any] | None = None, *, conn: AsyncConnection | None = None) -> int:
    """Run a write statement; returns affected rows. Autocommits outside a transaction."""
    statement, used = _prepare(sql, params)
    if conn is not None:
        result = await conn.execute(statement, used)
        return result.rowcount
    async with get_engine().begin() as connection:
        result = await connection.execute(statement, used)
        return result.rowcount


async def insert(sql: str, params: Mapping[str, Any] | None = None, *, conn: AsyncConnection | None = None) -> int:
    """Run an INSERT; returns ``lastrowid``."""
    statement, used = _prepare(sql, params)
    if conn is not None:
        result = await conn.execute(statement, used)
        return result.lastrowid
    async with get_engine().begin() as connection:
        result = await connection.execute(statement, used)
        return result.lastrowid


@asynccontextmanager
async def transaction() -> AsyncIterator[AsyncConnection]:
    """``sequelize.transaction(async t => ...)`` — commit on success, rollback on error."""
    async with get_engine().begin() as conn:
        yield conn
