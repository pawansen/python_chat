"""WhatsApp flows (port of aws.service.js: setWhatsUpWebhook, sendMessageWhatsapp,
handleWebhookWhatsApp, processWhatsAppWebhookJob, sendWhatsAppMessage,
sendOTPWhatsapp, sendOTPWhatsappDirect, sendWelcomeMessageWhatsapp,
sendWhatsAppWelcomeMessage, validateMobile, sendAutoMessageWhatsapp).
"""

from __future__ import annotations

import time
from typing import Any

from app.core.config import get_settings
from app.core.http import developer_message_or_message, error_status_or_message, request
from app.core.js import UNDEFINED, js_string, parse_int, strip_html, strip_phone, truthy
from app.core.tasks import spawn
from app.queues.webhook_queue import queue_webhook
from app.repositories import aws_message as aws_message_repo
from app.repositories import group as group_repo
from app.repositories import users as users_repo
from app.services import whatsapp as whatsapp_service
from app.services.chat.common import map_with_concurrency, now_aware, s3_prefixed, utcnow

USER_ATTRS = "CONCAT(country_code, mobile_number) as mobile_number,guard_name,related_name as full_name,role_id,user_id,whatsapp_country_code,whatsapp_number"
SENDER_ATTRS = (
    "CONCAT(country_code, mobile_number) as mobile_number,whatsapp_country_code,whatsapp_number,guard_name,"
    "related_name as full_name,guard_name,profile_image,user_id, user_id as id"
)

MIME_TO_EXT = {
    "image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp", "image/heic": "heic",
    "video/mp4": "mp4", "video/3gpp": "3gp",
    "audio/aac": "aac", "audio/amr": "amr", "audio/mpeg": "mp3", "audio/ogg": "ogg", "audio/opus": "opus", "audio/wav": "wav",
    "application/pdf": "pdf", "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-excel": "xls", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-powerpoint": "ppt", "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "text/plain": "txt", "application/rtf": "rtf", "application/zip": "zip", "application/x-rar-compressed": "rar",
    "application/x-tgsticker": "tgs",
}


def _g(obj: Any, key: str, default: Any = UNDEFINED) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else default


def _attr(obj: Any, key: str) -> Any:
    if obj is None or obj is UNDEFINED:
        raise TypeError(f"Cannot read properties of {js_string(obj)} (reading '{key}')")
    return _g(obj, key)


def _replace(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"Cannot read properties of {js_string(value)} (reading 'replace')")
    return strip_phone(value)


def _lang(language: Any) -> Any:
    return "zh_hk" if language == "zh_chs" else ("zh_cn" if language == "zh_hant" else language)


# ── webhook config (sandbox) ────────────────────────────────────────────────
async def set_whatsup_webhook() -> dict[str, Any]:
    """Preserved as-is: points the 360dialog SANDBOX webhook using a hard-coded
    sandbox key (security finding — see MIGRATION_REPORT.md §11)."""
    try:
        response = await request(
            "post",
            "https://waba-sandbox.360dialog.io/v1/configs/webhook",
            json_body={"url": "https://chatapi.py_chat.ai/chats/webhook"},
            headers={"D360-API-KEY": "N3WAmg_sandbox", "Content-Type": "application/json"},
        )
        return {"success": True, "data": response.data, "message": "success"}
    except Exception as error:  # noqa: BLE001
        print(f"Error sending message: {error_status_or_message(error)}", flush=True)
        return {"success": False, "data": [{"url": get_settings().whatsapp_api_url, "key": get_settings().whatsapp_api_key}],
                "message": error_status_or_message(error)}


async def send_message_whatsapp(req) -> None:
    spawn(set_whatsup_webhook())
    return None


async def send_auto_message_whatsapp(req) -> None:
    return None


def handle_webhook_whatsapp(body: Any) -> dict[str, Any]:
    queue_webhook({"source": "whatsapp", "body": body})
    return {"success": True, "message": "Webhook accepted"}


# ── inbound webhook processing ──────────────────────────────────────────────
_processed: dict[str, float] = {}


def _is_duplicate(wa_id: Any) -> bool:
    if not truthy(wa_id):
        return False
    now = time.time() * 1000
    if len(_processed) > 5000:
        for key in [k for k, exp in _processed.items() if exp <= now]:
            _processed.pop(key, None)
    expires = _processed.get(wa_id)
    return expires is not None and expires > now


