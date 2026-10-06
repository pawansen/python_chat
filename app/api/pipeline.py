"""Pre-routing HTTP pipeline (port of the Express middleware chain in server.js).

Order (identical to Node):
  1. CORS preflight (any OPTIONS -> 204, before everything else)
  2. CORS + helmet headers on every response
  3. X-Request-Id (honour inbound, else uuid4; echoed back)
  4. body parsing: JSON (strict, 100 kb) / urlencoded (100 kb, hpp: last
     value wins); parse failures surface as Express's final-handler 500
     (handleError itself crashes on ``req.__`` before i18n is initialised)
  5. static ``public/`` files
  6. POST /chats/webhook, POST /chats/line-hook (own 300/min/IP limiter) ->
     ack 200 "OK" and enqueue
  7. GET /livez, GET /readyz
  8. global per-IP rate limit (GLOBAL_RATE_LIMIT_MAX/min, fail-open)
  9. x-api-key gate (constant-time, API_KEYS rotation) -> 400 on failure
  10. FastAPI router (i18n, auth, validation, services) / 404 "Not found!"

Socket.IO traffic (``/socket.io``) is routed before this pipeline, exactly
like Node where Engine.IO handled it off the HTTP server.
"""

from __future__ import annotations

import ipaddress
import json
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from starlette.responses import FileResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.http import FINALHANDLER_500, HELMET_HEADERS, cors_headers, preflight_response, send, send_status
from app.core.config import BASE_DIR, get_settings
from app.core.context import request_id_var, user_id_var
from app.core.js import iso_now
from app.core.logging import logger
from app.core.security import is_api_key_valid

BODY_LIMIT = 100 * 1024
PUBLIC_DIR = BASE_DIR / "public"


class BodyError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _reject_constant(token: str) -> Any:
    raise ValueError(f"Unexpected token '{token[0]}', \"{token}\" is not valid JSON")


def parse_body(content_type: str, raw: bytes) -> Any:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype == "application/json" or (ctype.startswith("application/") and ctype.endswith("+json")):
        if len(raw) > BODY_LIMIT:
            raise BodyError(413, "request entity too large")
        if not raw:
            return {}
        text = raw.decode("utf-8", errors="replace")
        stripped = text.lstrip(" \t\n\r")
        if stripped[:1] not in ("{", "["):
            raise BodyError(400, f"Unexpected token in JSON at position {len(text) - len(stripped)}")
        try:
            return json.loads(text, parse_constant=_reject_constant)
        except ValueError as error:
            raise BodyError(400, str(error)) from error
    if ctype == "application/x-www-form-urlencoded":
        if len(raw) > BODY_LIMIT:
            raise BodyError(413, "request entity too large")
        body: dict[str, Any] = {}
        for key, value in parse_qsl(raw.decode("utf-8", errors="replace"), keep_blank_values=True)[:1000]:
            body[key] = value  # hpp: duplicated body params collapse to the LAST value
        return body
    return None


def parse_query(query_string: bytes) -> dict[str, Any]:
    """Express 5 "simple" (querystring) parser: duplicates become arrays; hpp is a no-op on req.query."""
    out: dict[str, Any] = {}
    for key, value in parse_qsl(query_string.decode("latin-1"), keep_blank_values=True):
        if key in out:
            existing = out[key]
            out[key] = existing + [value] if isinstance(existing, list) else [existing, value]
        else:
            out[key] = value
    return out


class FixedWindowLimiter:
    """express-rate-limit MemoryStore (per-key fixed window) + draft-6 headers."""

    def __init__(self, limit_getter: Callable[[], int], window_ms: int = 60_000) -> None:
        self.limit_getter = limit_getter
        self.window_ms = window_ms
        self.hits: dict[str, tuple[int, float]] = {}

    def hit(self, key: str) -> tuple[bool, list[tuple[str, str]]]:
        now = time.time() * 1000
        count, reset = self.hits.get(key, (0, 0))
        if reset <= now:
            count, reset = 0, now + self.window_ms
        count += 1
        self.hits[key] = (count, reset)
        if len(self.hits) > 50_000:
            for k in [k for k, (_c, r) in self.hits.items() if r <= now]:
                self.hits.pop(k, None)
        limit = self.limit_getter()
        remaining = max(limit - count, 0)
        reset_seconds = max(0, math.ceil((reset - now) / 1000))
        headers = [
            ("RateLimit-Policy", f"{limit};w={math.ceil(self.window_ms / 1000)}"),
            ("RateLimit-Limit", str(limit)),
            ("RateLimit-Remaining", str(remaining)),
            ("RateLimit-Reset", str(reset_seconds)),
        ]
        allowed = count <= limit
        if not allowed:
            headers.append(("Retry-After", str(reset_seconds)))
        return allowed, headers


def client_key(scope: Scope) -> str:
    host = (scope.get("client") or ("unknown", 0))[0] or "unknown"
    try:
        ip = ipaddress.ip_address(host)
        if ip.version == 6 and not ip.ipv4_mapped:
            return str(ipaddress.ip_network(f"{host}/56", strict=False))
        if ip.version == 6 and ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
    except ValueError:
        pass
    return host


