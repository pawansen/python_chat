"""Dynamic ``{{ }}`` template-field resolution for /chats/send-template-message
(port of src/utils/template.placeholder.util.js — pure logic, no I/O).

Only allowlisted keys, scoped by ``friend_type``, are resolved; every other
``{{ token }}`` is left verbatim (frontend contract). Placeholder keys never
index a DB row directly: values come from :func:`compute_field_value`'s
explicit switch, so ``{{ password }}``/``{{ token }}`` can never leak.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.js import js_string, nullish, truthy

TEMPLATE_PLACEHOLDER_REGEX = re.compile(r"{{(.*?)}}")

COMMON_FIELD_SOURCES = {"name": "sender", "mobile_number": "sender", "email": "sender"}

FRIEND_TYPE_FIELD_SOURCES: dict[str, dict[str, str]] = {
    "worker": {
        "worker_name": "friendWorker",
        "worker_surname": "friendWorker",
        "worker_mobile_number": "friendWorker",
        "worker_whatsapp_number": "friendWorker",
        "worker_email": "friendWorker",
        "hk_id": "friendWorker",
        "passport_number": "friendWorker",
        "worker_full_name": "friendView",
    },
    "employer": {
        "full_name": "friendView",
        "surname": "friendEmployer",
        "chinese_name": "friendEmployer",
        "employer_hk_id": "friendEmployer",
    },
    "user": {},
}


def get_field_source(key: str, friend_type: Any) -> str | None:
    if key in COMMON_FIELD_SOURCES:
        return COMMON_FIELD_SOURCES[key]
    scoped = FRIEND_TYPE_FIELD_SOURCES.get(friend_type) if isinstance(friend_type, str) else None
    if scoped and key in scoped:
        return scoped[key]
    return None


def extract_template_placeholder_keys(content: Any) -> list[str]:
    if not isinstance(content, str) or "{{" not in content:
        return []
    keys: list[str] = []
    for match in TEMPLATE_PLACEHOLDER_REGEX.finditer(content):
        key = match.group(1).strip()
        if key and key not in keys:
            keys.append(key)
    return keys


def _join(*parts: Any) -> str:
    return "".join("" if nullish(p) else js_string(p) for p in parts)


def _get(row: Any, key: str) -> Any:
    return row.get(key) if isinstance(row, dict) else None


def compute_field_value(key: str, sources: dict[str, Any]) -> Any:
    sender = sources.get("sender")
    worker = sources.get("friendWorker")
    employer = sources.get("friendEmployer")
    view = sources.get("friendView")
    if key == "name":
        first = _get(sender, "first_name")
        return first if not nullish(first) else _get(sender, "related_name")
    if key == "mobile_number":
        cc, mn = _get(sender, "country_code"), _get(sender, "mobile_number")
        return _join(cc, mn) if truthy(cc) or truthy(mn) else None
    if key == "email":
        return _get(sender, "email")
    if key == "worker_name":
        return _get(worker, "name")
    if key == "worker_surname":
        return _get(worker, "surname")
    if key == "worker_mobile_number":
        cc, mn = _get(worker, "country_code"), _get(worker, "mobile_number")
        return _join(cc, mn) if truthy(cc) or truthy(mn) else None
    if key == "worker_whatsapp_number":
        cc, mn = _get(worker, "whatsapp_country_code"), _get(worker, "whatsapp_number")
        return _join(cc, mn) if truthy(cc) or truthy(mn) else None
    if key == "worker_email":
        return _get(worker, "email")
    if key == "hk_id":
        return _get(worker, "hk_id")
    if key == "passport_number":
        return _get(worker, "passport_number")
    if key == "worker_full_name":
        return _get(view, "related_name")
    if key == "full_name":
        return _get(view, "related_name")
    if key == "surname":
        return _get(employer, "surname")
    if key == "chinese_name":
        return _get(employer, "chinese_name")
    if key == "employer_hk_id":
        return _get(employer, "hk_id")
    return None


def apply_template_placeholders(content: Any, values: dict[str, Any]) -> Any:
    if not isinstance(content, str) or "{{" not in content:
        return content

    def replace(match: re.Match) -> str:
        key = match.group(1).strip()
        if key not in values:
            return match.group(0)
        value = values[key]
        return "" if nullish(value) else js_string(value)

    return TEMPLATE_PLACEHOLDER_REGEX.sub(replace, content)


Fetcher = Callable[[Any], Awaitable[Any]]


async def resolve_template_placeholders(params: dict[str, Any], fetchers: dict[str, Fetcher]) -> Any:
    template_content = params.get("template_content")
    friend_type = params.get("friend_type")
    friend_id = params.get("friend_id")
    sender_user_id = params.get("sender_user_id")
    keys = extract_template_placeholder_keys(template_content)
    if not keys:
        return template_content
    supported = [(key, get_field_source(key, friend_type)) for key in keys]
    supported = [(key, source) for key, source in supported if source is not None]
    if not supported:
        return template_content
    needed = {source for _, source in supported}
    sources: dict[str, Any] = {}

    async def load(name: str, fetcher: str, ident: Any) -> None:
        sources[name] = await fetchers[fetcher](ident)

    tasks = []
    if "sender" in needed and truthy(sender_user_id):
        tasks.append(load("sender", "fetchSenderProfile", sender_user_id))
    if "friendWorker" in needed and truthy(friend_id):
        tasks.append(load("friendWorker", "fetchWorkerProfile", friend_id))
    if "friendEmployer" in needed and truthy(friend_id):
        tasks.append(load("friendEmployer", "fetchEmployerProfile", friend_id))
    if "friendView" in needed and truthy(friend_id):
        tasks.append(load("friendView", "fetchFriendView", friend_id))
    await asyncio.gather(*tasks)
    values = {key: compute_field_value(key, sources) for key, _ in supported}
    return apply_template_placeholders(template_content, values)
