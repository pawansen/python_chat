"""Request-scoped correlation context (port of src/utils/request.context.js).

A ``ContextVar`` replaces Node's ``AsyncLocalStorage``: the request-id
middleware sets it per HTTP request so log lines (and outbound calls) can pick
it up without threading it through every function.
"""

from __future__ import annotations

from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)


def get_request_id() -> str | None:
    return request_id_var.get()
