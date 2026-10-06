"""Socket.IO server (port of the Socket.IO setup in src/server.js).

python-socketio ``AsyncServer`` mounted on the same ASGI app/port as the
REST API at ``/socket.io``: CORS limited to the same origin list with
credentials, polling + websocket transports, 1 MB max buffer
(SOCKET_MAX_HTTP_BUFFER_SIZE), ping 25 s / timeout 20 s, JS-compatible JSON.

Compatibility: python-socketio implements Socket.IO protocol v5 / Engine.IO
v4 (socket.io-client v3/v4). Node's ``allowEIO3`` (socket.io-client v2) has
no python-engineio equivalent — v2 clients must upgrade before cut-over.
"""

from __future__ import annotations

import socketio

from app.core import jsonx
from app.core.config import get_settings
from app.core.logging import logger
from app.core.security import authenticate_socket_connection, extract_handshake_token
from app.socket import emitter, rate_limiter
from app.socket.handlers import ChatSocketService
from app.socket.manager import ChatRedisManager

EVENTS = (
    "join_branch_room",
    "join_room_user",
    "branch_checkin",
    "join_room",
    "send_message",
    "chat_message_read",
    "delete_message",
)


def create_socket_server() -> socketio.AsyncServer:
    settings = get_settings()
    manager = None
    if settings.redis.socket_adapter_enabled:
        manager = ChatRedisManager(sharded=settings.redis.adapter_sharded)
    else:
        print("⚠️  Socket.IO Redis adapter DISABLED — in-memory adapter, single instance only.", flush=True)
    sio = socketio.AsyncServer(
        async_mode="asgi",
        cors_allowed_origins=settings.allowed_origins,
        cors_credentials=True,
        transports=["polling", "websocket"],
        max_http_buffer_size=settings.socket.max_http_buffer_size,
        ping_interval=25,
        ping_timeout=20,
        json=jsonx,
        client_manager=manager,
        logger=False,
        engineio_logger=False,
    )
    service = ChatSocketService(sio)
    emitter.bind(sio)

    @sio.event
    async def connect(sid, environ, auth=None):
        """HI-09 handshake auth; enforcement ON unless SOCKET_HANDSHAKE_AUTH_ENFORCE=false."""
        enforce = get_settings().socket.handshake_auth_enforce
        try:
            token = extract_handshake_token(auth, environ)
            user = await authenticate_socket_connection(token) if token else None
            if user:
                await sio.save_session(sid, {"user": user, "token": token, "presence": None})
            elif enforce:
                raise socketio.exceptions.ConnectionRefusedError("UNAUTHORIZED")
            else:
                await sio.save_session(sid, {"user": None, "token": None, "presence": None})
        except socketio.exceptions.ConnectionRefusedError:
            raise
        except Exception as error:  # noqa: BLE001
            print(f"socket handshake auth error: {error}", flush=True)
            if enforce:
                raise socketio.exceptions.ConnectionRefusedError("UNAUTHORIZED") from error
            await sio.save_session(sid, {"user": None, "token": None, "presence": None})
        logger.debug("socket connected", {"socketId": sid})

    @sio.event
    async def disconnect(sid, *args):
        try:
            await service.disconnect(sid)
        except Exception as error:  # noqa: BLE001
            logger.error("disconnect handler error", error)

    async def limited(sid: str, event: str) -> bool:
        if not get_settings().socket.rate_limit_enabled:
            return True
        try:
            limiter = rate_limiter.for_sid(sid)
            if limiter.allow(event):
                return True
            await sio.emit("rate_limit_exceeded", limiter.exceeded_payload(event), to=sid)
            return False
        except Exception as error:  # noqa: BLE001 - the limiter must never break message flow
            print(f"socket rate limiter error: {error}", flush=True)
            return True

    def register(event: str) -> None:
        method = getattr(service, event)

        async def handler(sid, data=jsonx_undefined(), *_args):
            if not await limited(sid, event):
                return
            try:
                await method(sid, data)
            except Exception as error:  # noqa: BLE001
                logger.error(f"{event} error", {"error": str(error)})

        sio.on(event, handler)

    for event in EVENTS:
        register(event)

    @sio.on("*")
    async def catch_all(event, sid, *_args):
        # socket.use() in Node counted every inbound event, handled or not.
        await limited(sid, event)

    return sio


def jsonx_undefined():
    from app.core.js import UNDEFINED

    return UNDEFINED


async def start_backplane(sio: socketio.AsyncServer) -> None:
    """Connect Redis BEFORE serving (fail fast like Node's process.exit(1))."""
    manager = sio.manager
    if isinstance(manager, ChatRedisManager):
        await manager.connect()
        sio.manager_initialized = True
        manager.initialize()


async def stop_backplane(sio: socketio.AsyncServer) -> None:
    manager = sio.manager
    if isinstance(manager, ChatRedisManager):
        await manager.close()
