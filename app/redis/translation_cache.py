"""Content-addressed translation cache (port of src/services/translationCache.service.js).

Key: ``chat:translation:{sha1(text)}:{langCode}``. Immutable by construction
(a different input yields a different key), so it never needs invalidation;
the TTL (TRANSLATE_CACHE_TTL_SECONDS, default 7 days) is a size bound only.
Any Redis problem is treated as a miss / no-op.
"""

from __future__ import annotations

import hashlib
from typing import Any

from app.core.config import get_settings
from app.core.js import UNDEFINED, js_string
from app.redis import cache_keys
from app.redis import client as redis_client


def hash_text(text: Any) -> str:
    return hashlib.sha1(js_string(text).encode("utf-8")).hexdigest()


async def get_cached_translation(text_hash: str, lang_code: str) -> Any:
    """Cached string, or UNDEFINED on miss/disabled/error."""
    try:
        client = await redis_client.connect()
        if client is None:
            return UNDEFINED
        value = await client.get(cache_keys.translation_key(text_hash, lang_code))
        return UNDEFINED if value is None else value
    except Exception as err:  # noqa: BLE001 - cache must never break translation
        redis_client.mark_down()
        print(f"⚠️  translationCache get error: {err}", flush=True)
        return UNDEFINED


async def set_cached_translation(text_hash: str, lang_code: str, value: Any) -> None:
    """Null/empty translations are NOT cached so they can be retried."""
    if value is None or value is UNDEFINED or value == "":
        return
    try:
        client = await redis_client.connect()
        if client is None:
            return
        await client.set(
            cache_keys.translation_key(text_hash, lang_code),
            js_string(value),
            ex=int(get_settings().translate.cache_ttl_seconds),
        )
    except Exception as err:  # noqa: BLE001
        redis_client.mark_down()
        print(f"⚠️  translationCache set error: {err}", flush=True)
