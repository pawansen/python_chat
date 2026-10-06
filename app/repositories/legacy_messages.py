"""Legacy MySQL message stores (ports of message.repository.js,
messages.log.repository.js, action.logs.repository.js).

The LIVE chat message store is DynamoDB (``aws_message.py``). These tables are
the Twilio-era store; only ``createMessage`` (used by the legacy
``sendMessage`` service) and ``getUnreadMessageCountUser`` (used by the
``getUnreadMessageCount`` util) are reachable, plus ``action_logs`` inserts.
"""

from __future__ import annotations

from typing import Any

from app.db import raw
from app.models import build_values, messages
from app.repositories._sql import error, insert_sql


async def create_message(data: dict[str, Any]) -> Any:
    """``messageModel.create(data)`` -> id (unknown keys are dropped like Sequelize)."""
    try:
        values = build_values(messages, data)
        values.pop("id", None)
        params: dict[str, Any] = {}
        return await raw.insert(insert_sql("messages", values, params), params)
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def get_unread_message_count_user(user_id: Any, conversation_sid: Any) -> Any:
    """Sequelize ``count({ group: [id] })`` -> list of ``{count}`` rows."""
    try:
        return await raw.select(
            "SELECT COUNT(`messageModel`.`id`) AS `count` FROM `messages` AS `messageModel` "
            "WHERE `messageModel`.`group_id` = :g AND `messageModel`.`is_deleted` = 0 "
            "AND `messageModel`.`read_status` = 'unread' AND `messageModel`.`status` = 1 "
            "AND `messageModel`.`user_id` != :u GROUP BY `messageModel`.`id`",
            {"g": conversation_sid, "u": user_id},
        )
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def get_last_message(group_id: Any) -> Any:
    try:
        return await raw.select_one(
            "SELECT `content`, `message_type`, `created_at`, `id` FROM `messages` WHERE `group_id` = :g "
            "AND `is_deleted` = 0 ORDER BY `id` DESC LIMIT 1",
            {"g": group_id},
        )
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def create_action_log(data: dict[str, Any]) -> Any:
    try:
        params: dict[str, Any] = {}
        return await raw.insert(insert_sql("action_logs", data, params), params)
    except Exception as exc:  # noqa: BLE001
        return error(exc)
