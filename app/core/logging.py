"""Leveled JSON-line logger (port of src/utils/logger.util.js).

Levels: error < warn < info < debug. Threshold comes from LOG_LEVEL with a
production-aware default ("info" in production, "debug" elsewhere). Each line
is one JSON object ``{ts, level, msg, reqId?, ...fields}`` so the existing
JSON-parsing log tooling keeps working. Values that look like secrets are
never logged: callers pass ids, never tokens/passwords (HI-27), and
:func:`redact` scrubs known secret field names defensively.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from app.core.context import get_request_id, user_id_var
from app.core.js import iso_now
from app.core.jsonx import stringify

LEVELS = {"error": 0, "warn": 1, "info": 2, "debug": 3}
_SECRET_KEYS = {
    "token",
    "password",
    "secret",
    "authorization",
    "api_key",
    "apikey",
    "x-api-key",
    "access_token",
    "accesstoken",
    "private_key",
    "jwt",
}


def _threshold() -> int:
    configured = (os.environ.get("LOG_LEVEL") or ("info" if os.environ.get("NODE_ENV") == "production" else "debug")).lower()
    return LEVELS.get(configured, LEVELS["info"])


def redact(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if key.lower() in _SECRET_KEYS:
            out[key] = "[redacted]"
        elif isinstance(value, dict):
            out[key] = redact(value)
        else:
            out[key] = value
    return out


class Logger:
    def __init__(self) -> None:
        self._threshold = _threshold()

    @property
    def level(self) -> str:
        return next((k for k, v in LEVELS.items() if v == self._threshold), "info")

    def reconfigure(self) -> None:
        self._threshold = _threshold()

    def _emit(self, level: str, msg: str, fields: Any = None) -> None:
        if LEVELS[level] > self._threshold:
            return
        line: dict[str, Any] = {"ts": iso_now(), "level": level, "msg": msg}
        req_id = get_request_id()
        if req_id:
            line["reqId"] = req_id
        user_id = user_id_var.get()
        if user_id:
            line["userId"] = user_id
        if fields is not None:
            if isinstance(fields, BaseException):
                line["error"] = str(fields)
            elif isinstance(fields, dict):
                line.update(redact(fields))
            else:
                line["detail"] = fields
        try:
            text = stringify(line)
        except Exception:  # pragma: no cover - degrade instead of raising from a log call
            text = f"{line['ts']} {level} {msg}"
        stream = sys.stderr if level in ("error", "warn") else sys.stdout
        print(text, file=stream, flush=True)

    def error(self, msg: str, fields: Any = None) -> None:
        self._emit("error", msg, fields)

    def warn(self, msg: str, fields: Any = None) -> None:
        self._emit("warn", msg, fields)

    warning = warn

    def info(self, msg: str, fields: Any = None) -> None:
        self._emit("info", msg, fields)

    def debug(self, msg: str, fields: Any = None) -> None:
        self._emit("debug", msg, fields)


logger = Logger()


def component_log(component: str):
    """Structured ``{ts, component, event, ...}`` lines used by the queue/worker/translate modules."""

    def log(level: str, event: str, **fields: Any) -> None:
        line = {"ts": iso_now(), "component": component, "event": event, **redact(fields)}
        stream = sys.stderr if level == "error" else sys.stdout
        print(stringify(line), file=stream, flush=True)

    return log


def configure_stdlib_logging() -> None:
    """Route library loggers (uvicorn, engineio, botocore) through stdlib at WARNING."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # Windows pipes default to cp1252
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for noisy in ("botocore", "boto3", "urllib3", "engineio.server", "socketio.server", "httpx", "aiomysql"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
