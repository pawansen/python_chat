"""groups repository (port of src/repository/group.repository.js).

Primary-key detail lookups are cached under
``chat:group:detail:{group_id}:{attrsHash}`` and dropped by
``update_group_by_id`` (which runs on every message send and moves the
WhatsApp 24h-window fields). Non-id lookups use the TTL-bounded
``:filter:{hash}`` scope.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import get_settings
from app.core.js import js_string, nullish
from app.core.jsonx import stringify
from app.db import raw
from app.models import build_values, groups
from app.redis import cache_keys, cache_service
from app.repositories._sql import attrs_sql, error, insert_sql, set_sql, where_sql

DUPLICATE_ATTRS = [
    "id",
    ["id", "conversation_sid"],
    "friend_type",
    "platform",
    "type",
    "friendly_name",
    "unique_name",
    "platform_two",
    "created_at",
]


def _group_info_key(where: dict[str, Any], attributes: list[Any]) -> str:
    keys = list(where.keys()) if isinstance(where, dict) else []
    if len(keys) == 1 and keys[0] == "id" and not nullish(where.get("id")):
        return cache_keys.group_detail_key(js_string(where["id"]), cache_keys.cache_key_hash(attributes))
    return cache_keys.group_detail_filter_key(cache_keys.cache_key_hash({"where": where, "attributes": attributes}))


async def create_group(data: dict[str, Any], conn: AsyncConnection | None = None) -> dict[str, Any]:
    """``groupModel.create(data)``: returns the Sequelize-shaped dataValues (with ``id``)."""
    try:
        values = build_values(groups, data)
        insert_values = {k: v for k, v in values.items() if not (k == "id" and v is None)}
        params: dict[str, Any] = {}
        values["id"] = await raw.insert(insert_sql("groups", insert_values, params), params, conn=conn)
        return values
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def get_details_by_group_id(group_id: Any) -> Any:
    try:
        return await raw.select_one(
            "SELECT `id`, `chat_service_sid` FROM `groups` WHERE `id` = :id AND `is_deleted` = 0 LIMIT 1", {"id": group_id}
        )
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def check_valid_sid(conversation_sid: Any) -> Any:
    """Selects ``messaging_service_sid`` exactly like the Node code (column absent in the
    current schema, so this returns ``{error}`` — a truthy value — just as Sequelize did)."""
    try:
        return await raw.select_one(
            "SELECT `id`, `chat_service_sid`, `messaging_service_sid` FROM `groups` "
            "WHERE `conversation_sid` = :cs AND `is_deleted` = 0 LIMIT 1",
            {"cs": conversation_sid},
        )
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def update_group_by_id(data: dict[str, Any], group_id: Any) -> Any:
    try:
        params: dict[str, Any] = {"pk": group_id}
        result = await raw.execute(f"UPDATE `groups` SET {set_sql(data, params)} WHERE `id` = :pk", params)
        if not nullish(group_id):
            cache_service.delete_pattern_bg(cache_keys.group_detail_pattern(js_string(group_id)))
        return [result]
    except Exception as exc:  # noqa: BLE001
        print(str(exc), flush=True)
        return error(exc)


async def _find_one(where_clause: str, params: dict[str, Any], attributes: list[Any] = DUPLICATE_ATTRS) -> Any:
    return await raw.select_one(f"SELECT {attrs_sql(attributes)} FROM `groups` WHERE {where_clause} LIMIT 1", params)


async def check_duplicate_group(unique_name: Any) -> Any:
    try:
        return await _find_one(
            "`unique_name` = :un AND `type` = 'GROUP' AND `friend_type` = 'group_team'", {"un": unique_name}
        )
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def check_duplicate_group_with_platform(where: dict[str, Any]) -> Any:
    """Matches ``unique_name`` and its ``a-b`` -> ``b-a`` reversal (intentionally uncached)."""
    try:
        unique_name = where.get("unique_name")
        reversed_name = "-".join(reversed(unique_name.split("-"))) if isinstance(unique_name, str) else unique_name
        clauses = []
        params: dict[str, Any] = {}
        for index, (column, value) in enumerate(where.items()):
            if column == "unique_name":
                clauses.append("`unique_name` IN (:un_a, :un_b)")
                params["un_a"] = unique_name
                params["un_b"] = reversed_name
            elif value is None:
                clauses.append(f"`{column}` IS NULL")
            else:
                clauses.append(f"`{column}` = :w{index}")
                params[f"w{index}"] = value
        return await _find_one(" AND ".join(clauses), params)
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def check_duplicate_group_action(unique_name: Any) -> Any:
    try:
        return await _find_one("`unique_name` = :un AND `platform` = 'action_required'", {"un": unique_name})
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def check_duplicate_group_action_required(unique_name: Any) -> Any:
    try:
        return await _find_one("`unique_name` = :un AND `platform_two` = 'action_required'", {"un": unique_name})
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def get_group_details(where: dict[str, Any], attributes: list[Any]) -> Any:
    try:
        key = _group_info_key(where, attributes)
        cached = await cache_service.get(key)
        if cached:
            return json.loads(cached)
        params: dict[str, Any] = {}
        result = await raw.select_one(
            f"SELECT {attrs_sql(attributes)} FROM `groups` WHERE {where_sql(where, params)} LIMIT 1", params
        )
        await cache_service.set(key, stringify(result), get_settings().redis.cache_ttl_long)
        return result
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def _with_participants(group: dict[str, Any] | None, extra_clause: str = "", extra_params: dict | None = None):
    """Attach ``groupParticipantModel`` (id, user_id, role_sid) like a non-raw Sequelize include."""
    if not group:
        return group
    params = {"gid": group["id"], **(extra_params or {})}
    rows = await raw.select(
        "SELECT `id`, `user_id`, `role_sid` FROM `group_participants` WHERE `group_id` = :gid" + extra_clause, params
    )
    group["groupParticipantModel"] = rows
    return group


async def check_duplicate_group_with_participants(unique_name: Any) -> Any:
    try:
        group = await _find_one(
            "`unique_name` = :un AND `type` = 'GROUP' AND `friend_type` = 'group_team'", {"un": unique_name}
        )
        return await _with_participants(group, " AND `role_sid` != 'worker'")
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def get_group_with_participants(group_id: Any) -> Any:
    try:
        group = await _find_one("`id` = :id AND `type` = 'GROUP' AND `friend_type` = 'group_team'", {"id": group_id})
        return await _with_participants(group)
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def get_group_with_participants_by_job(job_stage_id: Any) -> Any:
    try:
        group = await _find_one(
            "`unique_name` = :un AND `type` = 'GROUP' AND `friend_type` = 'group_team'", {"un": job_stage_id}
        )
        return await _with_participants(group)
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def get_chat_group_by_employer_id(employer_user_id: Any) -> Any:
    """findAll with a REQUIRED include: only groups where the employer is a participant."""
    try:
        groups_rows = await raw.select(
            f"SELECT {attrs_sql(DUPLICATE_ATTRS, 'groupModel')} FROM `groups` AS `groupModel` "
            "WHERE `groupModel`.`type` = 'GROUP' AND `groupModel`.`friend_type` = 'group_team' "
            "AND EXISTS (SELECT 1 FROM `group_participants` gp WHERE gp.`group_id` = `groupModel`.`id` AND gp.`user_id` = :u)",
            {"u": employer_user_id},
        )
        for group in groups_rows:
            await _with_participants(group, " AND `user_id` = :u", {"u": employer_user_id})
        return groups_rows
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def get_all_list_count_from_promise() -> dict[str, str]:
    """``getAllListCount(convertObject(...))`` in filterListConversation.

    ``convertObject`` is ``async``, so Sequelize received a Promise instead of
    an options object and threw. The endpoint has always returned this error
    object as ``totalNotReply``; kept verbatim (documented quirk).
    """
    return {
        "error": "The argument passed to findAll must be an options object, use findByPk if you wish to pass a single primary key value"
    }
