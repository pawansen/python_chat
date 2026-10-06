"""Outbound HTTP (port of the axios usage across the service).

* one shared ``httpx.AsyncClient`` per process (connection pooling / reuse);
* every call has a deadline (default OUTBOUND_HTTP_TIMEOUT_MS, media uses
  OUTBOUND_MEDIA_TIMEOUT_MS) so no provider call can hang a worker;
* axios semantics: non-2xx raises :class:`AxiosError` carrying
  ``response.status`` / ``response.data``; JSON bodies are parsed, other
  bodies returned as text (or bytes for ``response_type="arraybuffer"``);
* redirects are followed (axios/follow-redirects default);
* the active request id is forwarded as ``X-Request-Id``.

Secrets (API keys, bearer tokens) are only ever placed in headers and are
never logged — callers log ``status`` codes only (HI-27).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.context import get_request_id

_client: httpx.AsyncClient | None = None


@dataclass
class AxiosResponse:
    status: int
    data: Any
    headers: dict[str, str] = field(default_factory=dict)


class AxiosError(Exception):
    def __init__(self, message: str, response: AxiosResponse | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.response = response
        self.code = code


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            follow_redirects=True,
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50, keepalive_expiry=30),
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _parse_body(response: httpx.Response, response_type: str) -> Any:
    if response_type == "arraybuffer":
        return response.content
    text = response.text
    if not text:
        return ""
    try:
        return json.loads(text)
    except ValueError:
        return text


async def request(
    method: str,
    url: str,
    *,
    json_body: Any = None,
    headers: dict[str, str] | None = None,
    timeout_ms: float | None = None,
    response_type: str = "json",
    validate_status: bool = True,
    max_content_length: int | None = None,
) -> AxiosResponse:
    timeout_ms = timeout_ms if timeout_ms is not None else get_settings().outbound_http_timeout_ms
    send_headers = dict(headers or {})
    request_id = get_request_id()
    if request_id and "X-Request-Id" not in send_headers:
        send_headers["X-Request-Id"] = request_id
    try:
        response = await get_client().request(
            method.upper(),
            url,
            json=json_body,
            headers=send_headers,
            timeout=timeout_ms / 1000 if timeout_ms else None,
        )
    except httpx.TimeoutException as exc:
        raise AxiosError(f"timeout of {int(timeout_ms)}ms exceeded", code="ECONNABORTED") from exc
    except httpx.HTTPError as exc:
        raise AxiosError(str(exc) or exc.__class__.__name__, code="ERR_NETWORK") from exc
    if max_content_length is not None and len(response.content) > max_content_length:
        raise AxiosError(f"maxContentLength size of {max_content_length} exceeded", code="ERR_BAD_RESPONSE")
    result = AxiosResponse(
        status=response.status_code,
        data=_parse_body(response, response_type),
        headers={k.lower(): v for k, v in response.headers.items()},
    )
    if validate_status and not (200 <= response.status_code < 300):
        raise AxiosError(f"Request failed with status code {response.status_code}", response=result, code="ERR_BAD_REQUEST")
    return result


def error_status_or_message(error: BaseException) -> Any:
    """``error.response?.status || error.message`` used in the provider logs."""
    response = getattr(error, "response", None)
    if response is not None and getattr(response, "status", None):
        return response.status
    return str(error)


def developer_message_or_message(error: BaseException) -> Any:
    """``error.response?.data?.meta?.developer_message || error?.message``."""
    response = getattr(error, "response", None)
    data = getattr(response, "data", None) if response is not None else None
    if isinstance(data, dict):
        meta = data.get("meta")
        if isinstance(meta, dict) and meta.get("developer_message"):
            return meta["developer_message"]
    return getattr(error, "message", None) or str(error)
