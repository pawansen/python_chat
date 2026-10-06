"""Delivered-receipt helper (port of src/utils/message.delivered.util.js).

Flips the recipient's pending 'unread' messages in a group to 'delivered'
(bulk, never-downgrade) and, when anything transitioned, emits ONE
``message_delivered`` event to the conversation room and to each affected
sender's personal user room. Never raises.
"""

from __future__ import annotations

from typing import Any

from app.core.js import iso_now, js_string, nullish, number, truthy
from app.repositories import aws_message as aws_message_repo


async def deliver_pending_messages(io, group_id: Any, recipient_user_id: Any) -> dict[str, Any]:
    try:
        if not truthy(group_id) or nullish(recipient_user_id):
            return {"success": False, "updated": []}
        result = await aws_message_repo.mark_messages_delivered(group_id, recipient_user_id)
        updated = (result or {}).get("updated") or []
        if not updated or io is None:
            return result
        payload = {
            "status": True,
            "message": "Delivered",
            "data": {
                "conversation_sid": number(group_id),
                "group_id": number(group_id),
                "message_ids": [m.get("id") for m in updated],
                "delivered_to": number(recipient_user_id),
                "delivered_at": iso_now(),
            },
        }
        await io.emit_to(js_string(group_id), "message_delivered", payload)
        sender_rooms: list[str] = []
        for message in updated:
            sender = message.get("sender_id")
            if nullish(sender):
                continue
            key = js_string(sender)
            if key not in sender_rooms:
                sender_rooms.append(key)
        if js_string(recipient_user_id) in sender_rooms:
            sender_rooms.remove(js_string(recipient_user_id))
        for room in sender_rooms:
            await io.emit_to(room, "message_delivered", payload)
        return result
    except Exception as error:  # noqa: BLE001
        print(f"deliverPendingMessages error: {error}", flush=True)
        return {"success": False, "updated": [], "error": str(error)}
