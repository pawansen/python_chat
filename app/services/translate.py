"""Resilient translation client (port of src/utils/translate.util.js).

``GET {TRANSLATE_API_URL}?text=...&target_language=...`` with header
``api-key``, per target language, in parallel. Off the hot path (called by the
translation worker). Features preserved: per-request timeout, retry with
exponential backoff, a shared circuit breaker (open after N consecutive
failures, half-open probe after the cooldown), in-flight de-duplication,
Redis content-hash cache, partial-failure tolerance (a failed language is
``None``; the call never raises).

Response shape is unchanged: ``{en, id, zh_cn, zh_tw, source_language}``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import quote

from app.core.config import TranslateTarget, get_settings
from app.core.http import request
from app.core.js import UNDEFINED, js_string
from app.core.logging import component_log
from app.redis.translation_cache import get_cached_translation, hash_text, set_cached_translation

log = component_log("translate")

_breaker = {"failures": 0, "state": "closed", "opened_at": 0.0}
_in_flight: dict[str, asyncio.Future] = {}


def _cfg():
    return get_settings().translate


def _now_ms() -> float:
    return time.monotonic() * 1000


def breaker_blocks() -> bool:
    if _breaker["state"] == "open":
        if _now_ms() - _breaker["opened_at"] >= _cfg().breaker_cooldown_ms:
            _breaker["state"] = "half-open"
            log("info", "breaker_half_open")
            return False
        return True
    return False


def breaker_on_success() -> None:
    if _breaker["state"] != "closed" or _breaker["failures"] != 0:
        log("info", "breaker_closed")
    _breaker["failures"] = 0
    _breaker["state"] = "closed"


def breaker_on_failure() -> None:
    _breaker["failures"] += 1
    if _breaker["failures"] >= _cfg().breaker_threshold and _breaker["state"] != "open":
        _breaker["state"] = "open"
        _breaker["opened_at"] = _now_ms()
        log("error", "breaker_open", failures=_breaker["failures"], cooldownMs=_cfg().breaker_cooldown_ms)


def _encode_uri_component(text: str) -> str:
    return quote(text, safe="-_.!~*'()")


async def call_translate_api(text: str, api_name: str) -> dict[str, Any]:
    cfg = _cfg()
    max_attempts = int(cfg.retries) + 1
    last_error: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        if breaker_blocks():
            raise RuntimeError("translate circuit breaker open")
        try:
            response = await request(
                "get",
                f"{cfg.base_url}?text={_encode_uri_component(js_string(text))}&target_language={api_name}",
                headers={"api-key": cfg.api_key},
                timeout_ms=cfg.timeout_ms,
            )
            breaker_on_success()
            data = response.data if isinstance(response.data, dict) else {}
            return {"translated_text": data.get("translated_text"), "source_language": data.get("source_language")}
        except Exception as error:  # noqa: BLE001
            last_error = error
            breaker_on_failure()
            if attempt < max_attempts:
                backoff = min(cfg.backoff_base_ms * 2 ** (attempt - 1), cfg.backoff_max_ms)
                log("error", "translate_retry", lang=api_name, attempt=attempt, backoff=backoff, error=str(error))
                await asyncio.sleep(backoff / 1000)
    raise last_error or RuntimeError("translate failed")


async def translate_one(text: str, target: TranslateTarget, text_hash: str) -> dict[str, Any]:
    cached = await get_cached_translation(text_hash, target.code)
    if cached is not UNDEFINED:
        return {"value": cached, "source_language": None}
    dedupe_key = f"{target.code}:{text_hash}"
    existing = _in_flight.get(dedupe_key)
    if existing is not None:
        return await asyncio.shield(existing)

    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    _in_flight[dedupe_key] = future
    try:
        try:
            result = await call_translate_api(text, target.name)
            translated = result.get("translated_text")
            if translated:
                await set_cached_translation(text_hash, target.code, translated)
            outcome = {"value": translated if translated is not None else None, "source_language": result.get("source_language")}
        except Exception as error:  # noqa: BLE001
            log("error", "translate_lang_failed", lang=target.name, error=str(error))
            outcome = {"value": None, "source_language": None}
        future.set_result(outcome)
        return outcome
    finally:
        _in_flight.pop(dedupe_key, None)


async def translate_text(text: Any) -> dict[str, Any]:
    started = time.monotonic()
    text_hash = hash_text(text)
    targets = _cfg().targets
    settled = await asyncio.gather(*(translate_one(js_string(text), target, text_hash) for target in targets))
    results: dict[str, Any] = {}
    source_language = "en"
    succeeded = 0
    for target, outcome in zip(targets, settled, strict=True):
        value = outcome.get("value")
        results[target.code] = value if value is not None else None
        if value:
            succeeded += 1
        if outcome.get("source_language"):
            source_language = outcome["source_language"]
    results["source_language"] = source_language
    log(
        "info",
        "translate_done",
        languages=len(targets),
        succeeded=succeeded,
        failed=len(targets) - succeeded,
        ms=int((time.monotonic() - started) * 1000),
    )
    return results