def _mark(wa_id: Any) -> None:
    if truthy(wa_id):
        _processed[wa_id] = time.time() * 1000 + get_settings().webhook.dedupe_ttl_ms


async def process_whatsapp_webhook_job(body: Any, io) -> dict[str, Any] | None:
    try:
        entry = _g(body, "entry")
        user_cache: dict[Any, Any] = {}
        group_cache: dict[Any, Any] = {}

        async def get_sender(sender_from: Any) -> Any:
            if sender_from in user_cache:
                return user_cache[sender_from]
            res = await users_repo.get_user_info_in_view_by_filter({"full_whatsapp_number": sender_from}, SENDER_ATTRS)
            user_cache[sender_from] = res
            return res

        async def get_sender_group(sender_id: Any) -> Any:
            if sender_id in group_cache:
                return group_cache[sender_id]
            group = await group_repo.get_group_details(
                {"unique_name": sender_id, "platform": "whatsapp", "platform_two": "whatsapp"}, ["id", "friendly_name", "platform"]
            )
            group_cache[sender_id] = group
            return group

        inbound: dict[Any, list] = {}
        statuses: dict[Any, list] = {}
        if isinstance(entry, list):
            for ent in entry:
                changes = _g(ent, "changes")
                if not isinstance(changes, list):
                    continue
                for change in changes:
                    value = _g(change, "value")
                    if isinstance(value, dict) and isinstance(value.get("messages"), list):
                        for msg in value["messages"]:
                            inbound.setdefault(_g(msg, "from"), []).append(msg)
                    if isinstance(value, dict) and isinstance(value.get("statuses"), list):
                        for status in value["statuses"]:
                            statuses.setdefault(_g(status, "recipient_id"), []).append(status)

        def sender_payload(info: dict, response: dict) -> dict:
            return {"conversation_sid": response.get("id"), "profile_image": s3_prefixed(info.get("profile_image"))}

        async def process_inbound(msg: dict) -> None:
            sender_from = msg.get("from")
            wa_message_id = _g(msg, "id")
            msg_type = msg.get("type")
            if _is_duplicate(wa_message_id):
                return
            sender = await get_sender(sender_from)
            if not truthy(sender) or (isinstance(sender, list) and len(sender) == 0):
                return
            info = sender[0]
            response = await get_sender_group(info.get("id"))
            if not truthy(response):
                return
            media_type, message_type, media_link = "text", "text", ""
            if msg_type == "text":
                text = _g(msg, "text")
                content = _attr(text, "body") if truthy(text) else msg_type
            else:
                content = msg_type
            if msg_type in ("image", "video", "sticker", "audio", "document"):
                media = _g(msg, msg_type)
                if msg_type == "document":
                    content = _g(media, "caption") if truthy(media) else _g(media, "filename")
                elif msg_type == "audio":
                    content = _g(media, "caption") if truthy(media) else msg_type
                else:
                    content = _g(media, "caption") if truthy(media) else ""
                media_type = message_type = "file" if msg_type == "document" else msg_type
                ext = MIME_TO_EXT.get(_attr(media, "mime_type"), "bin")
                media_link = await whatsapp_service.download_media(_attr(media, "id"), f"py_chat_{int(time.time() * 1000)}.{ext}")
            elif msg_type == "button":
                button = _g(msg, "button")
                content = _g(button, "text") if truthy(button) else msg_type
            message_data: dict[str, Any] = {
                "group_id": response.get("id"), "user_id": info.get("user_id"), "sender_id": info.get("user_id"),
                "platform": "whatsapp", "content": content, "message_type": message_type, "media_type": media_type,
                "entity_id": None, "entity_type": None, "media_link": media_link, "created_by": info.get("user_id"),
                "created_at": now_aware(), "response_time": 0, "wa_message_id": wa_message_id,
            }
            messages = await aws_message_repo.create_message_with_multi_language(message_data)
            message_id = _g(messages, "insert_id")
            _mark(wa_message_id)
            for key in ("content_en", "content_id", "content_zh_cn", "content_zh_tw", "source_language"):
                message_data[key] = _g(messages, key)
            now = utcnow()
            await group_repo.update_group_by_id(
                {"last_message": content, "last_msgtype": message_type, "last_message_ceated_at": now,
                 "message_send_by": info.get("user_id"), "updated_at": now, "updated_by": info.get("user_id")},
                response.get("id"),
            )
            message_data.update(sender_payload(info, response) | {})
            message_data["read_status"] = "unread"
            message_data["id"] = message_id
            message_data["message_id"] = message_id
            message_data["entry"] = entry
            receiver = dict(message_data)
            receiver.update({
                "id": message_id, "message_id": message_id, "attachments": None, "guard_name": info.get("guard_name", UNDEFINED),
                "full_name": info.get("full_name", UNDEFINED), "read_by": "", "replies": "", "status": "sent", "wa_event_status": "no",
                "receiver_id": None,
                "mobile_number": js_string(info.get("whatsapp_country_code", UNDEFINED)) + js_string(info.get("whatsapp_number", UNDEFINED)),
            })
            room = parse_int(response.get("id"))
            await io.emit_to(room, "send_message", {"status": True, "message": "Success", "data": message_data})
            await io.emit_to(room, "receive_message", {"status": True, "message": "Success", "data": receiver})

        async def process_status(status: dict) -> None:
            wa_message_id = _g(status, "id")
            wa_status = _g(status, "status")
            sender = await get_sender(_g(status, "recipient_id"))
            if not truthy(sender) or (isinstance(sender, list) and len(sender) == 0):
                return
            info = sender[0]
            response = await get_sender_group(info.get("id"))
            if not truthy(response):
                return
            message = await aws_message_repo.get_message_by_waid(response.get("id"), wa_message_id, wa_status)
            if not truthy(message):
                return
            try:
                await aws_message_repo.update_message_read_status_by_waid(
                    {"group_id": response.get("id"), "wa_message_id": wa_message_id, "id": _g(message, "id")},
                    {"read_status": wa_status, "updated_by": info.get("id"), "created_at": _g(message, "created_at")},
                )
            except Exception as error:  # noqa: BLE001
                print(f"error.message {error}", flush=True)
                return
            message_data: dict[str, Any] = {
                "group_id": response.get("id"), "user_id": info.get("user_id"), "sender_id": info.get("user_id"),
                "platform": "whatsapp", "content": _g(message, "content"), "message_type": "text", "media_type": "text",
                "entity_id": None, "entity_type": None, "media_link": None, "created_by": info.get("user_id"),
                "created_at": now_aware(), "response_time": 0, "wa_message_id": wa_message_id, "message_id": _g(message, "id"),
            }
            message_data.update(sender_payload(info, response))
            message_data["read_status"] = wa_status
            message_data["id"] = _g(message, "id")
            message_data["entry"] = entry
            receiver = dict(message_data)
            receiver.update({
                "id": _g(message, "id"), "message_id": _g(message, "id"), "attachments": None,
                "guard_name": info.get("guard_name", UNDEFINED), "full_name": info.get("full_name", UNDEFINED),
                "read_by": "", "replies": "", "status": "sent", "wa_event_status": "yes", "receiver_id": None,
                "mobile_number": js_string(info.get("whatsapp_country_code", UNDEFINED)) + js_string(info.get("whatsapp_number", UNDEFINED)),
            })
            room = parse_int(response.get("id"))
            await io.emit_to(room, "receive_message_status", {"status": True, "message": "Success", "data": message_data})
            await io.emit_to(room, "receive_message_status", {"status": True, "message": "Success", "data": receiver})

        async def lane(items: list, fn, label: str) -> None:
            for item in items:
                try:
                    await fn(item)
                except Exception as error:  # noqa: BLE001
                    print(f"webhook {label} error: {error}", flush=True)

        concurrency = get_settings().webhook_fanout_concurrency
        await map_with_concurrency(list(inbound.values()), concurrency, lambda items: lane(items, process_inbound, "inbound message"))
        await map_with_concurrency(list(statuses.values()), concurrency, lambda items: lane(items, process_status, "status update"))
        return {"success": True, "message": "Webhook processed"}
    except Exception as error:  # noqa: BLE001
        print(f"error {error}", flush=True)
        return None


