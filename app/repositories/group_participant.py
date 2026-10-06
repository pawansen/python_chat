"""group_participants repository (port of src/repository/group.participant.repository.js).

Return convention preserved: failures return ``{"error": msg}``, lookups
return a row dict / list or ``None``. Participant caches are group-scoped
(``chat:participant:{detail|list}:{gid}:...``) and invalidated after every
write that can change them (fire-and-forget, idempotent, fail-open).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import get_settings
from app.core.js import is_nan, js_string, nullish, number
from app.core.jsonx import stringify
from app.core.tasks import spawn
from app.db import raw
from app.models import build_values, group_participants
from app.redis import cache_keys, cache_service
from app.repositories._sql import attrs_sql, error, insert_sql, set_sql, where_sql


def _participant_info_key(where: dict[str, Any], attributes: list[Any]) -> str:
    return cache_keys.participant_detail_key(
        js_string(where.get("group_id")),
        js_string(where.get("user_id")),
        cache_keys.cache_key_hash({"where": where, "attributes": attributes}),
    )


def _participant_list_key(where: dict[str, Any], attributes: list[Any]) -> str:
    return cache_keys.participant_list_key(
        js_string(where.get("group_id")), cache_keys.cache_key_hash({"where": where, "attributes": attributes})
    )


def invalidate_participant_caches(group_id: Any, user_ids: Iterable[Any] = ()) -> None:
    """Drop every cached participant entry of a group and bump affected users' list versions."""
    try:
        if nullish(group_id):
            return
        gid = js_string(group_id)
        cache_service.delete_pattern_bg(cache_keys.participant_list_pattern(gid))
        cache_service.delete_pattern_bg(cache_keys.participant_detail_pattern(gid))
        for user_id in user_ids:
            if not nullish(user_id):
                cache_service.bump_version_bg(cache_keys.version_conversation_key(user_id))
    except Exception:  # noqa: BLE001 - invalidation must never break the write path
        pass


def invalidate_participant_caches_bulk(rows: Any) -> None:
    try:
        if not isinstance(rows, list):
            return
        by_group: dict[str, list[Any]] = {}
        for row in rows:
            if not isinstance(row, dict) or nullish(row.get("group_id")):
                continue
            users = by_group.setdefault(js_string(row["group_id"]), [])
            if not nullish(row.get("user_id")) and row["user_id"] not in users:
                users.append(row["user_id"])
        for gid, users in by_group.items():
            invalidate_participant_caches(gid, users)
    except Exception:  # noqa: BLE001
        pass


