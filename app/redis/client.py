"""App-cache Redis client singleton (port of src/services/redisClient.service.js).

* Disabled entirely when ``IS_REDIS_ENABLED=false`` (every call returns None,
  cache becomes a no-op) — the state is logged once at import time.
* Auth/database/TLS come from env only.
* Capped exponential reconnect backoff.

Python-side improvement (documented in MIGRATION_REPORT.md): after a failed
connection attempt the client is skipped for a short back-off window so a
Redis outage costs one connect timeout per window instead of one per call.
The fail-open contract is unchanged.
"""

from __future__ import annotations

import time

from redis.asyncio import Redis
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.retry import Retry

from app.core.config import get_settings

_client: Redis | None = None
_down_until: float = 0.0


def is_enabled() -> bool:
    return get_settings().redis.is_redis_enabled


def build_client(*, decode_responses: bool = True) -> Redis:
    cfg = get_settings().redis
    return Redis(
        host=cfg.host,
        port=cfg.port,
        username=cfg.username or None,
        password=cfg.password or None,
        db=cfg.db or 0,
        ssl=cfg.tls,
        socket_connect_timeout=cfg.connect_timeout_ms / 1000,
        socket_timeout=(cfg.command_timeout_ms / 1000) if cfg.command_timeout_ms else None,
        retry=Retry(ExponentialBackoff(cap=cfg.retry_max_delay_ms / 1000, base=cfg.retry_delay_ms / 1000), 1),
        retry_on_error=[RedisConnectionError, RedisTimeoutError],
        decode_responses=decode_responses,
        protocol=2,  # RESP2 like node-redis v4 (works on Redis 5+ and ElastiCache)
        health_check_interval=30,
    )


def get_client() -> Redis | None:
    """``RedisSingleton.getClient()`` — None when caching is disabled."""
    global _client
    if not is_enabled():
        return None
    if _client is None:
        print("🧱 Creating Redis client", flush=True)
        _client = build_client()
    return _client


async def connect() -> Redis | None:
    """``RedisSingleton.connect()`` — None when disabled or inside the outage back-off window."""
    global _down_until
    client = get_client()
    if client is None:
        return None
    if _down_until and time.monotonic() < _down_until:
        raise RedisConnectionError("redis unavailable (back-off)")
    return client


def mark_down() -> None:
    """Called by the cache service on a connection failure to start the back-off window."""
    global _down_until
    cfg = get_settings().redis
    _down_until = time.monotonic() + max(cfg.retry_max_delay_ms, 1000) / 1000


def mark_up() -> None:
    global _down_until
    _down_until = 0.0


async def disconnect() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        finally:
            _client = None
            print("👋 Redis disconnected", flush=True)


def log_state() -> None:
    print(
        "✅ Redis caching ENABLED" if is_enabled() else "⛔ Redis caching DISABLED (IS_REDIS_ENABLED=false) — cache is a no-op",
        flush=True,
    )
