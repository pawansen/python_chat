"""Repository behaviour against real MySQL (schema cloned from staging) + moto DynamoDB."""

from __future__ import annotations

import asyncio

import pytest

from app.core.js import UNDEFINED
from app.core.tasks import drain
from app.db import raw
from app.redis import cache_keys, cache_service
from app.repositories import aws_message as msgs
from app.repositories import group as group_repo
from app.repositories import group_participant as gp
from app.repositories import users as users_repo

pytestmark = pytest.mark.integration


async def test_create_group_returns_sequelize_shaped_values_and_commits():
    values = await group_repo.create_group(
        {"conversation_state": "Active", "friendly_name": "X", "unique_name": "100-201", "type": "ONE_TO_ONE",
         "platform": "app", "platform_two": "app", "friend_type": "worker", "created_by": 100}
    )
    keys = list(values)
    assert keys[:3] == ["participant_sid", "job_id", "job_worker_id"] and keys.index("id") < keys.index("conversation_state")
    row = await raw.select_one("SELECT unique_name, is_deleted, updated_at FROM `groups` WHERE id = :id", {"id": values["id"]})
    assert row["unique_name"] == "100-201" and row["is_deleted"] == 0 and row["updated_at"] is not None


async def test_transaction_rolls_back_on_error():
    with pytest.raises(RuntimeError):
        async with raw.transaction() as conn:
            created = await group_repo.create_group(
                {"conversation_state": "Active", "unique_name": "rollback-me", "type": "ONE_TO_ONE", "platform": "app",
                 "friend_type": "worker", "created_by": 1}, conn)
            assert created["id"]
            raise RuntimeError("abort")
    assert await raw.select_one("SELECT id FROM `groups` WHERE unique_name = 'rollback-me'") is None


async def test_participant_soft_delete_invalidates_cached_lookups():
    where = {"group_id": 1001, "user_id": 200, "is_deleted": 0}
    first = await gp.get_group_participant_details(where, ["id"])
    assert first and first["id"]
    row = await gp.check_user_added_in_group(1001, 200)
    await gp.update_group_participant_by_id({"is_deleted": 1, "deleted_by": 1}, row["id"])
    await drain(5)
    assert await gp.get_group_participant_details(where, ["id"]) is None  # cache dropped, DB says removed


async def test_unread_counters_and_routing_filter():
    await gp.increment_unread_count(1001, 200, 3)
    assert await gp.get_user_unread_total(200) == 3
    assert await gp.get_user_unread_total(200, "whatsapp") == 0
    await gp.reset_unread_count(1001, 200)
    assert await gp.get_user_unread_total(200) == 0
    assert gp.participant_routing_where({"group_id": 1}) == {"group_id": 1, "is_deleted": 0}


async def test_user_view_cache_and_allowlist():
    rows = await users_repo.get_user_info_in_view(100, "user_id, related_name")
    assert rows == [{"user_id": 100, "related_name": "Ka Yan Chan"}]
    assert (await users_repo.get_user_info_in_view(100, "user_id; DROP TABLE users")) == {"error": "Unsafe attributes rejected"}
    assert await users_repo.get_user_info_in_view_by_custom("password", "x") == {"error": "Invalid lookup column: password"}
    await users_repo.update_details_by_user_id(100, {"email": "changed@regress.test"})
    await drain(5)
    assert (await users_repo.get_user_info_in_view(100, "user_id, email"))[0]["email"] == "changed@regress.test"


async def test_message_lifecycle_in_dynamodb():
    created = await msgs.create_message_with_multi_language(
        {"group_id": 1001, "user_id": 100, "sender_id": 100, "receiver_id": 200, "content": "integration hi", "platform": "app"}
    )
    assert created["insert_id"] == 6 and created["content_en"] is None and created["receiver_id"] == 200
    await drain(5)
    assert await gp.get_user_unread_total(200) == 1  # recipient counter incremented
    delivered = await msgs.mark_messages_delivered(1001, 200)
    assert {m["id"] for m in delivered["updated"]} == {6}
    assert (await msgs.mark_messages_delivered(1001, 200))["updated"] == []  # never re-transitions
    page = await msgs.get_messages_with_filter_sort_pagination(1001, {"limit": 2, "ascending": False, "filters": {"platform": "app"}})
    assert [m["id"] for m in page["Items"]] == [6, 3] and page["lastEvaluatedKey"]
    read = await msgs.read_message_to_user({"read_status": "read", "updated_by": 200}, 1001, 200)
    assert read["success"]
    assert await msgs.get_unread_message_count_receiver(200, 1001) == 0
    deleted = await msgs.delete_message_to_user({"deleted_by": 100}, {"id": 6, "group_id": 1001, "user_id": 100})
    assert deleted == {"success": True, "message": "Message deleted successfully"}
    assert (await msgs.get_last_message(1001))["id"] == 3
    assert await msgs.delete_message_to_user({"deleted_by": 1}, {"id": 999, "group_id": 1001, "user_id": 1}) == {
        "success": False, "message": "No messages found for the specified criteria"}


async def test_cache_version_bump_semantics():
    key = cache_keys.version_message_key(42)
    assert await cache_service.get_version(key) == "0"
    assert await cache_service.bump_version(key) == 1
    assert await cache_service.get_version(key) == "1"
    await cache_service.set("chat:message:list:42:v1:x", "v")
    assert await cache_service.delete_conversation_list_cache(cache_keys.message_list_pattern(42)) == 1


async def test_named_replacement_missing_matches_sequelize():
    with pytest.raises(ValueError, match='Named replacement ":id" has no entry in the replacement map.'):
        await raw.select("SELECT 1 FROM users WHERE id = :id", {"id": UNDEFINED})


async def test_concurrent_unread_increments_are_atomic():
    await asyncio.gather(*(gp.increment_unread_count(1002, 10) for _ in range(20)))
    assert await gp.get_user_unread_total(10) == 20
