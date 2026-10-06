"""LINE Messaging API integration (port of src/services/lineMessage.service.js).

The Node service used ``@line/bot-sdk`` only for ``pushMessage`` /
``replyMessage``; both are plain REST calls, ported with httpx (same
endpoints, bearer auth, payloads). LINE features no-op (fail-open, warning
logged) when LINE_CHANNEL_ACCESS_TOKEN is not configured.

Inbound flow (webhook worker): unknown LINE user -> ask for registered email
-> link ``users.line_user_id``; known user -> create/find the LINE group with
staff user 1 and persist + emit the message to the conversation room.
"""

from __future__ import annotations

import re
import time
from typing import Any

from app.core.config import get_settings
from app.core.http import developer_message_or_message, error_status_or_message, request
from app.core.js import UNDEFINED, js_string, or_, parse_int, sanitize_filename
from app.db import raw
from app.repositories import aws_message as aws_message_repo
from app.repositories import group as group_repo
from app.repositories import group_participant as gp_repo
from app.repositories import people
from app.repositories import users as users_repo
from app.services import uploader
from app.utils.identity import get_friend_identity

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
DEFAULT_TEXT = "Please enter your py_chat registered email to continue."


def _token() -> str:
    return get_settings().line_channel_access_token


def _enabled() -> bool:
    if not _token():
        print("LINE_CHANNEL_ACCESS_TOKEN not set — LINE messaging disabled (skipping).", flush=True)
        return False
    return True


def _auth_headers() -> dict[str, str]:
    return {"Content-Type": "application/json", "Authorization": f"Bearer {_token()}"}


async def handle_webhook_line_message(body: Any, io) -> None:
    events = (body or {}).get("events") if isinstance(body, dict) else None
    if events is None:
        return
    if not isinstance(events, list) or len(events) == 0:
        return
    for event in events:
        try:
            await _handle_single_line_event(event, io)
        except Exception as error:  # noqa: BLE001 - one failing event never aborts the rest
            print(f"error {error}", flush=True)


async def _handle_single_line_event(event: dict[str, Any], io) -> None:
    message = event.get("message")
    source = event.get("source")
    reply_token = event.get("replyToken")
    if not source or not message:
        return
    line_user_id = source.get("userId")
    message_content = message.get("text")
    media_url = None
    sender = await users_repo.get_user_info_in_view_by_custom(
        "line_user_id",
        line_user_id,
        "user_id,email,line_user_id,role_id,mobile_number,role_name,guard_name,related_name as full_name, user_id as id,profile_image",
    )
    if isinstance(sender, list) and len(sender) == 0:
        if message_content:
            if EMAIL_RE.match(message_content):
                by_email = await users_repo.get_user_info_in_view_by_custom("email", message_content, "user_id,email")
                if isinstance(by_email, list) and len(by_email) == 0:
                    await send_line_text_message_to_replay(reply_token, "Email not found. Please register first on py_chat.")
                    return
                info = by_email[0]
                await users_repo.update_details_by_user_id(info["user_id"], {"line_user_id": line_user_id})
                await send_line_text_message_to_replay(reply_token, "Welcome to py_chat Chat! You can now start sending messages.")
            else:
                await send_line_text_message_to_replay(reply_token, DEFAULT_TEXT)
                return
    else:
        content_type = "text"
        receiver_user_id = 1
        if message.get("type") == "image":
            content_type = "image"
            media_info = await _upload_file_to_s3_from_line(message.get("id"))
            message_content = media_info["originalname"]
            media_url = media_info["mediaUrl"]
        elif message.get("type") in ("video", "audio", "sticker"):
            await send_line_text_message_to_replay(reply_token, "Video and audio messages are not supported at the moment.")
        await _validate_send_line_message_to_receiver(
            sender[0], message_content, content_type, media_url, "line", "line", receiver_user_id, io
        )


async def _upload_file_to_s3_from_line(message_id: Any) -> dict[str, Any]:
    content = await get_message_info_by_id(message_id)
    if isinstance(content, bytes | bytearray):
        output_file_name = f"line_image_{int(time.time() * 1000)}.jpg"
        file_ext = output_file_name.split(".")[-1]
        file_name = sanitize_filename(output_file_name.split(".")[0])
        result = await uploader.upload({"buffer": bytes(content)}, "chat", file_ext, file_name)
        return {"originalname": f"{file_name}.{file_ext}", "mediaUrl": result}
    # Node returned undefined here, so reading `.originalname` threw (caught per event).
    raise TypeError("Cannot read properties of undefined (reading 'originalname')")


