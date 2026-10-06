"""Fail-open cache service (port of src/services/cache.service.js).

Every method traps its own errors and returns a safe fallback
(None / False / 0 / [] / "0") so a Redis outage can never break a request.
Errors are logged with the KEY NAME ONLY — never values (they may hold PII).
"""

from __future__ import annotations

import asyncio
from typing import Any

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.core.jsonx import stringify
from app.core.logging import logger
from app.core.tasks import spawn
from app.redis import client as redis_client

EXPIRE = 3600 * 6  # 6 hours default TTL
VERSION_TTL = 604800  # 7 days: version counters are INCR + EXPIRE, never unbounded


def _fail(op: str, label: str, err: BaseException) -> None:
    if isinstance(err, RedisConnectionError | RedisTimeoutError | OSError):
        redis_client.mark_down()
    print(f"cache.{op} failed ({label}): {err}", flush=True)


async def set(key: str, value: Any, ttl: Any = None) -> bool:  # noqa: A001 - mirrors cacheService.set
    try:
        client = await redis_client.connect()
        if client is None:
            return False
        val = value if isinstance(value, str) else stringify(value)
        await client.set(key, val, ex=EXPIRE if ttl is None else int(ttl))
        redis_client.mark_up()
        return True
    except Exception as err:  # noqa: BLE001 - fail-open
        _fail("set", f"key={key}", err)
        return False


async def get(key: str) -> str | None:
    try:
        client = await redis_client.connect()
        if client is None:
            return None
        value = await client.get(key)
        redis_client.mark_up()
        return value
    except Exception as err:  # noqa: BLE001
        _fail("get", f"key={key}", err)
        return None


async def set_group(key: str, value: Any, ttl: Any = None) -> bool:
    try:
        client = await redis_client.connect()
        if client is None:
            return False
        val = value if isinstance(value, str) else stringify(value)
        await client.sadd(key, val)
        if ttl:
            await client.expire(key, int(ttl))
        return True
    except Exception as err:  # noqa: BLE001
        _fail("setGroup", f"key={key}", err)
        return False


async def get_group(key: str) -> list[str]:
    try:
        client = await redis_client.connect()
        if client is None:
            return []
        return list(await client.smembers(key))
    except Exception as err:  # noqa: BLE001
        _fail("getGroup", f"key={key}", err)
        return []


async def get_version(key: str) -> str:
    """Absent counter (or outage) -> "0", so the FIRST bump (0 -> 1) invalidates."""
    try:
        client = await redis_client.connect()
        if client is None:
            return "0"
        version = await client.get(key)
        return version or "0"
    except Exception as err:  # noqa: BLE001
        _fail("getVersion", f"key={key}", err)
        return "0"


async def bump_version(key: str) -> int:
    """INCR a version counter and re-arm its VERSION-tier TTL. 0 on failure."""
    try:
        logger.debug("cache version bump", {"key": key})
        client = await redis_client.connect()
        if client is None:
            return 0
        version = await client.incr(key)
        await client.expire(key, VERSION_TTL)
        return int(version)
    except Exception as err:  # noqa: BLE001
        _fail("bumpVersion", f"key={key}", err)
        return 0


def bump_version_bg(key: str) -> None:
    """Fire-and-forget ``cacheService.bumpVersion(key).catch(() => {})``."""
    spawn(bump_version(key))


async def delete(key: str) -> int:
    try:
        client = await redis_client.connect()
        if client is None:
            return 0
        return int(await client.delete(key))
    except Exception as err:  # noqa: BLE001
        _fail("del", f"key={key}", err)
        return 0


async def delete_conversation_list_cache_group(pattern: str) -> int:
    try:
        client = await redis_client.connect()
        if client is None:
            return 0
        keys = list(await client.smembers(pattern))
        if keys:
            await client.unlink(*keys)
            await client.delete(pattern)
        print(f"✅ Redis group cleanup done for pattern: {pattern}. Deleted {len(keys)} keys", flush=True)
        return len(keys)
    except Exception as err:  # noqa: BLE001
        _fail("deleteConversationListCacheGroup", f"pattern={pattern}", err)
        return 0


async def delete_conversation_list_cache(pattern: str) -> int:
    """SCAN MATCH pattern and UNLINK in batches of 500 with at most 5 UNLINKs in flight."""
    batch_size = 500
    unlink_concurrency = 5
    batch: list[str] = []
    deleted = 0
    pending: list[asyncio.Task] = []
    try:
        client = await redis_client.connect()
        if client is None:
            return 0
        logger.debug("redis scan started", {"pattern": pattern})
        async for key in client.scan_iter(match=pattern, count=1000):
            batch.append(key)
            if len(batch) >= batch_size:
                pending.append(asyncio.ensure_future(client.unlink(*batch)))
                deleted += len(batch)
                batch = []
                if len(pending) >= unlink_concurrency:
                    await asyncio.gather(*pending)
                    pending = []
        if batch:
            pending.append(asyncio.ensure_future(client.unlink(*batch)))
            deleted += len(batch)
        if pending:
            await asyncio.gather(*pending)
        logger.debug("redis cleanup done", {"pattern": pattern, "deleted": deleted})
        return deleted
    except Exception as err:  # noqa: BLE001
        for task in pending:
            task.cancel()
        _fail("deleteConversationListCache", f"pattern={pattern}", err)
        return 0


def delete_pattern_bg(pattern: str) -> None:
    """Fire-and-forget pattern invalidation."""
    spawn(delete_conversation_list_cache(pattern))
