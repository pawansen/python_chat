"""Cross-service cache-invalidation bus — SUBSCRIBER ONLY
(port of src/services/invalidation.bus.js; spec INVALIDATION_CHANNEL_SPEC.md).

Channel ``py_chat:cache:invalidate`` (protocol constant). Handled events:

* ``auth.user_changed {user_id}`` -> SCAN-delete ``chat:user:detail:{id}:*``
  and bump ``chat:version:conversation:{id}``;
* ``auth.logout {user_id}`` -> SCAN-delete ``chat:user:detail:{id}:*`` only
  (JWT revocation itself is the issuer-written ``jwt:blocklist:{jti}``);
* ``perm.*`` / ``master.changed`` / ``contract.changed`` -> logged no-ops.

Single-tenant: the payload ``tenant`` field is ignored. Delivery is
at-most-once; TTLs remain the correctness backstop. Handlers are idempotent
and fail-open; errors log the EVENT NAME ONLY (payloads carry user ids).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.config import get_settings
from app.core.logging import logger
from app.core.tasks import spawn
from app.redis import cache_keys, cache_service
from app.redis import client as redis_client

INVALIDATION_CHANNEL = "py_chat:cache:invalidate"

_stats = {"handled": 0, "errored": 0, "ignored": 0}


def get_invalidation_bus_stats() -> dict[str, int]:
    return dict(_stats)


_DIGITS = re.compile(r"^\d+$")


def normalize_user_id(value: Any) -> str | None:
    """Only positive-integer forms: no SCAN metacharacters can reach a pattern."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return str(value)
    if isinstance(value, float) and value.is_integer() and value > 0:
        return str(int(value))
    if isinstance(value, str) and _DIGITS.match(value):
        return value
    return None


Handler = Callable[[dict[str, Any]], Awaitable[None]]


def create_handlers(cache=cache_service) -> dict[str, Handler]:
    async def user_changed(data: dict[str, Any]) -> None:
        user_id = normalize_user_id((data or {}).get("user_id"))
        if not user_id:
            logger.debug("invalidation bus: auth.user_changed without usable user_id — ignored")
            return
        await cache.delete_conversation_list_cache(cache_keys.user_detail_pattern(user_id))
        await cache.bump_version(cache_keys.version_conversation_key(user_id))

    async def logout(data: dict[str, Any]) -> None:
        user_id = normalize_user_id((data or {}).get("user_id"))
        if not user_id:
            logger.debug("invalidation bus: auth.logout without usable user_id — ignored")
            return
        await cache.delete_conversation_list_cache(cache_keys.user_detail_pattern(user_id))

    def noop(event: str) -> Handler:
        async def handler(_data: dict[str, Any]) -> None:
            logger.debug(f"invalidation bus: {event} — no chat caches affected")

        return handler

    return {
        "auth.user_changed": user_changed,
        "auth.logout": logout,
        "perm.user_changed": noop("perm.user_changed"),
        "perm.role_changed": noop("perm.role_changed"),
        "master.changed": noop("master.changed"),
        "contract.changed": noop("contract.changed"),
    }


_default_handlers = create_handlers()


async def dispatch_invalidation_event(payload: Any, handlers: dict[str, Handler] | None = None) -> bool:
    handlers = handlers if handlers is not None else _default_handlers
    if not isinstance(payload, dict) or payload.get("v") != 1:
        _stats["ignored"] += 1
        logger.debug("invalidation bus: unsupported payload version — ignored")
        return False
    event = payload.get("event")
    handler = handlers.get(event) if isinstance(event, str) else None
    if handler is None:
        _stats["ignored"] += 1
        logger.debug("invalidation bus: unknown event — ignored", {"event": str(event)})
        return False
    try:
        logger.debug("invalidation bus: event received", {"event": event})
        data = payload.get("data")
        await handler(data if data is not None else {})
        _stats["handled"] += 1
        return True
    except Exception as err:  # noqa: BLE001
        _stats["errored"] += 1
        print(f"invalidation bus handler failed (event={event}): {err}", flush=True)
        return False


def handle_invalidation_message(raw: Any, handlers: dict[str, Handler] | None = None) -> None:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        _stats["ignored"] += 1
        logger.debug("invalidation bus: malformed payload — ignored")
        return
    spawn(dispatch_invalidation_event(payload, handlers))


_subscriber = None
_task: asyncio.Task | None = None
_started = False


async def _listen(pubsub) -> None:
    retry = 1.0
    while True:
        try:
            await pubsub.subscribe(INVALIDATION_CHANNEL)
            print(f"✅ Invalidation bus subscribed to {INVALIDATION_CHANNEL}", flush=True)
            retry = 1.0
            async for message in pubsub.listen():
                if message.get("type") == "message":
                    handle_invalidation_message(message.get("data"))
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - degrade to TTL behaviour, keep retrying
            print(f"❌ Invalidation bus Redis error: {err}", flush=True)
            await asyncio.sleep(retry)
            retry = min(retry * 2, get_settings().redis.retry_max_delay_ms / 1000 or 5)


async def start_invalidation_bus():
    """Start the dedicated subscriber (never blocks boot, never raises)."""
    global _subscriber, _task, _started
    try:
        if _started:
            return _subscriber
        if not get_settings().invalidation_bus_enabled:
            print("⛔ Invalidation bus DISABLED (INVALIDATION_BUS_ENABLED=false)", flush=True)
            return None
        if redis_client.get_client() is None:
            print("⛔ Invalidation bus not started — Redis cache is disabled", flush=True)
            return None
        _started = True
        _subscriber = redis_client.build_client()
        pubsub = _subscriber.pubsub(ignore_subscribe_messages=True)
        _task = asyncio.get_running_loop().create_task(_listen(pubsub))
        return _subscriber
    except Exception as err:  # noqa: BLE001
        print(f"❌ Invalidation bus failed to start: {err}", flush=True)
        return None


async def stop_invalidation_bus() -> None:
    global _subscriber, _task, _started
    task, sub = _task, _subscriber
    _task, _subscriber, _started = None, None, False
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    if sub is not None:
        try:
            await sub.aclose()
            print("👋 Invalidation bus subscriber closed", flush=True)
        except Exception as err:  # noqa: BLE001
            print(f"invalidation bus close error: {err}", flush=True)
