"""DynamoDB chat-message repository (port of src/repository/awsMessage.repository.js).

Table ``DynoMODB_Message_Table`` (default ``chat_messages``), PK ``id`` (N) +
SK ``created_at`` (S); ids come from the ``Counters`` table. GSIs used:
``ChatIdIndex`` (group_id, id), ``ChatCreatedAtIndex`` and
``CreatedAtIndexSort`` (group_id, created_at), ``UserConversationIndex``
(user_id, group_id), ``GetUnreadCountIndex`` (receiver_id, group_id).

Return conventions are preserved: ``{success, message}`` envelopes, raw item
lists, ``[]``/``False`` for "not found" and ``{error}`` on failure.
The stale ``*old``/``*New``/``*Backup`` variants were not ported (dead code).
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

from app.core.config import env_num, get_settings
from app.core.js import UNDEFINED, iso_now, js_string, loose_eq, nullish, number, or_, parse_int, truthy
from app.core.tasks import spawn
from app.dynamodb.client import document_client, error_code, insert_message
from app.redis import cache_keys, cache_service
from app.repositories import group_participant as gp_repo


def _table() -> str:
    return get_settings().dynamodb_message_table


def _env(name: str, default: float) -> int:
    return int(env_num(getattr(get_settings().raw, name, None), default))


def bump_conversation_list_versions(user_ids: list[Any]) -> None:
    seen: list[Any] = []
    for uid in user_ids or []:
        if nullish(uid) or uid in seen:
            continue
        seen.append(uid)
        cache_service.bump_version_bg(cache_keys.version_conversation_key(uid))


def bump_conversation_list_versions_for_group(group_id: Any, already_bumped: list[Any]) -> None:
    """Inbound webhook/worker messages carry no receiver_id: bump every participant (inclusive read)."""
    try:
        if nullish(group_id):
            return
        skip = {js_string(v) for v in already_bumped if not nullish(v)}

        async def _run() -> None:
            participants = await gp_repo.get_group_participants_list({"group_id": group_id}, ["id", "user_id", "role_sid"])
            if not isinstance(participants, list):
                return
            for participant in participants:
                uid = participant.get("user_id")
                if nullish(uid) or js_string(uid) in skip:
                    continue
                cache_service.bump_version_bg(cache_keys.version_conversation_key(uid))

        spawn(_run())
    except Exception:  # noqa: BLE001
        pass


def increment_recipient_unread(payload: dict[str, Any]) -> None:
    receiver_id = payload.get("receiver_id", UNDEFINED)
    if nullish(receiver_id) or math.isnan(float(number(receiver_id))):
        return
    if number(receiver_id) == number(payload.get("user_id")):
        return
    spawn(gp_repo.increment_unread_count(payload.get("group_id"), receiver_id))


def _queue_translation(payload: dict[str, Any], insert_id: Any) -> None:
    from app.queues.translation_queue import queue_translation

    queue_translation(
        {
            "messageId": insert_id,
            "createdAt": payload["created_at"],
            "groupId": payload["group_id"],
            "content": payload.get("content"),
            "platform": payload.get("platform"),
            "senderId": payload.get("sender_id"),
            "receiverId": payload.get("receiver_id", UNDEFINED),
            "userId": payload.get("user_id"),
        }
    )


def _g(message: dict[str, Any], key: str) -> Any:
    return message.get(key, UNDEFINED)


async def create_message(message: dict[str, Any]) -> Any:
    """Persist immediately with null translations; translation runs in the background."""
    try:
        receiver = _g(message, "receiver_id")
        payload = {
            "group_id": number(_g(message, "group_id")),
            "user_id": number(_g(message, "user_id")),
            "sender_id": or_(_g(message, "sender_id"), None),
            "receiver_id": number(receiver) if truthy(receiver) else None,
            "entity_id": or_(_g(message, "entity_id"), None),
            "replay_message_id": or_(_g(message, "replay_message_id"), None),
            "content": or_(_g(message, "content"), None),
            "content_en": None,
            "content_id": None,
            "content_zh_cn": None,
            "content_zh_tw": None,
            "message_type": or_(_g(message, "message_type"), "text"),
            "media_type": or_(_g(message, "media_type"), "text"),
            "media_link": or_(_g(message, "media_link"), None),
            "media_info": or_(_g(message, "media_info"), None),
            "attributes": or_(_g(message, "attributes"), None),
            "status": or_(_g(message, "status"), 1),
            "response_time": or_(_g(message, "response_time"), None),
            "read_status": or_(_g(message, "read_status"), "unread"),
            "is_deleted": or_(_g(message, "is_deleted"), 0),
            "is_media_deleted": or_(_g(message, "is_media_deleted"), 0),
            "deleted_by": or_(_g(message, "deleted_by"), None),
            "deleted_at": or_(_g(message, "deleted_at"), None),
            "created_at": iso_now(),
            "created_by": or_(_g(message, "created_by"), None),
            "updated_at": or_(_g(message, "updated_at"), None),
            "updated_by": or_(_g(message, "updated_by"), None),
            "platform": or_(_g(message, "platform"), "app"),
            "entity_type": or_(_g(message, "entity_type"), None),
            "wa_message_id": or_(_g(message, "wa_message_id"), None),
            "role_type": or_(_g(message, "role_type"), "member"),
            "type": or_(_g(message, "type"), "SINGLE"),
        }
        insert_id = await insert_message(payload)
        _queue_translation(payload, insert_id)
        bump_conversation_list_versions([payload["user_id"], payload["sender_id"], payload["receiver_id"]])
        if nullish(payload["receiver_id"]):
            bump_conversation_list_versions_for_group(payload["group_id"], [payload["user_id"], payload["sender_id"]])
        increment_recipient_unread(payload)
        return insert_id
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR CREATE MESSAGE ->>>>>>>>>>>>>>>>: {exc}", flush=True)
        return {"error": str(exc)}


async def create_message_with_multi_language(message: dict[str, Any]) -> dict[str, Any]:
    """Same as create_message but returns the payload (content_* null until the worker fills them)."""
    try:
        payload: dict[str, Any] = {
            "group_id": number(_g(message, "group_id")),
            "user_id": number(_g(message, "user_id")),
            "sender_id": or_(_g(message, "sender_id"), None),
            "entity_id": or_(_g(message, "entity_id"), None),
            "replay_message_id": or_(_g(message, "replay_message_id"), None),
            "content": or_(_g(message, "content"), None),
            "content_en": None,
            "content_id": None,
            "content_zh_cn": None,
            "content_zh_tw": None,
            "source_language": "en",
            "message_type": or_(_g(message, "message_type"), "text"),
            "media_type": or_(_g(message, "media_type"), "text"),
            "media_link": or_(_g(message, "media_link"), None),
            "media_info": or_(_g(message, "media_info"), None),
            "attributes": or_(_g(message, "attributes"), None),
            "status": or_(_g(message, "status"), 1),
            "response_time": or_(_g(message, "response_time"), None),
            "read_status": or_(_g(message, "read_status"), "unread"),
            "is_deleted": or_(_g(message, "is_deleted"), 0),
            "is_media_deleted": or_(_g(message, "is_media_deleted"), 0),
            "deleted_by": or_(_g(message, "deleted_by"), None),
            "deleted_at": or_(_g(message, "deleted_at"), None),
            "created_at": iso_now(),
            "created_by": or_(_g(message, "created_by"), None),
            "updated_at": or_(_g(message, "updated_at"), None),
            "updated_by": or_(_g(message, "updated_by"), None),
            "platform": or_(_g(message, "platform"), "app"),
            "entity_type": or_(_g(message, "entity_type"), None),
            "entity_name": or_(_g(message, "entity_name"), None),
            "wa_message_id": or_(_g(message, "wa_message_id"), None),
            "role_type": or_(_g(message, "role_type"), "member"),
            "type": or_(_g(message, "type"), "SINGLE"),
        }
        receiver = _g(message, "receiver_id")
        if not nullish(receiver):
            payload["receiver_id"] = number(receiver)
        insert_id = await insert_message(payload)
        payload["insert_id"] = insert_id
        _queue_translation(payload, insert_id)
        bump_conversation_list_versions(
            [payload["user_id"], payload["sender_id"], payload.get("receiver_id", UNDEFINED)]
        )
        if nullish(payload.get("receiver_id", UNDEFINED)):
            bump_conversation_list_versions_for_group(payload["group_id"], [payload["user_id"], payload["sender_id"]])
        increment_recipient_unread(payload)
        return payload
    except Exception as exc:  # noqa: BLE001
        print(str(exc), flush=True)
        return {"error": str(exc)}


async def update_message_translations(message_id: Any, created_at: Any, columns: dict[str, Any], group_id: Any = None) -> dict:
    try:
        if nullish(message_id) or not created_at:
            return {"error": "updateMessageTranslations: missing id/created_at"}
        names = {"#updated_at": "updated_at"}
        values: dict[str, Any] = {":updated_at": iso_now()}
        sets = ["#updated_at = :updated_at"]
        for column, value in (columns or {}).items():
            names[f"#{column}"] = column
            values[f":{column}"] = None if nullish(value) else value
            sets.append(f"#{column} = :{column}")
        await document_client.update(
            TableName=_table(),
            Key={"id": number(message_id), "created_at": created_at},
            UpdateExpression="SET " + ", ".join(sets),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="UPDATED_NEW",
        )
        if not nullish(group_id):
            await cache_service.bump_version(cache_keys.version_message_key(group_id))
        return {"success": True}
    except Exception as exc:  # noqa: BLE001
        print(f"Error updating message translations: {exc}", flush=True)
        return {"error": str(exc)}


async def get_unread_message_count(user_id: Any, group_id: Any) -> Any:
    try:
        result = await document_client.query(
            TableName=_table(),
            IndexName="UserConversationIndex",
            KeyConditionExpression="user_id = :user_id AND group_id = :group_id",
            FilterExpression="read_status IN (:unread, :delivered)",
            ExpressionAttributeValues={
                ":user_id": number(user_id),
                ":group_id": number(group_id),
                ":unread": "unread",
                ":delivered": "delivered",
            },
            Select="COUNT",
        )
        return result.get("Count")
    except Exception as exc:  # noqa: BLE001
        print(f"Error counting unread messages: {exc}", flush=True)
        return {"error": str(exc)}


async def get_unread_message_count_receiver(user_id: Any, group_id: Any) -> Any:
    try:
        result = await document_client.query(
            TableName=_table(),
            IndexName="GetUnreadCountIndex",
            KeyConditionExpression="receiver_id = :user_id AND group_id = :group_id",
            FilterExpression="read_status IN (:unread, :delivered)",
            ExpressionAttributeValues={
                ":user_id": number(user_id),
                ":group_id": number(group_id),
                ":unread": "unread",
                ":delivered": "delivered",
            },
            Select="COUNT",
        )
        return result.get("Count")
    except Exception as exc:  # noqa: BLE001
        print(f"Error counting unread messages: {exc}", flush=True)
        return {"error": str(exc)}


async def get_whatsapp_receiver_message_count(user_id: Any, group_id: Any) -> Any:
    try:
        result = await document_client.query(
            TableName=_table(),
            IndexName="CreatedAtIndexSort",
            KeyConditionExpression="group_id = :group_id",
            FilterExpression="sender_id = :sender_id",
            ExpressionAttributeValues={":group_id": number(group_id), ":sender_id": user_id},
            Select="COUNT",
        )
        return result.get("Count")
    except Exception as exc:  # noqa: BLE001
        print(f"Error counting unread messages: {exc}", flush=True)
        return {"error": str(exc)}


async def get_unread_counts_by_receiver(receiver_id: Any) -> dict[int, int]:
    """One paginated GSI query -> {group_id: unread count} for clean 1:1 app chats."""
    counts: dict[Any, int] = {}
    try:
        start_key = None
        while True:
            params: dict[str, Any] = {
                "TableName": _table(),
                "IndexName": "GetUnreadCountIndex",
                "KeyConditionExpression": "receiver_id = :receiver_id",
                "FilterExpression": "read_status IN (:unread, :delivered)",
                "ExpressionAttributeValues": {
                    ":receiver_id": number(receiver_id),
                    ":unread": "unread",
                    ":delivered": "delivered",
                },
                "ProjectionExpression": "group_id",
            }
            if start_key:
                params["ExclusiveStartKey"] = start_key
            result = await document_client.query(**params)
            for item in result.get("Items") or []:
                gid = number(item.get("group_id"))
                counts[gid] = counts.get(gid, 0) + 1
            start_key = result.get("LastEvaluatedKey")
            if not start_key:
                break
        return counts
    except Exception as exc:  # noqa: BLE001
        print(f"Error batching unread counts by receiver: {exc}", flush=True)
        return counts


async def get_group_unread_message_count(group_id: Any) -> Any:
    try:
        result = await document_client.query(
            TableName=_table(),
            IndexName="ChatIdIndex",
            KeyConditionExpression="group_id = :group_id",
            FilterExpression="read_status IN (:unread, :delivered)",
            ExpressionAttributeValues={":group_id": number(group_id), ":unread": "unread", ":delivered": "delivered"},
            Select="COUNT",
        )
        return result.get("Count")
    except Exception as exc:  # noqa: BLE001
        print(f"Error counting unread messages: {exc}", flush=True)
        return {"error": str(exc)}


async def get_unread_message_count_all(user_id: Any, platform: Any = None) -> Any:
    """Legacy DynamoDB badge path (kept for rollback; the API now sums MySQL counters)."""
    params: dict[str, Any] = {
        "TableName": _table(),
        "IndexName": "GetUnreadCountIndex",
        "KeyConditionExpression": "receiver_id = :user_id",
        "FilterExpression": "read_status IN (:unread, :delivered) AND is_deleted = :is_deleted",
        "ExpressionAttributeValues": {
            ":user_id": number(user_id),
            ":unread": "unread",
            ":delivered": "delivered",
            ":is_deleted": 0,
        },
        "Select": "COUNT",
    }
    if platform:
        params["FilterExpression"] += " AND platform = :platform"
        params["ExpressionAttributeValues"][":platform"] = platform
    try:
        total = 0
        last_key = None
        while True:
            if last_key:
                params["ExclusiveStartKey"] = last_key
            result = await document_client.query(**params)
            total += result.get("Count") or 0
            last_key = result.get("LastEvaluatedKey")
            if not last_key:
                break
        return total
    except Exception as exc:  # noqa: BLE001
        print(f"Error counting unread messages: {exc}", flush=True)
        return {"error": str(exc)}


async def _batched_conditional_updates(items: list[dict[str, Any]], build, on_success, on_error) -> None:
    batch_size = 25
    for index in range(0, len(items), batch_size):
        batch = items[index : index + batch_size]

        async def one(message: dict[str, Any]) -> None:
            try:
                await document_client.update(**build(message))
                on_success(message)
            except Exception as exc:  # noqa: BLE001
                on_error(message, exc)

        await asyncio.gather(*(one(m) for m in batch))


async def _query_capped(params: dict[str, Any], max_messages: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    last_key = None
    while True:
        if last_key:
            params["ExclusiveStartKey"] = last_key
        result = await document_client.query(**params)
        items.extend(result.get("Items") or [])
        last_key = result.get("LastEvaluatedKey")
        if not (last_key and len(items) < max_messages):
            break
    return items


async def mark_messages_delivered(group_id: Any, recipient_user_id: Any) -> dict[str, Any]:
    """'unread' -> 'delivered' for messages sent TO the recipient; never downgrades."""
    try:
        page_size = _env("DELIVERED_SYNC_PAGE_SIZE", 100)
        max_messages = _env("DELIVERED_SYNC_MAX_MESSAGES", 1000)
        messages = await _query_capped(
            {
                "TableName": _table(),
                "IndexName": "CreatedAtIndexSort",
                "KeyConditionExpression": "group_id = :group_id",
                "FilterExpression": "read_status = :read_status AND user_id <> :user_id",
                "ExpressionAttributeValues": {
                    ":user_id": number(recipient_user_id),
                    ":group_id": number(group_id),
                    ":read_status": "unread",
                },
                "ScanIndexForward": False,
                "Limit": page_size,
                "ProjectionExpression": "id, created_at, sender_id, user_id",
            },
            max_messages,
        )
        if not messages:
            return {"success": True, "updated": []}
        updated_at = iso_now()
        updated: list[dict[str, Any]] = []

        def build(message: dict[str, Any]) -> dict[str, Any]:
            return {
                "TableName": _table(),
                "Key": {"id": message.get("id"), "created_at": message.get("created_at")},
                "UpdateExpression": "SET read_status = :delivered, updated_by = :updated_by, updated_at = :updated_at",
                "ConditionExpression": "read_status = :unread",
                "ExpressionAttributeValues": {
                    ":delivered": "delivered",
                    ":unread": "unread",
                    ":updated_by": number(recipient_user_id),
                    ":updated_at": updated_at,
                },
            }

        def ok(message: dict[str, Any]) -> None:
            sender = message.get("sender_id")
            updated.append({"id": message.get("id"), "sender_id": message.get("user_id") if nullish(sender) else sender})

        def failed(_message: dict[str, Any], exc: BaseException) -> None:
            if error_code(exc) != "ConditionalCheckFailedException":
                print(f"Error marking message delivered: {exc}", flush=True)

        await _batched_conditional_updates(messages[:max_messages], build, ok, failed)
        if updated:
            cache_service.bump_version_bg(cache_keys.version_message_key(number(group_id)))
        return {"success": True, "updated": updated}
    except Exception as exc:  # noqa: BLE001
        print(f"Error in markMessagesDelivered: {exc}", flush=True)
        return {"success": False, "updated": [], "error": str(exc)}


async def get_message(where: dict[str, Any]) -> Any:
    try:
        return await document_client.get(TableName=_table(), Key=where)
    except Exception as exc:  # noqa: BLE001
        print(f"Error reading item: {exc}", flush=True)
        return {"error": str(exc)}


async def update_message_read_status(key: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    """Marks the messages SENT BY ``key.user_id`` in the group as read (existing semantics)."""
    try:
        result = await document_client.query(
            TableName=_table(),
            IndexName="UserConversationIndex",
            KeyConditionExpression="user_id = :user_id AND group_id = :group_id",
            FilterExpression="read_status IN (:unread, :delivered)",
            ExpressionAttributeValues={
                ":user_id": key.get("user_id"),
                ":group_id": key.get("group_id"),
                ":unread": "unread",
                ":delivered": "delivered",
            },
        )
        messages = result.get("Items") or []
        if not messages:
            return {"success": False, "message": "No messages found for the specified group_id and user_id"}
        stamp = iso_now()
        await asyncio.gather(
            *(
                document_client.update(
                    TableName=_table(),
                    Key={"id": m.get("id"), "created_at": m.get("created_at")},
                    UpdateExpression="SET read_status = :read_status, updated_by = :updated_by, updated_at = :updated_at",
                    ExpressionAttributeValues={
                        ":read_status": "read",
                        ":updated_by": updates.get("updated_by"),
                        ":updated_at": stamp,
                    },
                    ReturnValues="UPDATED_NEW",
                )
                for m in messages
            )
        )
        cache_service.bump_version_bg(cache_keys.version_message_key(number(key.get("group_id"))))
        bump_conversation_list_versions([key.get("user_id"), updates.get("updated_by")])
        return {"success": True, "message": "Messages read status updated successfully"}
    except Exception as exc:  # noqa: BLE001
        print(f"Error updating message read status: {exc}", flush=True)
        return {"success": False, "message": "Error updating message read status"}


async def update_message_read_status_by_waid(key: dict[str, Any], updates: dict[str, Any]) -> Any:
    try:
        result = await document_client.update(
            TableName=_table(),
            Key={"id": key.get("id"), "created_at": updates.get("created_at")},
            UpdateExpression="SET read_status = :read_status, updated_by = :updated_by, updated_at = :updated_at",
            ExpressionAttributeValues={
                ":read_status": updates.get("read_status"),
                ":updated_by": updates.get("updated_by"),
                ":updated_at": iso_now(),
            },
            ReturnValues="UPDATED_NEW",
        )
        cache_service.bump_version_bg(cache_keys.version_message_key(number(key.get("group_id"))))
        bump_conversation_list_versions([updates.get("updated_by")])
        return result
    except Exception as exc:  # noqa: BLE001
        print(f"Error updating message read status: {exc}", flush=True)
        return {"success": False, "message": "Error updating message read status"}


async def delete_message(where: dict[str, Any]) -> None:
    try:
        await document_client.delete(TableName=_table(), Key=where)
    except Exception as exc:  # noqa: BLE001
        print(f"Error deleting item: {exc}", flush=True)


async def get_messages_with_filter_sort_pagination(group_id: Any, options: dict[str, Any]) -> dict[str, Any]:
    """Canonical message-list read: ChatCreatedAtIndex, fills ``limit`` across pages."""
    start_key = options.get("startKey")
    limit = options.get("limit", 10)
    if limit is None or limit is UNDEFINED:
        limit = 10
    ascending = options.get("ascending", False)
    filters = options.get("filters") or {}
    collected: list[dict[str, Any]] = []
    last_key = start_key if truthy(start_key) else None
    try:
        params: dict[str, Any] = {
            "TableName": _table(),
            "IndexName": "ChatCreatedAtIndex",
            "KeyConditionExpression": "group_id = :group_id",
            "ExpressionAttributeValues": {
                ":group_id": group_id,
                ":is_deleted": 0,
                ":platform": or_(filters.get("platform"), "app"),
            },
            "ScanIndexForward": ascending,
            "FilterExpression": "is_deleted = :is_deleted AND platform = :platform",
        }
        while True:
            params["Limit"] = limit - len(collected)
            if last_key:
                params["ExclusiveStartKey"] = last_key
            result = await document_client.query(**params)
            collected.extend(result.get("Items") or [])
            last_key = result.get("LastEvaluatedKey")
            if not (last_key and len(collected) < limit):
                break
        return {"success": True, "Items": collected[: int(limit)], "lastEvaluatedKey": last_key or None}
    except Exception as exc:  # noqa: BLE001
        print(f"Error retrieving messages: {exc}", flush=True)
        return {"success": False, "data": [], "message": str(exc)}


async def get_messages_history_combine(group_id: Any, user_id: Any, options: dict[str, Any]) -> dict[str, Any]:
    try:
        if isinstance(group_id, list):
            group_ids = group_id
        elif isinstance(group_id, str):
            group_ids = [
                v for v in (parse_int(part.strip()) for part in group_id.split(",")) if truthy(v)
            ]
        else:
            group_ids = []
        if not group_ids:
            group_ids = [group_id] if truthy(group_id) else []
        if not group_ids:
            return {"success": True, "Items": {"Items": []}, "message": "Messages retrieved successfully"}
        max_groups = _env("HISTORY_MAX_GROUPS", 50)
        if len(group_ids) > max_groups:
            print(f"getMessagesHistoryCombine: {len(group_ids)} group ids requested, truncating to {max_groups}", flush=True)
            group_ids = group_ids[:max_groups]

        start_key = options.get("startKey")
        limit = options.get("limit")
        ascending = options.get("ascending")
        filters = options.get("filters")
        start_date = options.get("startDate")
        end_date = options.get("endDate")
        search = options.get("search")
        page = options.get("page")
        bounded_limit = min(number(limit) if truthy(number(limit)) else 10, 100)

        async def one(gid: Any) -> dict[str, Any] | None:
            use_date_range = truthy(start_date) and truthy(end_date)
            params: dict[str, Any] = {
                "TableName": _table(),
                "IndexName": "CreatedAtIndexSort",
                "KeyConditionExpression": "group_id = :group_id AND created_at BETWEEN :startDate AND :endDate"
                if use_date_range
                else "group_id = :group_id",
                "ExpressionAttributeValues": {":group_id": gid},
                "ScanIndexForward": ascending if ascending is not None and ascending is not UNDEFINED else True,
                "Limit": bounded_limit,
            }
            if use_date_range:
                params["ExpressionAttributeValues"][":startDate"] = start_date
                params["ExpressionAttributeValues"][":endDate"] = end_date
            expressions: list[str] = []
            if filters:
                for key, value in filters.items():
                    if not nullish(value) and key != "platform":
                        expressions.append(f"{key} = :{key}")
                        params["ExpressionAttributeValues"][f":{key}"] = value
                params["FilterExpression"] = " AND ".join(expressions)
            if filters and truthy(filters.get("platform")):
                platform_value = filters["platform"]
                if not isinstance(platform_value, str):
                    raise TypeError("filters.platform.split is not a function")
                platforms = [p.strip() for p in platform_value.split(",") if p.strip()]
                if len(platforms) == 1:
                    expressions.append("platform = :platform")
                    params["ExpressionAttributeValues"][":platform"] = platforms[0]
                elif len(platforms) > 1:
                    placeholders = [f":platform{i}" for i in range(len(platforms))]
                    expressions.append(f"platform IN ({', '.join(placeholders)})")
                    for i, value in enumerate(platforms):
                        params["ExpressionAttributeValues"][f":platform{i}"] = value
                params["FilterExpression"] = " AND ".join(expressions)
            if truthy(search):
                expressions.append("contains(content, :search)")
                params["ExpressionAttributeValues"][":search"] = search
                params["FilterExpression"] = " AND ".join(expressions)
            if not params.get("FilterExpression"):
                params.pop("FilterExpression", None)
            flag = False
            if truthy(start_key):
                if isinstance(start_key, list):
                    match = next((k for k in start_key if isinstance(k, dict) and loose_eq(k.get("group_id"), gid)), None)
                    if match:
                        params["ExclusiveStartKey"] = match
                        flag = True
                else:
                    params["ExclusiveStartKey"] = start_key
            if truthy(page):
                if flag:
                    return await document_client.query(**params)
                return None
            return await document_client.query(**params)

        results = [r for r in await asyncio.gather(*(one(gid) for gid in group_ids)) if r]
        all_items = [item for r in results for item in (r.get("Items") or [])]
        return {"success": True, "Items": all_items, "results": results, "message": "Messages retrieved successfully"}
    except Exception as exc:  # noqa: BLE001
        print(f"Error retrieving messages: {exc}", flush=True)
        return {"success": False, "data": [], "message": str(exc)}


async def get_last_message(group_id: Any) -> Any:
    """Newest non-deleted message (first of a 10-item page), or ``[]`` when none/error."""
    try:
        result = await document_client.query(
            TableName=_table(),
            IndexName="CreatedAtIndexSort",
            KeyConditionExpression="group_id = :group_id",
            FilterExpression="is_deleted = :is_deleted",
            ExpressionAttributeValues={":group_id": number(group_id), ":is_deleted": 0},
            ScanIndexForward=False,
            Limit=10,
        )
        items = result.get("Items") or []
        if not items:
            return []
        return items[0]
    except Exception as exc:  # noqa: BLE001
        print(f"Error retrieving last message: {exc}", flush=True)
        return []


async def get_message_by_waid(group_id: Any, wa_message_id: Any, wa_status: Any) -> Any:
    try:
        result = await document_client.query(
            TableName=_table(),
            IndexName="CreatedAtIndexSort",
            KeyConditionExpression="group_id = :group_id",
            FilterExpression="is_deleted = :is_deleted AND wa_message_id = :wa_message_id AND read_status <> :read_status",
            ExpressionAttributeValues={
                ":group_id": number(group_id),
                ":is_deleted": 0,
                ":wa_message_id": wa_message_id,
                ":read_status": wa_status,
            },
        )
        items = result.get("Items") or []
        if not items:
            return []
        return items[0]
    except Exception as exc:  # noqa: BLE001
        print(f"Error retrieving message by WA ID: {exc}", flush=True)
        return []


async def read_message_to_user(data: dict[str, Any], sid: Any, user_id: Any) -> dict[str, Any]:
    """Mark messages TO ``user_id`` read (bounded) + O(1) last-read watermark."""
    try:
        spawn(gp_repo.set_last_read_watermark(sid, user_id))
        page_size = _env("READ_SYNC_PAGE_SIZE", 100)
        max_messages = _env("READ_SYNC_MAX_MESSAGES", 1000)
        messages = await _query_capped(
            {
                "TableName": _table(),
                "IndexName": "CreatedAtIndexSort",
                "KeyConditionExpression": "group_id = :group_id",
                "FilterExpression": "read_status IN (:unread, :delivered) AND user_id <> :user_id",
                "ExpressionAttributeValues": {
                    ":user_id": number(user_id),
                    ":group_id": number(sid),
                    ":unread": "unread",
                    ":delivered": "delivered",
                },
                "ScanIndexForward": False,
                "Limit": page_size,
                "ProjectionExpression": "id, created_at",
            },
            max_messages,
        )
        if not messages:
            return {"success": False, "message": "No messages found for the specified criteria"}
        stamp = iso_now()
        batch_size = 25
        to_update = messages[:max_messages]
        for index in range(0, len(to_update), batch_size):
            await asyncio.gather(
                *(
                    document_client.update(
                        TableName=_table(),
                        Key={"id": m.get("id"), "created_at": m.get("created_at")},
                        UpdateExpression="SET read_status = :read_status, updated_by = :updated_by, updated_at = :updated_at",
                        ExpressionAttributeValues={
                            ":read_status": data.get("read_status"),
                            ":updated_by": data.get("updated_by"),
                            ":updated_at": stamp,
                        },
                        ReturnValues="UPDATED_NEW",
                    )
                    for m in to_update[index : index + batch_size]
                )
            )
        cache_service.bump_version_bg(cache_keys.version_message_key(number(sid)))
        bump_conversation_list_versions([user_id])
        return {"success": True, "message": "Messages read status updated successfully"}
    except Exception as exc:  # noqa: BLE001
        print(f"Error updating message read status: {exc}", flush=True)
        return {"success": False, "message": "Error updating message read status"}


async def delete_message_to_user(data: dict[str, Any], where: dict[str, Any]) -> dict[str, Any]:
    """Soft-delete the caller's own message (base-table query on id)."""
    try:
        result = await document_client.query(
            TableName=_table(),
            KeyConditionExpression="id = :id",
            FilterExpression="group_id = :group_id AND user_id = :user_id",
            ExpressionAttributeValues={
                ":id": number(where.get("id")),
                ":group_id": number(where.get("group_id")),
                ":user_id": number(where.get("user_id")),
            },
        )
        messages = result.get("Items") or []
        if not messages:
            return {"success": False, "message": "No messages found for the specified criteria"}
        deleted_at = iso_now()
        await asyncio.gather(
            *(
                document_client.update(
                    TableName=_table(),
                    Key={"id": m.get("id"), "created_at": m.get("created_at")},
                    UpdateExpression="SET is_deleted = :is_deleted, deleted_by = :deleted_by, deleted_at = :deleted_at, read_status = :read_status",
                    ExpressionAttributeValues={
                        ":is_deleted": 1,
                        ":read_status": "read",
                        ":deleted_by": data.get("deleted_by"),
                        ":deleted_at": deleted_at,
                    },
                    ReturnValues="UPDATED_NEW",
                )
                for m in messages
            )
        )
        await cache_service.delete_conversation_list_cache(cache_keys.message_list_pattern(where.get("group_id")))
        cache_service.bump_version_bg(cache_keys.version_message_key(number(where.get("group_id"))))
        bump_conversation_list_versions([where.get("user_id")])
        return {"success": True, "message": "Message deleted successfully"}
    except Exception as exc:  # noqa: BLE001
        print(f"Error deleting message: {exc}", flush=True)
        return {"success": False, "message": "Error deleting message"}


async def get_message_by_receiver_id(group_id: Any, sender_id: Any) -> Any:
    """Newest message from ``sender_id`` within a bounded newest-first window, else False."""
    page_size = _env("RECEIVER_LOOKUP_PAGE_SIZE", 100)
    max_pages = _env("RECEIVER_LOOKUP_MAX_PAGES", 5)
    params: dict[str, Any] = {
        "TableName": _table(),
        "IndexName": "CreatedAtIndexSort",
        "KeyConditionExpression": "group_id = :group_id",
        "FilterExpression": "sender_id = :sender_id",
        "ExpressionAttributeValues": {":group_id": number(group_id), ":sender_id": sender_id},
        "ScanIndexForward": False,
        "Limit": page_size,
    }
    try:
        pages = 0
        last_key = None
        while True:
            if last_key:
                params["ExclusiveStartKey"] = last_key
            result = await document_client.query(**params)
            items = result.get("Items") or []
            if items:
                return items[0]
            last_key = result.get("LastEvaluatedKey")
            pages += 1
            if not (last_key and pages < max_pages):
                break
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"Error retrieving message by WA ID: {exc}", flush=True)
        return False
