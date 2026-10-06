"""HTTP response helpers reproducing Express response details.

* ``res.send(object)`` -> ``application/json; charset=utf-8`` with
  JSON.stringify output and Express's weak ETag (``W/"<len hex>-<sha1 b64 27>"``);
  GET/HEAD requests carrying a matching ``If-None-Match`` get 304;
* helmet v8 default headers (CORP relaxed to cross-origin, as configured);
* ``cors`` package semantics for the configured origin list.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

from starlette.responses import Response

from app.core.config import get_settings
from app.core.jsonx import stringify

HELMET_HEADERS = [
    (
        "Content-Security-Policy",
        "default-src 'self';base-uri 'self';font-src 'self' https: data:;form-action 'self';frame-ancestors 'self';"
        "img-src 'self' data:;object-src 'none';script-src 'self';script-src-attr 'none';style-src 'self' https: 'unsafe-inline';"
        "upgrade-insecure-requests",
    ),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "cross-origin"),
    ("Origin-Agent-Cluster", "?1"),
    ("Referrer-Policy", "no-referrer"),
    ("Strict-Transport-Security", "max-age=31536000; includeSubDomains"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-DNS-Prefetch-Control", "off"),
    ("X-Download-Options", "noopen"),
    ("X-Frame-Options", "SAMEORIGIN"),
    ("X-Permitted-Cross-Domain-Policies", "none"),
    ("X-XSS-Protection", "0"),
]

CORS_METHODS = "GET,POST,PUT,PATCH,DELETE,OPTIONS"
CORS_HEADERS = "Content-Type,Authorization,authorization,x-api-key,token,hosttype,timezone,ip,device,language,Accept-Language"


def weak_etag(body: bytes) -> str:
    digest = base64.b64encode(hashlib.sha1(body).digest()).decode()[:27]
    return f'W/"{len(body):x}-{digest}"'


def cors_headers(origin: str | None) -> list[tuple[str, str]]:
    headers: list[tuple[str, str]] = []
    if origin and origin in get_settings().allowed_origins:
        headers.append(("Access-Control-Allow-Origin", origin))
    headers.append(("Vary", "Origin"))
    headers.append(("Access-Control-Allow-Credentials", "true"))
    return headers


def preflight_response(origin: str | None) -> Response:
    headers = cors_headers(origin)
    headers += [("Access-Control-Allow-Methods", CORS_METHODS), ("Access-Control-Allow-Headers", CORS_HEADERS), ("Content-Length", "0")]
    response = Response(status_code=204)
    for key, value in headers:
        response.headers.append(key, value)
    return response


def _fresh(request_headers: Any, etag: str) -> bool:
    if request_headers is None:
        return False
    inm = request_headers.get("if-none-match")
    if not inm:
        return False
    if inm.strip() == "*":
        return True
    candidates = [t.strip() for t in inm.split(",")]
    return any(c == etag or c.removeprefix("W/") == etag.removeprefix("W/") for c in candidates)


def send(status: int, body: Any, *, method: str = "GET", request_headers: Any = None, content_type: str | None = None) -> Response:
    """Express ``res.status(status).send(body)``."""
    if isinstance(body, str):
        payload = body.encode("utf-8")
        content_type = content_type or "text/html; charset=utf-8"
    elif isinstance(body, bytes):
        payload = body
        content_type = content_type or "application/octet-stream"
    else:
        payload = stringify(body).encode("utf-8")
        content_type = content_type or "application/json; charset=utf-8"
    etag = weak_etag(payload)
    if method in ("GET", "HEAD") and (200 <= status < 300 or status == 304) and _fresh(request_headers, etag):
        response = Response(status_code=304)
        response.headers["ETag"] = etag
        return response
    response = Response(content=b"" if method == "HEAD" else payload, status_code=status, media_type=None)
    response.headers["Content-Type"] = content_type
    response.headers["ETag"] = etag
    if method == "HEAD":
        response.headers["Content-Length"] = str(len(payload))
    return response


def send_status(status: int, *, method: str = "POST") -> Response:
    """Express ``res.sendStatus(code)``."""
    phrases = {200: "OK", 401: "Unauthorized", 404: "Not Found"}
    return send(status, phrases.get(status, str(status)), method=method, content_type="text/plain; charset=utf-8")


FINALHANDLER_500 = (
    '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n<title>Error</title>\n</head>\n<body>\n'
    "<pre>Internal Server Error</pre>\n</body>\n</html>\n"
)