async def _validate_send_line_message_to_receiver(
    sender: dict[str, Any], message_content: Any, content_type: str, media_url: Any, platform: str, platform2: str, receiver_user_id: int, io
) -> None:
    try:
        if not sender:
            return
        sender["receiver_user_id"] = receiver_user_id
        sender["platform"] = platform
        sender["platform2"] = platform2
        sender["friend_id"] = sender.get("user_id")
        sender["friend_type"] = "user" if sender.get("guard_name") in ("cs_interpreter_all", "interpreter") else sender.get("guard_name")
        receiver = await users_repo.get_user_info_in_view(
            receiver_user_id,
            "CONCAT(country_code, mobile_number) as mobile_number,whatsapp_country_code,whatsapp_number,guard_name,related_name as full_name,guard_name,profile_image,user_id, user_id as id, email, role_id",
        )
        group_response = await create_conversation_line_action_required(sender, receiver)
        if group_response.get("success"):
            group_info = group_response["data"]
            receiver_info = receiver[0] if isinstance(receiver, list) and receiver else {}
            from datetime import UTC, datetime

            message_data: dict[str, Any] = {
                "group_id": group_info.get("id"),
                "user_id": sender.get("user_id"),
                "sender_id": sender.get("user_id"),
                "receiver_id": parse_int(receiver_info.get("id")),
                "platform": platform if platform else "app",
                "content": message_content,
                "message_type": content_type if content_type else "text",
                "media_type": content_type if content_type else "text",
                "entity_id": None,
                "entity_type": None,
                "media_link": media_url if content_type != "text" else None,
                "created_by": sender.get("user_id"),
                "created_at": datetime.now(UTC),
                "response_time": 0,
            }
            messages = await aws_message_repo.create_message_with_multi_language(message_data)
            message_id = messages.get("insert_id") if isinstance(messages, dict) else None
            for key in ("content_en", "content_id", "content_zh_cn", "content_zh_tw", "source_language"):
                message_data[key] = messages.get(key) if isinstance(messages, dict) else None
            await group_repo.update_group_by_id(
                {
                    "last_message": message_content,
                    "last_msgtype": content_type if content_type else "text",
                    "last_message_ceated_at": datetime.now(UTC).replace(tzinfo=None),
                    "message_send_by": sender.get("id"),
                    "updated_at": datetime.now(UTC).replace(tzinfo=None),
                    "updated_by": sender.get("id"),
                },
                group_info.get("id"),
            )
            settings = get_settings()
            message_data["conversation_sid"] = group_info.get("id")
            message_data["profile_image"] = (
                f"{settings.aws_s3_base_url}{sender['profile_image']}" if sender.get("profile_image") else settings.default_profile_image
            )
            message_data["read_status"] = "unread"
            message_data["id"] = message_id
            message_data["message_id"] = message_id
            receiver_data = dict(message_data)
            receiver_data.update(
                {
                    "id": message_id,
                    "message_id": message_id,
                    "attachments": None,
                    "guard_name": sender.get("guard_name"),
                    "full_name": sender.get("full_name"),
                    "email": sender.get("email"),
                    "read_by": "",
                    "replies": "",
                    "status": "sent",
                    "isWhatsTemplate": "no",
                    "receiver_id": parse_int(group_info.get("unique_name")),
                    "mobile_number": f"{_js(sender.get('whatsapp_country_code', UNDEFINED))}{_js(sender.get('whatsapp_number', UNDEFINED))}",
                }
            )
            await io.emit_to(receiver_data["conversation_sid"], "send_message", {"status": True, "message": "Success", "data": receiver_data})
            await io.emit_to(receiver_data["conversation_sid"], "receive_message", {"status": True, "message": "Success", "data": receiver_data})
        else:
            data = group_response.get("data")
            await io.emit_to(
                data.get("id") if isinstance(data, dict) else None,
                "receive_message",
                {"status": False, "message": group_response.get("message"), "data": {}},
            )
    except Exception as error:  # noqa: BLE001
        print(f"error {developer_message_or_message(error)}", flush=True)


def _js(value: Any) -> str:
    return js_string(value)


