"""Shared helpers for the chat REST services (from the top of aws.service.js)."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from hashids import Hashids

from app.core.config import get_settings
from app.core.errors import StatusError
from app.core.js import UNDEFINED, js_string, truthy

END_USER_GUARDS = ("employer", "worker")
_hashids = Hashids(salt="G7k9B2pQ5dH8X1vR", min_length=8, alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890")
_DIGITS = re.compile(r"^\d+$")


def sha1(value: Any) -> str:
    return hashlib.sha1(js_string(value).encode("utf-8")).hexdigest()


def hashids_encode(value: Any) -> str:
    """JS ``hashids.encode(x)``: numbers / numeric strings encode; anything else -> ""."""
    if isinstance(value, bool) or value is None or value is UNDEFINED:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, str) and _DIGITS.match(value):
        value = int(value)
    if isinstance(value, int) and value >= 0:
        return _hashids.encode(value)
    return ""


def is_end_user(req) -> bool:
    details = req.user_details if isinstance(req.user_details, dict) else {}
    return details.get("role_guard_name") in END_USER_GUARDS


def require_staff(req) -> None:
    if is_end_user(req):
        raise StatusError.forbidden("You are not authorized to access this resource.")


def resolve_scoped_user_id(req, requested: Any) -> Any:
    return req.user_details["id"] if is_end_user(req) else requested


def build_duplicate_group_filter(unique_name: Any, platform: Any) -> dict[str, Any]:
    if platform == "action_required":
        return {"unique_name": unique_name, "platform_two": platform}
    if platform == "whatsapp":
        return {"unique_name": unique_name, "platform": platform, "platform_two": platform}
    return {"unique_name": unique_name, "platform": platform}


def resolve_profile_image(profile_image: Any) -> Any:
    settings = get_settings()
    if not truthy(profile_image):
        return settings.default_profile_image
    if isinstance(profile_image, str) and profile_image.startswith("http"):
        return profile_image
    return js_string(settings.aws_s3_base_url) + js_string(profile_image)


def profile_image_inline(profile_image: Any) -> Any:
    """``p && !p.startsWith('http') ? s3 + p : (p ? p : default)``."""
    settings = get_settings()
    if truthy(profile_image) and not (isinstance(profile_image, str) and profile_image.startswith("http")):
        return js_string(settings.aws_s3_base_url) + js_string(profile_image)
    return profile_image if truthy(profile_image) else settings.default_profile_image


def s3_prefixed(profile_image: Any) -> Any:
    """``p ? s3 + "" + p : default`` (webhook / template paths)."""
    settings = get_settings()
    return js_string(settings.aws_s3_base_url) + js_string(profile_image) if truthy(profile_image) else settings.default_profile_image


async def map_with_concurrency(items: Iterable[Any], concurrency: int, mapper: Callable[[Any], Awaitable[Any]]) -> list[Any]:
    items = list(items)
    results: list[Any] = [None] * len(items)
    semaphore = asyncio.Semaphore(max(1, min(concurrency, len(items) or 1)))

    async def run(index: int, item: Any) -> None:
        async with semaphore:
            results[index] = await mapper(item)

    await asyncio.gather(*(run(i, item) for i, item in enumerate(items)))
    return results


def utcnow() -> datetime:
    """``new Date()`` for values written to MySQL (naive UTC; Sequelize timezone +00:00)."""
    return datetime.now(UTC).replace(tzinfo=None)


def now_aware() -> datetime:
    """``new Date()`` for values serialised into responses/payloads."""
    return datetime.now(UTC)


def first(rows: Any) -> Any:
    """``rows[0]`` with JS semantics (undefined for empty / non-list)."""
    if isinstance(rows, list) and rows:
        return rows[0]
    return UNDEFINED


def length(value: Any) -> Any:
    """``value.length`` (undefined for objects without length)."""
    if isinstance(value, list | str):
        return len(value)
    return UNDEFINED
