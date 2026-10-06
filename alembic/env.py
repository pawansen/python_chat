"""Alembic environment (async engine, same DB_* settings as the service).

IMPORTANT: the tables are owned by the shared py_chat database. This chain
never creates or drops tables; it only carries the chat service's own
idempotent, additive schema changes (ported from the Node ``migrations/``
SQL files). Nothing runs automatically at application start-up — apply with
``alembic upgrade head`` after review, or ``alembic stamp head`` on a database
where the Node migrations were already applied.
"""

from __future__ import annotations

import asyncio

from sqlalchemy.engine import Connection

from alembic import context
from app.db.session import build_engine
from app.models import metadata

config = context.config
target_metadata = metadata


def run_migrations_offline() -> None:
    context.configure(url=str(build_engine().url), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=False)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = build_engine()
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
