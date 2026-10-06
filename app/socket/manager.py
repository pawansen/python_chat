"""Redis backplane for python-socketio (replaces @socket.io/redis-adapter).

Why a custom manager:

* ElastiCache Serverless / cluster-mode rejects classic ``PSUBSCRIBE``; the
  Node service used the *sharded* adapter (``SSUBSCRIBE``/``SPUBLISH``,
  Redis 7+). python-socketio's stock ``AsyncRedisManager`` only speaks
  classic pub/sub, so this manager supports both
  (``REDIS_SOCKET_ADAPTER_SHARDED``, default true).
* The Node code relied on ``io.in(room).fetchSockets()`` (cross-instance) for
  delivered receipts. python-socketio has no equivalent, so this manager
  implements the same request/response protocol over the backplane:
  broadcast a ``fetch_room_users`` request, every instance answers with its
  local sockets' ``data.user``, the caller waits for all subscribers (or a
  5s timeout, the adapter's ``requestsTimeout``).

NOTE: the wire format is python-socketio's, not @socket.io/redis-adapter's,
so Node and Python instances cannot share one backplane — cut over a whole
fleet at once (see MIGRATION_REPORT.md §14).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from redis.asyncio import Redis
from socketio.async_pubsub_manager import AsyncPubSubManager

from app.core.config import get_settings

REQUEST_TIMEOUT_S = 5.0


class ChatRedisManager(AsyncPubSubManager):
    name = "py_chat-redis"

    def __init__(self, channel: str = "socket.io#/#", sharded: bool = True, logger=None) -> None:
        super().__init__(channel=channel, write_only=False, logger=logger)
        self.sharded = sharded
        self.redis: Redis | None = None
        self.pubsub = None
        self._pending: dict[str, dict[str, Any]] = {}

    # ── connection ────────────────────────────────────────────────────────
    def _build(self) -> Redis:
        cfg = get_settings().redis
        return Redis(
            host=cfg.adapter_host,
            port=cfg.adapter_port,
            password=cfg.adapter_password or None,
            ssl=cfg.adapter_tls,
            socket_connect_timeout=5,
            decode_responses=False,
            protocol=2,
        )

    async def connect(self) -> None:
        """Connect and verify BEFORE the server listens (fail fast in cluster mode)."""
        self.redis = self._build()
        await self.redis.ping()
        cfg = get_settings().redis
        mode = "sharded SSUBSCRIBE" if self.sharded else "classic PSUBSCRIBE"
        print(
            f"✅ Socket.IO Redis adapter attached ({cfg.adapter_host}:{cfg.adapter_port}{', TLS' if cfg.adapter_tls else ''}, {mode})",
            flush=True,
        )

    async def close(self) -> None:
        clients = [c for c in (self.pubsub, self.redis) if c is not None]
        self.pubsub = None
        self.redis = None
        for client in clients:
            try:
                await client.aclose()
            except Exception as error:  # noqa: BLE001
                print(f"❌ Error quitting Socket.IO Redis adapter client: {error}", flush=True)
        if clients:
            print("👋 Socket.IO Redis adapter clients closed", flush=True)

    # ── pub/sub transport ─────────────────────────────────────────────────
    async def _publish(self, data: Any) -> None:
        payload = self.json.dumps(data)
        for retries_left in (1, 0):
            try:
                if self.redis is None:
                    self.redis = self._build()
                if self.sharded:
                    await self.redis.execute_command("SPUBLISH", self.channel, payload)
                else:
                    await self.redis.publish(self.channel, payload)
                return
            except Exception as error:  # noqa: BLE001
                if retries_left:
                    self.redis = None
                    continue
                self._get_logger().error("Cannot publish to redis... giving up: %s", error)

    async def _redis_listen_with_retries(self):
        retry_sleep = 1
        while True:
            try:
                if self.redis is None:
                    self.redis = self._build()
                self.pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
                if self.sharded:
                    await self.pubsub.ssubscribe(self.channel)
                else:
                    await self.pubsub.subscribe(self.channel)
                retry_sleep = 1
                async for message in self.pubsub.listen():
                    yield message
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                self._get_logger().error("Cannot receive from redis... retrying in %s secs: %s", retry_sleep, error)
                self.redis = None
                await asyncio.sleep(retry_sleep)
                retry_sleep = min(retry_sleep * 2, 5)

    async def _listen(self):
        channel = self.channel.encode("utf-8")
        async for message in self._redis_listen_with_retries():
            if message.get("type") in ("message", "smessage") and message.get("channel") == channel and "data" in message:
                yield message["data"]

    # ── cross-instance fetchSockets ───────────────────────────────────────
    async def _subscriber_count(self) -> int:
        if self.redis is None:
            return 1
        try:
            if self.sharded:
                result = await self.redis.execute_command("PUBSUB", "SHARDNUMSUB", self.channel)
            else:
                result = await self.redis.execute_command("PUBSUB", "NUMSUB", self.channel)
            # [channel, count]
            return int(result[1]) if isinstance(result, list | tuple) and len(result) >= 2 else 1
        except Exception:  # noqa: BLE001
            return 1

    async def _local_room_users(self, namespace: str, room: str) -> list[dict[str, Any]]:
        users = []
        for sid, _eio in list(self.get_participants(namespace, room)):
            try:
                session = await self.server.get_session(sid, namespace=namespace)
            except Exception:  # noqa: BLE001
                continue
            users.append({"sid": sid, "user": session.get("user")})
        return users

    async def fetch_room_users(self, namespace: str, room: str) -> list[dict[str, Any]]:
        local = await self._local_room_users(namespace, room)
        expected = await self._subscriber_count() - 1
        if expected <= 0:
            return local
        request_id = uuid.uuid4().hex
        waiter = {"future": asyncio.get_running_loop().create_future(), "remaining": expected, "users": list(local)}
        self._pending[request_id] = waiter
        try:
            await self._publish(
                {"method": "fetch_room_users", "request_id": request_id, "namespace": namespace, "room": room, "host_id": self.host_id}
            )
            try:
                await asyncio.wait_for(waiter["future"], timeout=REQUEST_TIMEOUT_S)
            except TimeoutError:
                pass
            return waiter["users"]
        finally:
            self._pending.pop(request_id, None)

    async def _handle_fetch_request(self, data: dict[str, Any]) -> None:
        users = await self._local_room_users(data.get("namespace") or "/", data.get("room"))
        await self._publish(
            {
                "method": "fetch_room_users_response",
                "request_id": data.get("request_id"),
                "target": data.get("host_id"),
                "users": users,
                "host_id": self.host_id,
            }
        )

    def _handle_fetch_response(self, data: dict[str, Any]) -> None:
        if data.get("target") != self.host_id:
            return
        waiter = self._pending.get(data.get("request_id"))
        if not waiter:
            return
        waiter["users"].extend(data.get("users") or [])
        waiter["remaining"] -= 1
        if waiter["remaining"] <= 0 and not waiter["future"].done():
            waiter["future"].set_result(True)

    async def _thread(self):
        """Stock dispatch loop plus the fetch_room_users request/response methods."""
        while True:
            try:
                async for message in self._listen():
                    data = None
                    if isinstance(message, dict):
                        data = message
                    else:
                        try:
                            data = self.json.loads(message)
                        except Exception:  # noqa: BLE001
                            data = None
                    if not data or "method" not in data:
                        continue
                    method = data["method"]
                    try:
                        if method == "callback":
                            await self._handle_callback(data)
                        elif method == "fetch_room_users_response":
                            self._handle_fetch_response(data)
                        elif data.get("host_id") != self.host_id:
                            if method == "emit":
                                await self._handle_emit(data)
                            elif method == "disconnect":
                                await self._handle_disconnect(data)
                            elif method == "enter_room":
                                await self._handle_enter_room(data)
                            elif method == "leave_room":
                                await self._handle_leave_room(data)
                            elif method == "close_room":
                                await self._handle_close_room(data)
                            elif method == "fetch_room_users":
                                await self._handle_fetch_request(data)
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001
                        self._get_logger().exception("Handler error in pubsub listening thread")
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                self._get_logger().exception("Unexpected Error in pubsub listening thread")
                await asyncio.sleep(1)