def participant_routing_where(base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Phase 8: routing/receiving reads exclude soft-deleted participants (flag-gated)."""
    base = dict(base or {})
    if get_settings().exclude_removed_participants:
        base["is_deleted"] = 0
    return base


async def _insert(data: dict[str, Any], conn: AsyncConnection | None) -> dict[str, Any]:
    values = build_values(group_participants, data)
    insert_values = {k: v for k, v in values.items() if not (k == "id" and v is None)}
    params: dict[str, Any] = {}
    new_id = await raw.insert(insert_sql("group_participants", insert_values, params), params, conn=conn)
    values["id"] = new_id
    return values


async def create_group_participant_insert(data: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await _insert(data, None)
        invalidate_participant_caches(data.get("group_id"), [data.get("user_id")])
        return result
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def create_group_participant(data: dict[str, Any], conn: AsyncConnection | None = None) -> dict[str, Any]:
    try:
        result = await _insert(data, conn)
        invalidate_participant_caches(data.get("group_id"), [data.get("user_id")])
        return result
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def bulk_create_group_participants(rows: list[dict[str, Any]], conn: AsyncConnection | None = None) -> Any:
    try:
        results = [await _insert(row, conn) for row in rows]
        invalidate_participant_caches_bulk(rows)
        return results
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def bulk_create_group_participants_custom(rows: list[dict[str, Any]]) -> Any:
    try:
        async with raw.transaction() as conn:
            results = [await _insert(row, conn) for row in rows]
        invalidate_participant_caches_bulk(rows)
        return results
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def get_all_groups_ids_by_user(user_id: Any) -> Any:
    try:
        rows = await raw.select("SELECT `group_id` FROM `group_participants` WHERE `user_id` = :u", {"u": user_id})
        return [row["group_id"] for row in rows]
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def group_participants_count(group_id: Any) -> Any:
    try:
        row = await raw.select_one(
            "SELECT COUNT(`id`) AS `count` FROM `group_participants` WHERE `group_id` = :g", {"g": group_id}
        )
        return int(row["count"]) if row else 0
    except Exception as exc:  # noqa: BLE001
        return error(exc)


_FULL_ROW = (
    "`id`, `role_sid`, `messaging_binding`, `identity`, `group_id`, `user_id`, `account_sid`, `role_id`, "
    "`last_read_message_index`, `last_read_timestamp`, `unread_messages_count`, `is_host`, `last_seen_on`, "
    "`joined_on`, `is_deleted`, `deleted_by`, `deleted_at`, `created_at`, `created_by`, `updated_at`, `updated_by`"
)


async def check_user_added_in_group(group_id: Any, participant_id: Any) -> Any:
    try:
        return await raw.select_one(
            f"SELECT {_FULL_ROW} FROM `group_participants` WHERE `group_id` = :g AND `user_id` = :u AND `is_deleted` = 0 LIMIT 1",
            {"g": group_id, "u": participant_id},
        )
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def update_group_participant_by_id(data: dict[str, Any], row_id: Any) -> Any:
    """Participant REMOVE path (soft delete) — invalidates the group's caches after the write."""
    try:
        params: dict[str, Any] = {"pk": row_id}
        result = await raw.execute(f"UPDATE `group_participants` SET {set_sql(data, params)} WHERE `id` = :pk", params)

        async def _invalidate() -> None:
            row = await raw.select_one(
                "SELECT `group_id`, `user_id` FROM `group_participants` WHERE `id` = :pk", {"pk": row_id}
            )
            if row:
                invalidate_participant_caches(row["group_id"], [row["user_id"]])

        spawn(_invalidate())
        return [result]
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def update_by_conversation_id_user_id(data: dict[str, Any], conversation_sid: Any, user_id: Any) -> Any:
    try:
        params: dict[str, Any] = {"cs": conversation_sid, "u": user_id}
        result = await raw.execute(
            f"UPDATE `group_participants` SET {set_sql(data, params)} WHERE `conversation_sid` = :cs AND `user_id` = :u",
            params,
        )

        async def _invalidate() -> None:
            rows = await raw.select(
                "SELECT `group_id`, `user_id` FROM `group_participants` WHERE `conversation_sid` = :cs AND `user_id` = :u",
                {"cs": conversation_sid, "u": user_id},
            )
            invalidate_participant_caches_bulk(rows)

        spawn(_invalidate())
        return [result]
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def increment_unread_count(group_id: Any, user_id: Any, by: int = 1) -> Any:
    """Atomic ``unread_messages_count = unread_messages_count + by`` for a live participant row."""
    try:
        if nullish(group_id) or nullish(user_id) or is_nan(number(user_id)):
            return {"error": "invalid key"}
        await raw.execute(
            "UPDATE `group_participants` SET `unread_messages_count` = `unread_messages_count` + :by "
            "WHERE `group_id` = :g AND `user_id` = :u AND `is_deleted` = 0",
            {"by": by, "g": number(group_id), "u": number(user_id)},
        )
        return True
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def reset_unread_count(group_id: Any, user_id: Any) -> Any:
    try:
        await raw.execute(
            "UPDATE `group_participants` SET `unread_messages_count` = 0 WHERE `group_id` = :g AND `user_id` = :u",
            {"g": number(group_id), "u": number(user_id)},
        )
        return True
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def set_last_read_watermark(group_id: Any, user_id: Any) -> Any:
    """BA-H3 read watermark (raw SQL: ``last_read_at`` is not a declared model column)."""
    try:
        if nullish(group_id) or nullish(user_id):
            return {"error": "invalid key"}
        await raw.execute(
            "UPDATE group_participants SET last_read_at = NOW() WHERE group_id = :group_id AND user_id = :user_id AND is_deleted = 0",
            {"group_id": number(group_id), "user_id": number(user_id)},
        )
        return True
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def get_user_unread_total(user_id: Any, platform: Any = None) -> int:
    """O(1) global unread badge: SUM of the user's per-conversation counters."""
    try:
        params: dict[str, Any] = {"user_id": number(user_id)}
        sql = "SELECT COALESCE(SUM(gp.unread_messages_count), 0) AS total_count FROM group_participants gp "
        if platform:
            sql += "INNER JOIN `groups` g ON g.id = gp.group_id AND g.is_deleted = 0 "
        sql += "WHERE gp.user_id = :user_id AND gp.is_deleted = 0"
        if platform:
            sql += " AND g.platform = :platform"
            params["platform"] = platform
        rows = await raw.select(sql, params)
        value = number(rows[0]["total_count"]) if rows else 0
        return 0 if is_nan(value) or not value else int(value)
    except Exception as exc:  # noqa: BLE001
        print(f"getUserUnreadTotal error: {exc}", flush=True)
        return 0


async def check_pid(group_id: Any, participant_id: Any, participant_sid: Any) -> Any:
    try:
        return await raw.select_one(
            f"SELECT {_FULL_ROW} FROM `group_participants` WHERE `group_id` = :g AND `user_id` = :u "
            "AND `participant_sid` = :p AND `is_deleted` = 0 LIMIT 1",
            {"g": group_id, "u": participant_id, "p": participant_sid},
        )
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def check_valid_psid(conversation_sid: Any, participant_sid: Any) -> Any:
    try:
        return await raw.select_one(
            "SELECT `id` FROM `group_participants` WHERE `conversation_sid` = :c AND `participant_sid` = :p "
            "AND `is_deleted` = 0 LIMIT 1",
            {"c": conversation_sid, "p": participant_sid},
        )
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def get_frind_participant_details(user_id: Any, conversation_sid: Any) -> Any:
    try:
        return await raw.select_one(
            "SELECT `user_id` FROM `group_participants` WHERE `conversation_sid` = :c AND `user_id` != :u "
            "AND `is_deleted` = 0 LIMIT 1",
            {"c": conversation_sid, "u": user_id},
        )
    except Exception as exc:  # noqa: BLE001
        print(str(exc), flush=True)
        return error(exc)


async def check_valid_participant_by_sid(conversation_sid: Any, user_id: Any) -> Any:
    """CR-08 IDOR guard: the sid IS the group id. Fails closed (None) on error."""
    try:
        if not conversation_sid or not user_id:
            return None
        return await raw.select_one(
            f"SELECT {_FULL_ROW} FROM `group_participants` WHERE `group_id` = :g AND `user_id` = :u AND `is_deleted` = 0 LIMIT 1",
            {"g": conversation_sid, "u": user_id},
        )
    except Exception as exc:  # noqa: BLE001
        print(f"checkValidParticipantBySid error: {exc}", flush=True)
        return None


async def get_group_participant_details(where: dict[str, Any], attributes: list[Any]) -> Any:
    try:
        key = _participant_info_key(where, attributes)
        cached = await cache_service.get(key)
        if cached:
            return json.loads(cached)
        params: dict[str, Any] = {}
        result = await raw.select_one(
            f"SELECT {attrs_sql(attributes)} FROM `group_participants` WHERE {where_sql(where, params)} LIMIT 1", params
        )
        await cache_service.set(key, stringify(result), get_settings().redis.cache_ttl_long)
        return result
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def get_group_participants_list(where: dict[str, Any], attributes: list[Any]) -> Any:
    """Participants + ``usersModel.*`` (INNER JOIN users), flattened like Sequelize raw includes."""
    try:
        key = _participant_list_key(where, attributes)
        cached = await cache_service.get(key)
        if cached:
            return json.loads(cached)
        params: dict[str, Any] = {}
        sql = (
            f"SELECT {attrs_sql(attributes, 'groupParticipantModel')}, "
            "`usersModel`.`id` AS `usersModel.id`, `usersModel`.`role_id` AS `usersModel.role_id`, "
            "`usersModel`.`mobile_number` AS `usersModel.mobile_number`, "
            "`usersModel`.`country_code` AS `usersModel.country_code` "
            "FROM `group_participants` AS `groupParticipantModel` "
            "INNER JOIN `users` AS `usersModel` ON `groupParticipantModel`.`user_id` = `usersModel`.`id` "
            f"WHERE {where_sql(where, params, 'groupParticipantModel')}"
        )
        result = await raw.select(sql, params)
        await cache_service.set(key, stringify(result), get_settings().redis.cache_ttl_long)
        return result
    except Exception as exc:  # noqa: BLE001
        return error(exc)