# ── outbound sends ──────────────────────────────────────────────────────────
async def _user_wa(user_id: Any, req) -> tuple[Any, dict | None]:
    rows = await users_repo.get_user_info_in_view(user_id, USER_ATTRS)
    if isinstance(rows, list) and len(rows) == 0:
        return {"success": False, "data": [], "message": "User not found"}, None
    info = rows[0] if isinstance(rows, list) else UNDEFINED
    if not truthy(_attr(info, "whatsapp_country_code")) and not truthy(_g(info, "whatsapp_number")):
        return {"success": False, "data": [], "message": req.t("WHATSAPP_NOT_VERIFIED_FOR_USER")}, None
    return None, info


async def send_whatsapp_message(req) -> dict[str, Any]:
    try:
        user_id, message = _g(req.body, "user_id"), _g(req.body, "message")
        last = await users_repo.get_details_by_user_id(user_id, ["id", "last_whatsapp_message_ceated_at"])
        failure, info = await _user_wa(user_id, req)
        if failure:
            return failure
        to_number = _replace(info.get("whatsapp_country_code")) + _replace(info.get("whatsapp_number"))
        content = strip_html(message)
        try:
            if not truthy(_attr(last, "last_whatsapp_message_ceated_at")):
                await whatsapp_service.send_welcome_message_whatsapp(to_number, "en", info.get("full_name"))
                await users_repo.update_details_by_user_id(user_id, {"last_whatsapp_message_ceated_at": utcnow()})
            response = await whatsapp_service.send_message_whatsapp(content, to_number)
            return {"success": True, "data": response.data, "message": req.t("SEND_MESSAGE_SUCCESS")}
        except Exception as error:  # noqa: BLE001
            return {"success": False, "data": [], "message": developer_message_or_message(error)}
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": str(error)}


