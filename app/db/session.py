"""Async MySQL engine (port of src/config/database.config.js).

SQLAlchemy 2.x async engine over aiomysql. The connection is configured to
return exactly what Sequelize/mysql2 returned so JSON responses do not change:

* session ``time_zone = '+00:00'`` (Sequelize's default timezone) — DATETIME
  values are UTC and ``NOW()`` is UTC;
* ``DATE`` -> ``"YYYY-MM-DD"`` string (Sequelize DATEONLY parser);
* ``DECIMAL``/``SUM()`` results -> string (mysql2 default);
* ``JSON`` columns/expressions -> parsed object (Sequelize JSON parser);
* ``TIME`` -> string; invalid zero dates -> ``None`` (JS "Invalid Date" -> null).
"""

from __future__ import annotations

import json
from datetime import datetime

from pymysql.constants import FIELD_TYPE
from pymysql.converters import conversions as _base_conversions
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import get_settings
from app.core.logging import logger

_engine: AsyncEngine | None = None


def _datetime(value: str | bytes):
    text = value.decode() if isinstance(value, bytes) else value
    if not text or text.startswith("0000-00-00"):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _as_str(value: str | bytes) -> str:
    return value.decode() if isinstance(value, bytes) else value


def _json(value: str | bytes):
    text = value.decode() if isinstance(value, bytes) else value
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def converters() -> dict:
    conv = dict(_base_conversions)
    conv[FIELD_TYPE.DATE] = _as_str
    conv[FIELD_TYPE.NEWDATE] = _as_str
    conv[FIELD_TYPE.DECIMAL] = _as_str
    conv[FIELD_TYPE.NEWDECIMAL] = _as_str
    conv[FIELD_TYPE.TIME] = _as_str
    conv[FIELD_TYPE.DATETIME] = _datetime
    conv[FIELD_TYPE.TIMESTAMP] = _datetime
    conv[FIELD_TYPE.JSON] = _json
    return conv


def build_engine() -> AsyncEngine:
    settings = get_settings()
    db = settings.db
    url = URL.create(
        "mysql+aiomysql",
        username=db.username,
        password=db.password,
        host=db.host or "localhost",
        port=int(db.port) if db.port and str(db.port).isdigit() else 3306,
        database=db.database,
        query={"charset": "utf8mb4"},
    )
    return create_async_engine(
        url,
        pool_size=max(db.pool_max, 1),
        max_overflow=0,
        pool_timeout=db.pool_acquire_ms / 1000,
        pool_recycle=1800,
        pool_pre_ping=True,
        connect_args={"conv": converters(), "init_command": "SET time_zone = '+00:00'"},
    )


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def set_engine(engine: AsyncEngine | None) -> None:
    """Test seam."""
    global _engine
    _engine = engine


async def check_connection() -> None:
    """``sequelize.authenticate()`` — logs, never raises (matches connection())."""
    from sqlalchemy import text

    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1+1 AS result"))
        print("Connection established!!", flush=True)
    except Exception as error:  # noqa: BLE001 - mirrors the Node log-and-continue behaviour
        logger.error(f"Error while connecting!! {error.__class__.__name__}")


async def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        print("✅ MySQL pool closed", flush=True)