async def create_conversation_line_action_required(user_info: dict[str, Any], receiver_info: Any) -> dict[str, Any]:
    try:
        user_identity = user_info.get("line_user_id")
        friend_identity: Any = ""
        friend_role_id: Any = ""
        platform = user_info.get("platform")
        friend_id = user_info.get("friend_id")
        friend_type = user_info.get("friend_type")
        platform2 = user_info.get("platform2")
        unique_name = friend_id
        duplicate = await group_repo.check_duplicate_group_with_platform(
            {"unique_name": unique_name, "platform": platform, "platform_two": platform2}
        )
        if duplicate:
            return {"success": True, "data": duplicate, "message": "Success Created"}
        receiver = receiver_info[0] if isinstance(receiver_info, list) and receiver_info else None
        friendly_name = receiver.get("full_name", UNDEFINED) if receiver else UNDEFINED
        if friend_type == "employer":
            details = await people.employer_get_details_by_user_id(friend_id)
            if not details:
                # Node referenced an undefined `req` here (ReferenceError -> caught below).
                raise NameError("req is not defined")
            friendly_name = f"{_js(friendly_name)}/{_js(details.get('name', UNDEFINED))} {_js(details.get('surname', UNDEFINED))}"
            friend_role_id = details.get("role_id")
            friend_identity = await get_friend_identity(details, platform)
        elif friend_type == "worker":
            details = await people.worker_get_details_by_user_id(friend_id)
            if not details:
                raise NameError("req is not defined")
            friendly_name = f"{_js(friendly_name)}/{_js(details.get('name', UNDEFINED))} {_js(details.get('surname', UNDEFINED))}"
            friend_role_id = details.get("role_id")
            friend_identity = await get_friend_identity(details, platform)
        elif friend_type in ("user", "partner"):
            rows = await users_repo.get_user_info_in_view(
                friend_id,
                "CONCAT(country_code, mobile_number) as mobile_number,guard_name,related_name as full_name,role_id,user_id,whatsapp_country_code,whatsapp_number",
            )
            if isinstance(rows, list) and len(rows) == 0:
                raise NameError("req is not defined")
            info = rows[0]
            friendly_name = f"{_js(friendly_name)}/{_js(info.get('full_name', UNDEFINED))}"
            friend_role_id = info.get("role_id")
            friend_identity = await get_friend_identity(info, platform)

        if platform not in ("line", "action_required"):
            return {"success": True, "data": duplicate, "message": "Success Created"}
        if receiver is None:
            raise TypeError("Cannot read properties of undefined (reading 'id')")
        from datetime import UTC, datetime

        now = datetime.now(UTC).replace(tzinfo=None)
        async with raw.transaction() as conn:
            group = await group_repo.create_group(
                {
                    "conversation_state": "Active",
                    "friendly_name": friendly_name,
                    "unique_name": unique_name,
                    "type": "ONE_TO_MANY",
                    "platform": platform,
                    "platform_two": platform2,
                    "friend_type": friend_type,
                    "created_by": receiver.get("id"),
                    "created_at": now,
                },
                conn,
            )
            if "error" in group and len(group) == 1:
                raise RuntimeError(group["error"])
            await gp_repo.create_group_participant(
                {
                    "identity": user_identity,
                    "group_id": group["id"],
                    "user_id": receiver.get("id"),
                    "is_host": 1,
                    "role_id": receiver.get("role_id"),
                    "joined_on": now,
                    "created_by": receiver.get("id"),
                    "created_at": now,
                },
                conn,
            )
            await gp_repo.create_group_participant(
                {
                    "identity": friend_identity,
                    "group_id": group["id"],
                    "user_id": friend_id,
                    "is_host": 0,
                    "role_id": friend_role_id,
                    "joined_on": now,
                    "created_by": receiver.get("id"),
                    "created_at": now,
                    "account_sid": 1,
                },
                conn,
            )
        group["conversation_sid"] = group.get("id")
        return {"success": True, "data": dict(group), "message": "Success Created"}
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": str(error)}


async def send_line_text_message(to_user_id: Any, text: Any) -> None:
    try:
        if not _enabled():
            return
        await request(
            "post",
            "https://api.line.me/v2/bot/message/push",
            json_body={"to": to_user_id, "messages": [{"type": "text", "text": text}]},
            headers=_auth_headers(),
        )
    except Exception as error:  # noqa: BLE001
        print(f"LINE SDK Error: {error}", flush=True)


async def send_line_text_message_to_replay(reply_token: Any, text: str) -> None:
    try:
        if not _enabled():
            return
        await request(
            "post",
            "https://api.line.me/v2/bot/message/reply",
            json_body={"replyToken": reply_token, "messages": [{"type": "text", "text": text}]},
            headers=_auth_headers(),
        )
    except Exception as error:  # noqa: BLE001
        print(f"LINE SDK Error: {error}", flush=True)


async def send_line_image_message_sdk(*_args: Any) -> None:
    """Preserved defect: the Node function took no parameters and referenced undefined
    ``toUserId``/``imageUrl``, so every call threw and was swallowed — LINE image sends
    never reached LINE. Kept as a logged no-op; see MIGRATION_REPORT.md (security/
    behaviour findings) before "fixing" it, since that would change behaviour."""
    if not _token():
        print("LINE_CHANNEL_ACCESS_TOKEN not set — LINE messaging disabled (skipping).", flush=True)
        return
    print("LINE SDK Error: toUserId is not defined", flush=True)


async def get_message_info_by_id(message_id: Any) -> Any:
    try:
        response = await request(
            "get",
            f"https://api-data.line.me/v2/bot/message/{message_id}/content",
            headers={"Authorization": f"Bearer {_token()}"},
            response_type="arraybuffer",
            timeout_ms=get_settings().outbound_media_timeout_ms,
        )
        return response.data
    except Exception as error:  # noqa: BLE001
        print(f"LINE SDK Error: {error}", flush=True)
        return None


async def send_line_text_message_url(to_user_id: Any, text: Any) -> Any:
    try:
        res = await request(
            "post",
            "https://api.line.me/v2/bot/message/push",
            json_body={"to": to_user_id, "messages": [{"type": "text", "text": text}]},
            headers=_auth_headers(),
            timeout_ms=5000,
        )
        return res.data
    except Exception as error:  # noqa: BLE001
        print(f"LINE API Error: {error_status_or_message(error)}", flush=True)
        return None


def or_default(value: Any, fallback: Any) -> Any:
    return or_(value, fallback)
