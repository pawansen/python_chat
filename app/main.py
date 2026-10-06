"""ASGI entry point (port of src/server.js).

``app`` = Socket.IO (``/socket.io``) in front of the Express-compatible HTTP
pipeline in front of the FastAPI router — one port, like the Node httpServer.

Startup (lifespan):
  load locales -> MySQL connection check (log-only, like Node) -> Socket.IO
  Redis backplane (FAIL FAST: exit(1) when enabled and unreachable) ->
  translation + webhook workers -> LCC OTA -> invalidation-bus subscriber.
Shutdown (SIGTERM/SIGINT via uvicorn): close Socket.IO (clients reconnect
elsewhere) -> backplane -> invalidation bus -> app-cache Redis -> MySQL
pool -> HTTP client, with a 10 s budget (uvicorn --timeout-graceful-shutdown).
"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager

import socketio
from fastapi import FastAPI
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request

from app.api.http import send
from app.api.pipeline import NodePipelineMiddleware
from app.api.routes import build_router
from app.core import i18n
from app.core.config import get_settings
from app.core.http import close_client
from app.core.logging import configure_stdlib_logging
from app.core.tasks import drain
from app.db import session as db_session
from app.lcc_ota import LccOta
from app.queues.webhook_queue import queue_webhook
from app.redis import client as redis_client
from app.redis.invalidation_bus import start_invalidation_bus, stop_invalidation_bus
from app.services.chat.whatsapp_flows import handle_webhook_whatsapp
from app.socket.emitter import io
from app.socket.server import create_socket_server, start_backplane, stop_backplane
from app.workers import init_translation_worker, init_webhook_worker

configure_stdlib_logging()
settings = get_settings()
sio = create_socket_server()
lcc_ota = LccOta()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    i18n.load_catalogs()
    redis_client.log_state()
    await db_session.check_connection()
    if settings.redis.socket_adapter_enabled:
        try:
            await start_backplane(sio)
        except Exception as error:  # noqa: BLE001
            print(f"❌ FATAL: could not attach Socket.IO Redis adapter: {error}", flush=True)
            sys.stdout.flush()
            os._exit(1)
    init_translation_worker(io)
    init_webhook_worker(io)
    lcc_ota.start()
    print(f"py_chat server listening on port {settings.port}", flush=True)
    await start_invalidation_bus()
    try:
        yield
    finally:
        print("\n🛑 Shutting down — starting graceful shutdown…", flush=True)
        try:
            await sio.shutdown()
            print("✅ Socket.IO server closed", flush=True)
        except Exception as error:  # noqa: BLE001
            print(f"❌ Error closing Socket.IO: {error}", flush=True)
        await stop_backplane(sio)
        await stop_invalidation_bus()
        await lcc_ota.stop()
        await drain(5.0)
        await redis_client.disconnect()
        await db_session.dispose_engine()
        await close_client()
        print("👋 Shutdown complete", flush=True)


def create_fastapi() -> FastAPI:
    docs_enabled = os.environ.get("ENABLE_API_DOCS") == "true"
    api = FastAPI(
        title="py_chat Chat API",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    if settings.http_compression_enabled:
        api.add_middleware(GZipMiddleware, minimum_size=1024)
    api.include_router(build_router())

    @api.exception_handler(StarletteHTTPException)
    async def not_found(request: Request, _exc: StarletteHTTPException):
        return send(404, {"message": "Not found!"}, method=request.method)

    return api


fastapi_app = create_fastapi()


async def _readiness() -> None:
    from sqlalchemy import text

    async with db_session.get_engine().connect() as conn:
        await conn.execute(text("SELECT 1+1 AS result"))


http_app = NodePipelineMiddleware(
    fastapi_app,
    on_whatsapp_webhook=handle_webhook_whatsapp,
    on_line_webhook=lambda body: queue_webhook({"source": "line", "body": body}),
    readiness=_readiness,
)

app = socketio.ASGIApp(sio, other_asgi_app=http_app, socketio_path="socket.io")