async def send_otp_whatsapp(req) -> dict[str, Any]:
    try:
        user_id, otp, language = _g(req.body, "user_id"), _g(req.body, "otp"), _g(req.body, "language")
        failure, info = await _user_wa(user_id, req)
        if failure:
            return failure
        to_number = _replace(info.get("whatsapp_country_code")) + _replace(info.get("whatsapp_number"))
        try:
            response = await whatsapp_service.send_otp_message_whatsapp(otp, to_number, _lang(language))
            return {"success": True, "data": response.data, "message": req.t("SEND_MESSAGE_SUCCESS")}
        except Exception as error:  # noqa: BLE001
            return {"success": False, "data": [], "message": developer_message_or_message(error)}
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": str(error)}


async def send_otp_whatsapp_direct(req) -> dict[str, Any]:
    try:
        body = req.body
        to_number = _replace(_g(body, "whatsapp_country_code")) + js_string(_g(body, "whatsapp_number"))
        try:
            response = await whatsapp_service.send_otp_message_whatsapp(_g(body, "otp"), to_number, _lang(_g(body, "language")))
            return {"success": True, "data": response.data, "message": req.t("SEND_MESSAGE_SUCCESS")}
        except Exception as error:  # noqa: BLE001
            return {"success": False, "data": [], "message": developer_message_or_message(error)}
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": str(error)}


async def send_welcome_message_whatsapp(req) -> dict[str, Any]:
    try:
        body = req.body
        to_number = _replace(_g(body, "whatsapp_country_code")) + js_string(_g(body, "whatsapp_number"))
        try:
            response = await whatsapp_service.send_welcome_message_whatsapp(to_number, _lang(_g(body, "language")), "User")
            return {"success": True, "data": response, "message": req.t("SEND_MESSAGE_SUCCESS")}
        except Exception as error:  # noqa: BLE001
            return {"success": False, "data": [], "message": developer_message_or_message(error)}
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": str(error)}


async def send_whatsapp_welcome_message(req) -> dict[str, Any]:
    try:
        user_id = _g(req.body, "user_id")
        last = await users_repo.get_details_by_user_id(user_id, ["id", "last_whatsapp_message_ceated_at"])
        failure, info = await _user_wa(user_id, req)
        if failure:
            return failure
        to_number = _replace(info.get("whatsapp_country_code")) + _replace(info.get("whatsapp_number"))
        try:
            if not truthy(_attr(last, "last_whatsapp_message_ceated_at")):
                await whatsapp_service.send_welcome_message_whatsapp(to_number, "en", info.get("full_name"))
                await users_repo.update_details_by_user_id(user_id, {"last_whatsapp_message_ceated_at": utcnow()})
            return {"success": True, "data": {}, "message": req.t("SEND_MESSAGE_SUCCESS")}
        except Exception as error:  # noqa: BLE001
            return {"success": False, "data": [], "message": developer_message_or_message(error)}
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": str(error)}


async def validate_mobile(req) -> dict[str, Any]:
    try:
        body = req.body
        to_number = _replace(_g(body, "whatsapp_country_code")) + js_string(_g(body, "whatsapp_number"))
        try:
            await whatsapp_service.validate_whatsapp_number(to_number)
            return {"success": True, "data": {}, "message": "Success"}
        except Exception as error:  # noqa: BLE001
            return {"success": False, "data": [], "message": developer_message_or_message(error)}
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": str(error)}
