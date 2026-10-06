"""Integration fixtures: real local MySQL test DB + moto DynamoDB + local Redis (db 14).

Skipped automatically when the local services are not reachable.
"""

from __future__ import annotations

import os
import socket

import pytest

from tests.harness import env


def _up(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def pytest_collection_modifyitems(config, items):
    if all(_up(p) for p in (env.MYSQL_PORT, env.DYNAMO_PORT, env.REDIS_PORT)):
        return
    skip = pytest.mark.skip(reason="local MySQL/moto/Redis not running (see tests/README.md)")
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(skip)


@pytest.fixture(autouse=True)
async def fresh_state():
    os.environ.update(env.server_env("python"))
    from app.core.config import get_settings

    get_settings.cache_clear()
    from tests.harness import seed

    seed.seed_all()
    seed.flush_redis()
    from app.db import session
    from app.dynamodb import client as dynamo
    from app.redis import client as redis_client

    session.set_engine(None)
    dynamo.set_client(None)
    yield
    from app.core.tasks import drain

    await drain(5)
    await session.dispose_engine()
    await redis_client.disconnect()
