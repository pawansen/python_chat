"""Queues, rate limiters, invalidation bus, LCC OTA, DynamoDB marshalling (no network)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.api.pipeline import BodyError, FixedWindowLimiter, parse_body, parse_query
from app.dynamodb.client import deserialize_item, serialize_item
from app.lcc_ota import (
    OtaConfig,
    StateStore,
    build_manifest_url,
    extract_namespace,
    has_changed,
    merge_catalog,
    read_config,
    run_refresh,
)
from app.queues import translation_queue
from app.redis import invalidation_bus
from app.socket.rate_limiter import SocketRateLimiter


def test_socket_rate_limiter_windows():
    limiter = SocketRateLimiter()
    assert all(limiter.allow("send_message") for _ in range(30))
    assert not limiter.allow("send_message")
    assert limiter.allow("chat_message_read")  # other event, global budget left
    payload = limiter.exceeded_payload("send_message")
    assert payload == {"status": False, "event": "send_message", "message": "Too many requests. Please slow down.", "retry_after_ms": 10000}


def test_http_rate_limiter_headers():
    limiter = FixedWindowLimiter(lambda: 2)
    ok, headers = limiter.hit("1.2.3.4")
    assert ok and dict(headers)["RateLimit-Remaining"] == "1" and dict(headers)["RateLimit-Policy"] == "2;w=60"
    limiter.hit("1.2.3.4")
    ok, headers = limiter.hit("1.2.3.4")
    assert not ok and "Retry-After" in dict(headers)


def test_body_and_query_parsing_like_express():
    assert parse_body("application/json", b"") == {}
    assert parse_body("application/json; charset=utf-8", b'{"a":1}') == {"a": 1}
    assert parse_body("text/plain", b"x") is None
    assert parse_body("application/x-www-form-urlencoded", b"a=1&a=2") == {"a": "2"}  # hpp: last value
    with pytest.raises(BodyError):
        parse_body("application/json", b'"x"')  # strict mode
    with pytest.raises(BodyError):
        parse_body("application/json", b"{NaN}")
    with pytest.raises(BodyError) as too_big:
        parse_body("application/json", b"{" + b" " * (101 * 1024) + b"}")
    assert too_big.value.status == 413
    assert parse_query(b"a=1&a=2&b=3") == {"a": ["1", "2"], "b": "3"}  # Express 5: hpp is a no-op on req.query


def test_dynamodb_marshalling_round_trip():
    item = {"id": 5, "rt": 0.25, "content": "hi", "nothing": None, "nested": {"a": [1, 2.5]}}
    serialized = serialize_item(item)
    assert serialized["rt"] == {"N": "0.25"} and serialized["nothing"] == {"NULL": True}
    assert deserialize_item(serialized) == item
    assert deserialize_item({"n": {"N": "12"}}) == {"n": 12}
    with pytest.raises(TypeError):
        serialize_item({"bad": float("nan")})  # aws-sdk rejected NaN too


async def test_translation_queue_dedupes_and_retries(monkeypatch):
    translation_queue.reset_for_tests()
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings().translate, "job_retries", 1)
    monkeypatch.setattr(get_settings().translate, "job_backoff_ms", 10)
    calls: list[int] = []

    async def processor(job):
        calls.append(job["messageId"])
        if len(calls) == 1:
            raise RuntimeError("first attempt fails")

    translation_queue.set_translation_processor(processor)
    translation_queue.queue_translation({"messageId": 1, "content": "hi", "createdAt": "x", "groupId": 1})
    translation_queue.queue_translation({"messageId": 1, "content": "hi", "createdAt": "x", "groupId": 1})  # deduped
    translation_queue.queue_translation({"messageId": 2, "content": "", "createdAt": "x", "groupId": 1})  # no content
    await asyncio.sleep(0.2)
    stats = translation_queue.get_queue_stats()
    assert calls == [1, 1] and stats["processed"] == 1 and stats["retried"] == 1
    translation_queue.reset_for_tests()


class StubCache:
    def __init__(self):
        self.calls = []

    async def delete_conversation_list_cache(self, pattern):
        self.calls.append(("del", pattern))
        return 1

    async def bump_version(self, key):
        self.calls.append(("bump", key))
        return 1


async def test_invalidation_bus_handlers():
    stub = StubCache()
    handlers = invalidation_bus.create_handlers(stub)
    assert await invalidation_bus.dispatch_invalidation_event({"v": 1, "event": "auth.user_changed", "data": {"user_id": "42"}}, handlers)
    assert stub.calls == [("del", "chat:user:detail:42:*"), ("bump", "chat:version:conversation:42")]
    stub.calls.clear()
    assert await invalidation_bus.dispatch_invalidation_event({"v": 1, "event": "auth.logout", "data": {"user_id": 7}}, handlers)
    assert stub.calls == [("del", "chat:user:detail:7:*")]
    stub.calls.clear()
    assert await invalidation_bus.dispatch_invalidation_event({"v": 1, "event": "auth.user_changed", "data": {"user_id": "4*"}}, handlers)
    assert stub.calls == []  # SCAN metacharacters can never reach a pattern
    assert not await invalidation_bus.dispatch_invalidation_event({"v": 2, "event": "auth.logout"}, handlers)
    assert not await invalidation_bus.dispatch_invalidation_event({"v": 1, "event": "unknown"}, handlers)
    assert invalidation_bus.normalize_user_id(True) is None and invalidation_bus.normalize_user_id(5.0) == "5"


def _config(tmp_path: Path) -> OtaConfig:
    locales = tmp_path / "locales"
    locales.mkdir()
    (locales / "en.json").write_text(json.dumps({"A": "old", "KEEP": "k"}, indent=4) + "\n", encoding="utf-8")
    return OtaConfig(enabled=True, environment="staging", base_url="https://bucket", poll_seconds=0, timeout_ms=1000,
                     cache_dir=str(tmp_path / "cache"), locales_dir=str(locales))


def _transport(responses: dict):
    async def transport(url, options):
        status, body, headers = responses[url]
        return {"status": status, "body": body, "headers": headers}

    return transport


async def test_lcc_ota_applies_and_detects_changes(tmp_path):
    config = _config(tmp_path)
    payload = json.dumps({"strings": {"chat.A": "new", "chat.B": "added", "other.X": "ignored"}}).encode()
    sha = hashlib.sha256(payload).hexdigest()
    manifest = json.dumps({"version": 3, "languages": {"en": {"url": "https://bucket/en.json", "sha256": sha}, "fr": {"url": "x"}}}).encode()
    responses = {build_manifest_url(config): (200, manifest, {"etag": "e1"}), "https://bucket/en.json": (200, payload, {})}
    state = StateStore(config.cache_dir)
    result = await run_refresh(config, _transport(responses), state)
    assert result["applied"] and result["languages"][0]["updated"] == 1 and result["languages"][0]["added"] == 1
    written = json.loads((Path(config.locales_dir) / "en.json").read_text(encoding="utf-8"))
    assert list(written) == ["A", "KEEP", "B"] and written["A"] == "new"
    again = await run_refresh(config, _transport(responses), state)
    assert again["reason"] == "up-to-date"


async def test_lcc_ota_failure_paths_keep_strings(tmp_path):
    config = _config(tmp_path)
    manifest = json.dumps({"version": 1, "languages": {"en": {"url": "https://bucket/en.json", "sha256": "deadbeef"}}}).encode()
    responses = {build_manifest_url(config): (200, manifest, {}), "https://bucket/en.json": (200, b'{"strings":{"chat.A":"x"}}', {})}
    result = await run_refresh(config, _transport(responses), StateStore(config.cache_dir))
    assert not result["ok"] and "sha256 mismatch" in result["reason"]
    assert json.loads((Path(config.locales_dir) / "en.json").read_text())["A"] == "old"
    bad = {build_manifest_url(config): (500, b"", {})}
    assert not (await run_refresh(config, _transport(bad), StateStore(config.cache_dir)))["ok"]


def test_lcc_ota_pure_helpers():
    assert extract_namespace({"chat.A": "1", "chatter.B": "2", "chat.": "3", "chat.N": 5}) == {"A": "1"}
    assert merge_catalog({"a": 1, "b": 2}, {"b": 2, "c": 3})["merged"] == {"a": 1, "b": 2, "c": 3}
    assert has_changed({"version": 2}, {}, {"version": 1}) and not has_changed({"version": 1}, {"en": {"sha256": "x"}}, {"version": 1, "sha256": {"en": "x"}})
    cfg = read_config({"LANGUAGE_PULL_FROM_SERVER": "live", "LCC_OTA_BASE_URL": "https://b///", "LCC_OTA_ENABLED": "off"})
    assert build_manifest_url(cfg) == "https://b/i18n/live/admin-backend/manifest.json" and cfg.enabled is False


def test_decimal_round_trip_types():
    assert deserialize_item({"x": {"N": "1.50"}}) == {"x": 1.5}
    assert isinstance(serialize_item({"x": Decimal("1")})["x"]["N"], str)