webhook_limiter = FixedWindowLimiter(lambda: get_settings().webhook_rate_limit_max)
global_limiter = FixedWindowLimiter(lambda: get_settings().global_rate_limit_max)


class NodePipelineMiddleware:
    def __init__(self, app: ASGIApp, *, on_whatsapp_webhook: Callable[[Any], Any], on_line_webhook: Callable[[Any], Any],
                 readiness: Callable[[], Awaitable[None]]) -> None:
        self.app = app
        self.on_whatsapp_webhook = on_whatsapp_webhook
        self.on_line_webhook = on_line_webhook
        self.readiness = readiness

    async def __call__(self, scope: Scope, receive: Receive, send_fn: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send_fn)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        method = scope["method"]
        path = scope["path"]
        origin = headers.get("origin")

        if method == "OPTIONS":
            await preflight_response(origin)(scope, receive, send_fn)
            return

        extra_headers = cors_headers(origin) + HELMET_HEADERS
        incoming_id = headers.get("x-request-id", "").strip()
        request_id = incoming_id or str(uuid.uuid4())
        extra_headers.append(("x-request-id", request_id))
        token = request_id_var.set(request_id)
        user_token = user_id_var.set(None)
        started = time.monotonic()
        status_holder = {"status": 0}

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                raw_headers = list(message.get("headers", []))
                present = {k.decode("latin-1").lower() for k, _ in raw_headers}
                for key, value in extra_headers:
                    if key.lower() == "vary" and "vary" in present:
                        continue
                    raw_headers.append((key.encode("latin-1"), value.encode("latin-1")))
                message = {**message, "headers": raw_headers}
            await send_fn(message)

        async def respond(response: Response) -> None:
            await response(scope, receive, send_with_headers)

        try:
            raw = b""
            more = True
            while more:
                message = await receive()
                raw += message.get("body", b"")
                more = message.get("more_body", False)
                if len(raw) > BODY_LIMIT * 20:
                    break
            scope.setdefault("state", {})
            content_type = headers.get("content-type", "")
            try:
                parsed = parse_body(content_type, raw)
            except BodyError as error:
                # body-parser error -> handleError -> `req.__` is not yet defined -> Express final handler.
                logger.error("body parse error", {"status": error.status, "error": str(error)})
                response = send(500, FINALHANDLER_500, method=method)
                response.headers["Content-Security-Policy"] = "default-src 'none'"  # finalhandler overrides helmet's CSP
                extra_headers[:] = [(k, v) for k, v in extra_headers if k.lower() != "content-security-policy"]
                await respond(response)
                return
            scope["state"]["raw_body"] = raw
            scope["state"]["parsed_body"] = parsed
            scope["state"]["query"] = parse_query(scope.get("query_string", b""))

            if method in ("GET", "HEAD") and path.startswith("/") and not path.startswith("/socket.io"):
                candidate = (PUBLIC_DIR / path.lstrip("/")).resolve()
                if candidate.is_file() and PUBLIC_DIR.resolve() in candidate.parents:
                    await respond(FileResponse(candidate))
                    return

            if method == "POST" and path in ("/chats/webhook", "/chats/line-hook"):
                allowed, limit_headers = webhook_limiter.hit(client_key(scope))
                extra_headers.extend(limit_headers)
                if not allowed:
                    await respond(send(429, "Too many requests, please try again later.", method=method))
                    return
                if path == "/chats/webhook":
                    self.on_whatsapp_webhook(parsed)
                else:
                    self.on_line_webhook(parsed)
                await respond(send_status(200))
                return

            if method == "GET" and path == "/livez":
                await respond(send(200, {"status": "ok", "ts": iso_now()}, method=method, request_headers=headers))
                return
            if method == "GET" and path == "/readyz":
                try:
                    await self.readiness()
                    await respond(send(200, {"status": "ready", "ts": iso_now()}, method=method, request_headers=headers))
                except Exception as error:  # noqa: BLE001
                    await respond(send(503, {"status": "not-ready", "error": str(error)}, method=method))
                return

            if path not in ("/", "/health", "/healthz"):
                try:
                    allowed, limit_headers = global_limiter.hit(client_key(scope))
                    extra_headers.extend(limit_headers)
                    if not allowed:
                        await respond(send(429, "Too many requests, please try again later.", method=method))
                        return
                except Exception as error:  # noqa: BLE001 - fail-open
                    print(f"global rate limiter error — allowing request: {error}", flush=True)

            api_key = headers.get("x-api-key")
            if not api_key:
                await respond(send(400, {"success": False, "message": "Api key is required"}, method=method))
                return
            if not any(is_api_key_valid(key, api_key) for key in get_settings().api_keys):
                await respond(send(400, {"success": False, "message": "Api key is invalid"}, method=method))
                return

            replayed = False

            async def replay() -> Message:
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": raw, "more_body": False}
                return await receive()

            await self.app(scope, replay, send_with_headers)
        finally:
            logger.debug(
                "http request",
                {"method": method, "path": path, "status": status_holder["status"], "durationMs": int((time.monotonic() - started) * 1000)},
            )
            request_id_var.reset(token)
            user_id_var.reset(user_token)


def public_dir() -> Path:
    return PUBLIC_DIR
