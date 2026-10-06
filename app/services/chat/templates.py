"""Template / action-required / chat-message send services (port of
aws.service.js: sendTemplateMessage, sendTemplateMessageAssignPartner,
getTemplateList, sendTemplateMessageActionRequired, sendChatMessage,
addActionRequired, sentMessageToUserActionRequired, sendWhatsAppTemplateMessage,
sendWhatsAppTemplateNotification, getTemplateBody).

Behaviour notes kept on purpose (see MIGRATION_REPORT.md §13/§14):
* sendTemplateMessage (in-app branches) and sendTemplateMessageActionRequired
  answer BEFORE the message is created: Node's inner promise chain was not
  awaited, so the response carries ``data: {}`` and the send runs in the
  background (here: a spawned task whose failures are logged, not crashing).
* sendChatMessage (whatsapp) sends the WhatsApp text, then fails with
  "entity_id is not defined" (undefined variables in the Node code).
* sendChatMessage (app) answers 500 after emitting when an FCM token exists
  for an employer/worker sender (undefined ``content`` variable in Node).
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from app.core.config import get_settings
from app.core.http import developer_message_or_message
from app.core.i18n import t_default
from app.core.js import UNDEFINED, js_string, loose_eq, parse_date, parse_int, strip_html, strip_phone, truthy
from app.core.tasks import spawn
from app.repositories import aws_message as aws_message_repo
from app.repositories import group as group_repo
from app.repositories import group_participant as gp_repo
from app.repositories import users as users_repo
from app.services import push
from app.services import whatsapp as whatsapp_service
from app.services.chat.common import hashids_encode, now_aware, s3_prefixed, utcnow
from app.services.chat.conversations import create_conversation, create_conversation_whatsapp_action_required
from app.utils.resolve_template import resolve_template_content

VIEW_ATTRS = (
    "CONCAT(country_code, mobile_number) as mobile_number,whatsapp_country_code,whatsapp_number,guard_name,"
    "related_name as full_name,guard_name,profile_image,user_id, user_id as id"
)
STAFF_WA_ATTRS = "whatsapp_country_code,whatsapp as whatsapp_number,user_id, user_id as id"
IMAGE_EXTS = ("jpg", "jpeg", "png", "gif", "bmp", "webp")


def _g(obj: Any, key: str, default: Any = UNDEFINED) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else default


def _first(rows: Any) -> Any:
    return rows[0] if isinstance(rows, list) and rows else UNDEFINED


def _req_attr(obj: Any, key: str) -> Any:
    """``obj.key`` where obj may be undefined -> TypeError like JS."""
    if obj is UNDEFINED or obj is None:
        raise TypeError(f"Cannot read properties of {js_string(obj)} (reading '{key}')")
    return obj.get(key, UNDEFINED) if isinstance(obj, dict) else UNDEFINED


def _response_time(last: Any) -> float:
    from app.socket.handlers import response_time_seconds

    return response_time_seconds(last)


def _apply_translations(message_data: dict, messages: Any, with_source: bool = True) -> None:
    for key in ("content_en", "content_id", "content_zh_cn", "content_zh_tw"):
        message_data[key] = _g(messages, key)
    if with_source:
        message_data["source_language"] = _g(messages, "source_language")


def _receiver_base(message_data: dict, message_id: Any, sender: Any) -> dict:
    receiver = dict(message_data)
    receiver["id"] = message_id
    receiver["message_id"] = message_id
    receiver["attachments"] = None
    receiver["guard_name"] = _g(sender, "guard_name")
    receiver["full_name"] = _g(sender, "full_name")
    return receiver


def _group_payload(content: Any, sender_id: Any, msgtype: Any = "text") -> dict:
    now = utcnow()
    return {"last_message": content, "last_msgtype": msgtype, "last_message_ceated_at": now,
            "message_send_by": sender_id, "updated_at": now, "updated_by": sender_id}


def _to_number(info: Any) -> str:
    cc = _g(info, "whatsapp_country_code")
    num = _g(info, "whatsapp_number")
    return strip_phone(cc if truthy(cc) else "") + strip_phone(num if truthy(num) else "")


def _media_type(path: str) -> str:
    return "image" if path.split(".")[-1].lower() in IMAGE_EXTS else "document"


async def _push_tokens(user_id: Any) -> list[str]:
    devices = await users_repo.get_user_devices_info_notification(user_id)
    return [d.get("device_token") for d in devices if d.get("device_token")] if isinstance(devices, list) else []


async def _in_app_template_send(io, group_ref: Any, sender: Any, platform: Any, template_content: Any, entity_id: Any,
                                entity_type: Any, template_file_path: Any, action_branch: bool) -> None:
    """The un-awaited ``.then`` chain of sendTemplateMessage (runs after the response)."""
    try:
        conversation_sid = _g(group_ref, "conversation_sid")
        response = await group_repo.get_group_details({"id": conversation_sid}, ["id", "friendly_name"])
        if not truthy(response):
            return
        participants = await gp_repo.get_group_participants_list(
            gp_repo.participant_routing_where({"group_id": conversation_sid}), ["id", "user_id"]
        )
        if not truthy(participants):
            return
        if not isinstance(participants, list):
            raise TypeError("responseParticipant?.filter is not a function")
        participants = [r for r in participants if not loose_eq(r.get("user_id"), _req_attr(sender, "id"))]
        first = participants[0] if participants else None
        if first is None:
            raise TypeError("Cannot read properties of undefined (reading 'user_id')")
        receiver_id = parse_int(first["user_id"])
        diff = _response_time(await aws_message_repo.get_last_message(conversation_sid)) if action_branch else 0
        now = now_aware()
        common = {
            "content": template_content, "message_type": "text", "media_type": "text",
            "entity_id": entity_id if truthy(entity_id) else None, "entity_type": entity_type if truthy(entity_type) else None,
            "media_link": template_file_path if truthy(template_file_path) else None,
            "created_by": sender["id"], "created_at": now, "response_time": diff,
        }
        if action_branch:
            message_data = {"group_id": conversation_sid, "user_id": sender["id"], "sender_id": sender["id"],
                            "platform": platform if truthy(platform) else "app", "receiver_id": receiver_id, **common}
        else:
            message_data = {"group_id": conversation_sid, "receiver_id": receiver_id, "user_id": sender["id"], "sender_id": sender["id"],
                            "platform": platform if truthy(platform) else "app", **common}
        messages = await aws_message_repo.create_message_with_multi_language(message_data)
        message_id = _g(messages, "insert_id")
        _apply_translations(message_data, messages, with_source=action_branch)
        await group_repo.update_group_by_id(_group_payload(template_content, sender["id"]), conversation_sid)
        message_data["conversation_sid"] = conversation_sid
        message_data["profile_image"] = s3_prefixed(_g(sender, "profile_image"))
        message_data["read_status"] = "unread"
        message_data["id"] = message_id
        message_data["message_id"] = message_id
        receiver = _receiver_base(message_data, message_id, sender)
        receiver.update({"read_by": "", "replies": "", "status": "sent", "isInAppTemplates": "yes", "receiver_id": receiver_id,
                         "mobile_number": js_string(first.get("usersModel.country_code", UNDEFINED)) + js_string(first.get("usersModel.mobile_number", UNDEFINED))})
        await io.emit_to(conversation_sid, "send_message", {"status": True, "message": "Success", "data": receiver})
        await io.emit_to(conversation_sid, "receive_message", {"status": True, "message": "Success", "data": receiver})
        if _g(sender, "guard_name") in ("employer", "worker"):
            tokens = await _push_tokens(receiver_id)
            if tokens:
                spawn(push.send_fcm_notification(tokens, t_default("CHAT_NOTIFICATION_TITLE"), template_content,
                                                  {"event_id": conversation_sid, "user_id": sender["id"], "event_type": "chat_app"}))
    except Exception as error:  # noqa: BLE001 - Node: unhandled rejection
        print(f"sendTemplateMessage background error: {error}", flush=True)


async def send_template_message(req) -> dict[str, Any]:
    body = req.body
    platform, friend_id, friend_type = _g(body, "platform"), _g(body, "friend_id"), _g(body, "friend_type")
    entity_id, entity_type = _g(body, "entity_id"), _g(body, "entity_type")
    template_file_path, sender_id = _g(body, "template_file_path"), _g(body, "sender_id")
    user_info = req.user_details
    template_content = await resolve_template_content({
        "template_content": _g(body, "template_content"), "friend_type": friend_type, "friend_id": friend_id,
        "sender_user_id": sender_id if truthy(sender_id) else _g(user_info, "id"),
    })
    if platform == "whatsapp":
        try:
            if friend_type != "user":
                receiver_rows = await users_repo.get_user_info_in_view(friend_id, VIEW_ATTRS)
            else:
                receiver_rows = await users_repo.get_user_staff_info_in_view(friend_id, STAFF_WA_ATTRS)
            sender_rows = await users_repo.get_user_info_in_view(user_info["id"], VIEW_ATTRS)
            group_response = await create_conversation_whatsapp_action_required(req)
            sender = _first(sender_rows)
            if not group_response.get("success"):
                return {"success": False, "data": [], "message": group_response.get("message") or req.t("SEND_MESSAGE_FAILED")}
            group_info = group_response["data"]
            receiver_info = _first(receiver_rows)
            to_number = _to_number(receiver_info)
            content = strip_html(template_content)
            last = await users_repo.get_details_by_user_id(friend_id, ["id", "last_whatsapp_message_ceated_at"])
            if not truthy(_req_attr(last, "last_whatsapp_message_ceated_at")):
                await whatsapp_service.send_welcome_message_whatsapp(to_number, "en", _g(receiver_info, "full_name"))
                await users_repo.update_details_by_user_id(friend_id, {"last_whatsapp_message_ceated_at": utcnow()})
            await whatsapp_service.send_text_message_whatsapp(content, to_number)
            if truthy(template_file_path):
                await whatsapp_service.send_media_message_whatsapp(template_file_path, _media_type(template_file_path), to_number)
            message_data = {
                "group_id": group_info.get("id"), "user_id": _req_attr(sender, "id"), "sender_id": sender["id"],
                "receiver_id": parse_int(friend_id), "platform": platform if truthy(platform) else "app",
                "content": template_content, "message_type": "text", "media_type": "text",
                "entity_id": entity_id if truthy(entity_id) else None, "entity_type": entity_type if truthy(entity_type) else None,
                "media_link": template_file_path if truthy(template_file_path) else None,
                "created_by": sender["id"], "created_at": now_aware(), "response_time": 0,
            }
            messages = await aws_message_repo.create_message_with_multi_language(message_data)
            message_id = _g(messages, "insert_id")
            _apply_translations(message_data, messages)
            payload = _group_payload(template_content, sender["id"])
            payload["platform_two"] = "whatsapp"
            await group_repo.update_group_by_id(payload, group_info.get("id"))
            message_data["conversation_sid"] = group_info.get("id")
            message_data["profile_image"] = sender.get("profile_image") if truthy(sender.get("profile_image")) else get_settings().default_profile_image
            message_data["read_status"] = "unread"
            message_data["id"] = message_id
            message_data["message_id"] = message_id
            receiver = _receiver_base(message_data, message_id, sender)
            receiver.update({"read_by": "", "replies": "", "status": "sent", "receiver_id": parse_int(group_info.get("unique_name")),
                             "mobile_number": js_string(_g(receiver_info, "whatsapp_country_code")) + js_string(_g(receiver_info, "whatsapp_number"))})
            await req.io.emit_to(receiver["conversation_sid"], "send_message", {"status": True, "message": "Success", "data": receiver})
            await req.io.emit_to(receiver["conversation_sid"], "receive_message", {"status": True, "message": "Success", "data": receiver})
            return {"success": True, "data": receiver, "message": req.t("SEND_MESSAGE_SUCCESS")}
        except Exception as error:  # noqa: BLE001
            return {"success": False, "data": [], "message": developer_message_or_message(error)}

    if entity_type == "template_action_required":
        valid = await group_repo.check_duplicate_group_action(friend_id)
        sender_rows = await users_repo.get_user_info_in_view(sender_id if truthy(sender_id) else user_info["id"], VIEW_ATTRS)
        if not truthy(valid):
            req.body["platform"] = "action_required"
            if truthy(sender_id) and isinstance(sender_rows, list) and sender_rows:
                req.user_details = sender_rows[0]
            group_response = await create_conversation(req)
            if group_response.get("success"):
                valid = group_response["data"]
        if truthy(valid) and truthy(sender_rows):
            spawn(_in_app_template_send(req.io, valid, _first(sender_rows), platform, template_content, entity_id, entity_type,
                                        template_file_path, action_branch=True))
            return {"success": True, "data": {}, "message": req.t("SEND_MESSAGE_SUCCESS")}
        return {"success": False, "data": [], "message": req.t("SEND_MESSAGE_FAILED")}

    sender_rows = await users_repo.get_user_info_in_view(sender_id if truthy(sender_id) else user_info["id"], VIEW_ATTRS)
    if truthy(sender_id) and isinstance(sender_rows, list) and sender_rows:
        req.user_details = sender_rows[0]
    group_response = await create_conversation(req)
    if truthy(sender_rows):
        group_ref = group_response.get("data")
        if group_ref is None:
            raise TypeError("Cannot read properties of null (reading 'conversation_sid')")  # -> next(error) -> 500
        spawn(_in_app_template_send(req.io, group_ref, _first(sender_rows), platform, template_content, entity_id, entity_type,
                                    template_file_path, action_branch=False))
        return {"success": True, "data": {}, "message": req.t("SEND_MESSAGE_SUCCESS")}
    return {"success": False, "data": [], "message": req.t("SEND_MESSAGE_FAILED")}


async def send_template_message_assign_partner(req) -> dict[str, Any]:
    body = req.body
    platform, friend_id, friend_type = _g(body, "platform"), _g(body, "friend_id"), _g(body, "friend_type")
    template_content, entity_type = _g(body, "template_content"), _g(body, "entity_type")
    template_file_path = _g(body, "template_file_path")
    worker_name, nationality, reference_id = _g(body, "worker_name"), _g(body, "nationality"), _g(body, "reference_id")
    if platform != "whatsapp":
        return {"success": False, "data": [], "message": "Only whatsapp platform is supported for this action."}
    try:
        if friend_type != "user":
            receiver_rows = await users_repo.get_user_info_in_view(friend_id, VIEW_ATTRS)
        else:
            receiver_rows = await users_repo.get_user_staff_info_in_view(friend_id, STAFF_WA_ATTRS)
        sender_rows = await users_repo.get_user_info_in_view(req.user_details["id"], VIEW_ATTRS)
        group_response = await create_conversation_whatsapp_action_required(req)
        sender = _first(sender_rows)
        if not group_response.get("success"):
            return {"success": False, "data": [], "message": group_response.get("message") or req.t("SEND_MESSAGE_FAILED")}
        group_info = group_response["data"]
        receiver_info = _first(receiver_rows)
        to_number = _to_number(receiver_info)
        last = await users_repo.get_details_by_user_id(friend_id, ["id", "last_whatsapp_message_ceated_at"])
        if not truthy(_req_attr(last, "last_whatsapp_message_ceated_at")):
            await users_repo.update_details_by_user_id(friend_id, {"last_whatsapp_message_ceated_at": utcnow()})
        await whatsapp_service.send_assign_pt_message_whatsapp(to_number, "en", worker_name, nationality, reference_id)
        if truthy(template_file_path):
            await whatsapp_service.send_media_message_whatsapp(template_file_path, _media_type(template_file_path), to_number)
        message_data = {
            "group_id": group_info.get("id"), "user_id": _req_attr(sender, "id"), "sender_id": sender["id"],
            "receiver_id": parse_int(friend_id), "platform": platform if truthy(platform) else "app",
            "content": template_content, "message_type": "text", "media_type": "text", "entity_id": None,
            "entity_type": entity_type if truthy(entity_type) else None,
            "media_link": template_file_path if truthy(template_file_path) else None,
            "created_by": sender["id"], "created_at": now_aware(), "response_time": 0,
        }
        messages = await aws_message_repo.create_message_with_multi_language(message_data)
        message_id = _g(messages, "insert_id")
        _apply_translations(message_data, messages)
        payload = _group_payload(template_content, sender["id"])
        payload["platform_two"] = "whatsapp"
        await group_repo.update_group_by_id(payload, group_info.get("id"))
        message_data["conversation_sid"] = group_info.get("id")
        message_data["profile_image"] = sender.get("profile_image") if truthy(sender.get("profile_image")) else get_settings().default_profile_image
        message_data["read_status"] = "unread"
        message_data["id"] = message_id
        message_data["message_id"] = message_id
        receiver = _receiver_base(message_data, message_id, sender)
        receiver.update({"read_by": "", "replies": "", "status": "sent", "receiver_id": parse_int(group_info.get("unique_name")),
                         "mobile_number": js_string(_g(receiver_info, "whatsapp_country_code")) + js_string(_g(receiver_info, "whatsapp_number"))})
        return {"success": True, "data": receiver, "message": req.t("SEND_MESSAGE_SUCCESS")}
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": developer_message_or_message(error)}


async def get_template_list(req) -> dict[str, Any]:
    result = await whatsapp_service.get_template_list(_g(req.query, "name"), _g(req.query, "language"))
    return {"success": True, "data": UNDEFINED if result is None else result, "message": req.t("TEMPLATE_LIST_FETCHED")}


async def _action_required_background(check: dict, sender: Any, receiver_rows: Any, content_row: dict, entity_type: str,
                                      entity_name: str, user_id: Any) -> None:
    try:
        conversation_sid = check.get("conversation_sid")
        response = await group_repo.get_group_details({"id": conversation_sid}, ["id", "friendly_name"])
        if not truthy(response):
            return
        participants = await gp_repo.get_group_participants_list(
            gp_repo.participant_routing_where({"group_id": conversation_sid}), ["id", "user_id"]
        )
        if not truthy(participants):
            return
        receiver_name = _req_attr(_first(receiver_rows), "full_name")
        template_content = re.sub(r"{{\s*worker_name\s*}}", lambda _m: js_string(receiver_name) if truthy(receiver_name) else "N/A",
                                  content_row["template_content"])
        file_path = content_row.get("template_file_path") or None
        message_data = {
            "group_id": conversation_sid, "user_id": sender["id"], "sender_id": sender["id"], "platform": "action_required",
            "content": template_content, "message_type": "text", "media_type": "text",
            "entity_id": content_row.get("id") if truthy(content_row.get("id")) else None, "entity_type": entity_type,
            "entity_name": entity_name, "media_link": file_path if truthy(file_path) else None,
            "created_by": sender["id"], "created_at": now_aware(), "response_time": 0,
        }
        messages = await aws_message_repo.create_message_with_multi_language(message_data)
        _apply_translations(message_data, messages)
        await group_repo.update_group_by_id(_group_payload(template_content, sender["id"]), conversation_sid)
        tokens = await _push_tokens(parse_int(user_id))
        if tokens:
            spawn(push.send_fcm_notification(tokens, t_default("CHAT_NOTIFICATION_TITLE"), template_content,
                                              {"event_id": conversation_sid, "user_id": sender["id"], "event_type": "chat_app"}))
    except Exception as error:  # noqa: BLE001
        print(f"sendTemplateMessageActionRequired background error: {error}", flush=True)


async def send_template_message_action_required(req) -> dict[str, Any]:
    body = req.body
    user_id, language = _g(body, "user_id"), _g(body, "language")
    reason, action_module = _g(body, "reason"), _g(body, "action_module")
    req.body["platform"] = "action_required"
    req.body["friend_type"] = "worker"
    req.body["friend_id"] = user_id
    if action_module == "passport":
        entity_type = "passport_lost_expired_action_required"
        entity_name = "Doc Upload - Passport Lost/expired Indo (Action Required)"
    else:
        entity_type = "birth_certificate_dont_have_action_required"
        entity_name = "Doc Upload - Birth Certificate Dont have (Action Required)"
        if reason == 2:
            entity_type = "birth_certificate_date_not_matched_action_required"
            entity_name = "Doc Upload - Birth Certificate Date not match (Action Required)"
    sender_rows = await users_repo.get_user_info_in_view(
        1, "country_code, mobile_number,whatsapp_country_code,whatsapp_number,guard_name,related_name as full_name,guard_name,profile_image,user_id, user_id as id,role_id"
    )
    req.user_details = _first(sender_rows) if isinstance(sender_rows, list) else UNDEFINED
    check = await create_conversation_whatsapp_action_required(req)
    receiver_rows = await users_repo.get_user_info_in_view(user_id, VIEW_ATTRS + ",role_id")
    content_rows = await users_repo.get_template_category_content(language, action_module, reason)
    if isinstance(content_rows, list) and len(content_rows) == 0:
        return {"success": False, "data": [], "message": req.t("CONTENT_NOT_FOUND")}
    if check.get("success") and truthy(sender_rows):
        spawn(_action_required_background(check["data"], _first(sender_rows), receiver_rows, _first(content_rows), entity_type, entity_name, user_id))
        return {"success": True, "data": {}, "message": req.t("SEND_MESSAGE_SUCCESS")}
    return {"success": False, "data": [], "message": req.t("SEND_MESSAGE_FAILED")}


async def send_chat_message(req) -> dict[str, Any]:
    body = req.body
    platform, user_id = _g(body, "platform"), _g(body, "user_id")
    user_type, message = _g(body, "user_type"), _g(body, "message")
    user_info = req.user_details
    if platform == "whatsapp":
        try:
            req.body["friend_type"] = user_type
            req.body["friend_id"] = user_id
            receiver_rows = await users_repo.get_user_info_in_view(user_id, VIEW_ATTRS)
            await users_repo.get_user_info_in_view(user_info["id"], VIEW_ATTRS)
            group_response = await create_conversation(req)
            if not group_response.get("success"):
                return {"success": False, "data": [], "message": group_response.get("message") or req.t("SEND_MESSAGE_FAILED")}
            to_number = _to_number(_first(receiver_rows))
            await whatsapp_service.send_text_message_whatsapp(strip_html(message), to_number)
            # Node built messageData with undefined `entity_id` -> ReferenceError (caught below).
            raise NameError("entity_id is not defined")
        except Exception as error:  # noqa: BLE001
            return {"success": False, "data": [], "message": developer_message_or_message(error)}

    req.body["friend_type"] = user_type
    req.body["friend_id"] = user_id
    group_response = await create_conversation(req)
    if not group_response.get("success"):
        return {"success": False, "data": [], "message": group_response.get("message") or req.t("SEND_MESSAGE_FAILED")}
    sender_rows = await users_repo.get_user_info_in_view(user_info["id"], VIEW_ATTRS)
    if not truthy(sender_rows):
        return {"success": False, "data": [], "message": group_response.get("message") or req.t("SEND_MESSAGE_FAILED")}
    group = group_response["data"]
    sender = _first(sender_rows)
    conversation_sid = _req_attr(group, "conversation_sid")
    participants = await gp_repo.get_group_participants_list(
        gp_repo.participant_routing_where({"group_id": conversation_sid}), ["id", "user_id"]
    )
    if not truthy(participants):
        return None
    if not isinstance(participants, list):
        raise TypeError("responseParticipant?.filter is not a function")
    participants = [r for r in participants if not loose_eq(r.get("user_id"), _req_attr(sender, "id"))]
    if not participants:
        raise TypeError("Cannot read properties of undefined (reading 'user_id')")
    first = participants[0]
    message_data = {
        "group_id": conversation_sid, "user_id": sender["id"], "sender_id": sender["id"],
        "platform": platform if truthy(platform) else "app", "content": message, "message_type": "text", "media_type": "text",
        "entity_id": None, "entity_type": None, "media_link": None, "created_by": sender["id"], "created_at": now_aware(),
        "response_time": 0,
    }
    messages = await aws_message_repo.create_message_with_multi_language(message_data)
    message_id = _g(messages, "insert_id")
    _apply_translations(message_data, messages)
    await group_repo.update_group_by_id(_group_payload(message, sender["id"]), conversation_sid)
    message_data["conversation_sid"] = conversation_sid
    message_data["profile_image"] = s3_prefixed(sender.get("profile_image"))
    message_data["read_status"] = "unread"
    message_data["id"] = message_id
    message_data["message_id"] = message_id
    receiver = _receiver_base(message_data, message_id, sender)
    receiver.update({"read_by": "", "replies": "", "status": "sent", "receiver_id": parse_int(first["user_id"]),
                     "mobile_number": js_string(first.get("usersModel.country_code", UNDEFINED)) + js_string(first.get("usersModel.mobile_number", UNDEFINED)),
                     "groupInfo": group})
    receiver_info = await users_repo.get_user_info_in_view(first["user_id"], "employer_worker_staff_common_id")
    if isinstance(receiver_info, list) and len(receiver_info) > 0:
        receiver["employer_worker_staff_common_id"] = receiver_info[0].get("employer_worker_staff_common_id")
    await req.io.emit_to(conversation_sid, "send_message", {"status": True, "message": "Success", "data": message_data})
    await req.io.emit_to(conversation_sid, "receive_message", {"status": True, "message": "Success", "data": receiver})
    await req.io.emit_to(receiver["receiver_id"], "receive_message_all", {"status": True, "message": "Success", "data": receiver})
    if sender.get("guard_name") in ("employer", "worker"):
        tokens = await _push_tokens(parse_int(first["user_id"]))
        if tokens:
            raise NameError("content is not defined")  # Node: undefined `content` -> next(error) -> 500
    return {"success": True, "data": receiver, "message": req.t("SEND_MESSAGE_SUCCESS")}


async def add_action_required(req) -> dict[str, Any]:
    try:
        user = req.user_details
        user_id = _g(req.body, "user_id")
        req.body["platform"] = "action_required"
        req.body["friend_type"] = "worker"
        req.body["friend_id"] = user_id
        req.body["message_type"] = "text"
        req.body["content"] = await resolve_template_content({
            "template_content": _g(req.body, "content"), "friend_type": _g(req.body, "entity_type"),
            "friend_id": user_id, "sender_user_id": _g(user, "id"),
        })
        group = await create_conversation_whatsapp_action_required(req)
        if group.get("success"):
            return await sent_message_to_user_action_required(req.body, user, group["data"], req.io)
        return {"success": False, "data": [], "message": group.get("message")}
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": str(error)}


async def sent_message_to_user_action_required(data: dict, is_user: dict, group_info: dict, io) -> dict[str, Any]:
    conversation_sid = parse_int(_g(group_info, "id"))
    try:
        receiver_rows = await users_repo.get_user_info_in_view(_g(group_info, "unique_name"), VIEW_ATTRS)
        template_info = await users_repo.get_template_info(_g(data, "action_type"), "name_en")
        if not truthy(receiver_rows) or (isinstance(receiver_rows, list) and len(receiver_rows) == 0):
            await io.emit_to(conversation_sid, "receive_message", {"status": False, "message": t_default("INVALID_RECEIVER"), "data": {}})
            return {"success": False, "data": [], "message": t_default("INVALID_RECEIVER")}
        receiver = _first(receiver_rows)
        template_name = template_info[0].get("name_en") if isinstance(template_info, list) and template_info else ""
        media_type = _g(data, "media_type")
        media_link = _g(data, "media_link")
        message_data = {
            "group_id": conversation_sid, "user_id": is_user.get("id"), "sender_id": is_user.get("id"),
            "receiver_id": parse_int(_g(group_info, "unique_name")), "platform": "action_required",
            "content": _g(data, "content") if truthy(_g(data, "content")) else None,
            "message_type": _g(data, "message_type") if truthy(_g(data, "message_type")) else "text",
            "media_type": media_type if truthy(media_type) else "text",
            "entity_id": _g(data, "entity_id") if truthy(_g(data, "entity_id")) else None,
            "entity_type": _g(data, "action_type") if truthy(_g(data, "action_type")) else None,
            "entity_name": template_name,
            "media_link": media_link if truthy(media_link) and media_type != "text" else None,
            "created_by": is_user.get("id"), "created_at": now_aware(), "response_time": 0,
        }
        messages = await aws_message_repo.create_message_with_multi_language(message_data)
        message_id = _g(messages, "insert_id")
        _apply_translations(message_data, messages)
        payload = _group_payload(_g(data, "content"), is_user.get("id"), _g(data, "message_type"))
        payload["platform_two"] = "action_required"
        await group_repo.update_group_by_id(payload, conversation_sid)
        message_data["conversation_sid"] = conversation_sid
        profile = is_user.get("profile_image", UNDEFINED)
        message_data["profile_image"] = profile if truthy(profile) else get_settings().default_profile_image
        message_data["read_status"] = "unread"
        message_data["id"] = message_id
        message_data["message_id"] = message_id
        receiver_data = _receiver_base(message_data, message_id, is_user)
        receiver_data.update({"read_by": "", "replies": "", "status": 1, "receiver_id": parse_int(_g(group_info, "unique_name")),
                              "mobile_number": _g(receiver, "mobile_number")})
        await io.emit_to(conversation_sid, "send_message", {"status": True, "message": "Success", "data": message_data})
        await io.emit_to(conversation_sid, "receive_message", {"status": True, "message": "Success", "data": receiver_data})
        return {"success": True, "data": receiver_data, "message": "Successfully sent message"}
    except Exception as error:  # noqa: BLE001
        message = developer_message_or_message(error)
        await io.emit_to(conversation_sid, "receive_message", {"status": False, "message": message})
        return {"success": False, "data": [], "message": message}


def _template_text(template_info: dict) -> str:
    components = template_info.get("components") or []
    body = next((c for c in components if c.get("type") == "BODY"), None)
    header = next((c for c in components if c.get("type") == "HEADER"), None)
    return body, header


async def send_whatsapp_template_message(req) -> dict[str, Any]:
    body = req.body
    friend_id, friend_type = _g(body, "friend_id"), _g(body, "friend_type")
    sender_id, template_id = _g(body, "sender_id"), _g(body, "template_id")
    custom_params, language = _g(body, "custom_content_params"), _g(body, "language")
    platform = "whatsapp"
    req.body["platform"] = "whatsapp"
    template_content: Any = _g(body, "template_content")
    try:
        if friend_type != "user":
            receiver_rows = await users_repo.get_user_info_in_view(friend_id, VIEW_ATTRS)
        else:
            receiver_rows = await users_repo.get_user_staff_info_in_view(friend_id, STAFF_WA_ATTRS)
        sender_rows = await users_repo.get_user_info_in_view(sender_id if truthy(sender_id) else req.user_details["id"], VIEW_ATTRS + ", email")
        group_response = await create_conversation_whatsapp_action_required(req)
        sender = _first(sender_rows)
        if not group_response.get("success"):
            return {"success": False, "data": [], "message": group_response.get("message") or req.t("SEND_MESSAGE_FAILED")}
        group_info = group_response["data"]
        receiver_info = _first(receiver_rows)
        if _g(receiver_info, "whatsapp_country_code") == "" or _g(receiver_info, "whatsapp_number") == "":
            return {"success": False, "data": [], "message": req.t("WHATSAPP_NOT_VERIFIED_FOR_USER")}
        to_number = _to_number(receiver_info)
        template_info = await whatsapp_service.get_template_details(template_id)
        if not truthy(template_info):
            return {"success": False, "data": [], "message": req.t("TEMPLATE_NOT_FOUND")}
        params = json.loads(custom_params) if truthy(custom_params) else {}
        components: list[dict] = [{"type": "body"}]
        if template_info.get("name") == "welcome_message_template":
            if not truthy(_g(params, "name")):
                return {"success": False, "data": [], "message": "Custom content param name is required"}
            components.append({"type": "header", "parameters": [{"type": "text", "text": params["name"] if truthy(params.get("name")) else "Dear User"}]})
        await whatsapp_service.send_template_message_whatsapp(to_number, template_info, components, language)
        if template_info.get("components"):
            body_c, header_c = _template_text(template_info)
            template_content = body_c.get("text") if body_c else ""
            if header_c and header_c.get("format") == "TEXT":
                name = _g(params, "name")
                template_content = js_string(header_c.get("text")).replace("{{1}}", js_string(name) if truthy(name) else "", 1) + "\n" + js_string(template_content)
        message_data = {
            "group_id": group_info.get("id"), "user_id": _req_attr(sender, "id"), "sender_id": sender["id"],
            "receiver_id": parse_int(friend_id), "platform": platform, "content": template_content,
            "message_type": "text", "media_type": "text", "entity_id": template_info.get("name"), "entity_type": "template",
            "media_link": None, "created_by": sender["id"], "created_at": now_aware(), "response_time": 0,
        }
        messages = await aws_message_repo.create_message_with_multi_language(message_data)
        message_id = _g(messages, "insert_id")
        _apply_translations(message_data, messages)
        payload = _group_payload(template_content, sender["id"])
        payload["platform_two"] = "whatsapp"
        await group_repo.update_group_by_id(payload, group_info.get("id"))
        await users_repo.update_details_by_user_id(friend_id, {"last_whatsapp_message_ceated_at": utcnow()})
        message_data["conversation_sid"] = group_info.get("id")
        message_data["profile_image"] = sender.get("profile_image") if truthy(sender.get("profile_image")) else get_settings().default_profile_image
        message_data["read_status"] = "unread"
        message_data["id"] = message_id
        message_data["message_id"] = message_id
        receiver = _receiver_base(message_data, message_id, sender)
        receiver.update({"email": _g(sender, "email"), "read_by": "", "replies": "", "status": "sent", "isWhatsTemplate": "yes",
                         "receiver_id": parse_int(group_info.get("unique_name")),
                         "mobile_number": js_string(_g(receiver_info, "whatsapp_country_code")) + js_string(_g(receiver_info, "whatsapp_number"))})
        await req.io.emit_to(receiver["conversation_sid"], "send_message", {"status": True, "message": "Success", "data": receiver})
        await req.io.emit_to(receiver["conversation_sid"], "receive_message", {"status": True, "message": "Success", "data": receiver})
        return {"success": True, "data": receiver, "message": req.t("SEND_MESSAGE_SUCCESS")}
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": developer_message_or_message(error)}


def _fail(message: str) -> dict[str, Any]:
    return {"success": False, "data": [], "message": message}


def _require(params: dict, *names: str) -> dict[str, Any] | None:
    for name in names:
        if not truthy(_g(params, name)):
            return _fail(f"Custom content param {name} is required")
    return None


def _button_param(text: str) -> dict[str, Any]:
    return {"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": text}]}


def _link(button: Any, query: str, trailing_newline: bool = False) -> str:
    if not button:
        return ""
    url = js_string(button["buttons"][0]["url"]).replace("{{1}}", query, 1)
    return "\n<a target='_blank' href='" + url + "'>" + js_string(button["buttons"][0]["text"]) + "</a>" + ("\n" if trailing_newline else "")


def get_template_body(template_info: dict, params: dict) -> dict[str, Any]:
    """Port of getTemplateBody: WhatsApp components + the stored chat text per template."""
    h = hashids_encode
    p = params or {}
    name = template_info.get("name")
    components: list[dict] = []
    content = ""
    comps = template_info.get("components")

    def find(kind: str) -> Any:
        if comps is None:
            raise TypeError("Cannot read properties of undefined (reading 'find')")
        return next((c for c in comps if c.get("type") == kind), None)

    def body_text() -> str:
        body = find("BODY")
        if body is None:
            raise TypeError("Cannot read properties of undefined (reading 'text')")
        return js_string(body.get("text"))

    std_query = f"?worker_id={h(_g(p, 'worker_id'))}&job_id={h(_g(p, 'job_id'))}&employer_id={h(_g(p, 'employer_id'))}"

    def v(key: str, fallback: str = "") -> str:
        value = _g(p, key)
        return js_string(value) if truthy(value) else fallback

    if name == "welcome_message_template":
        if err := _require(p, "name"):
            return err
        components += [{"type": "body"}, {"type": "header", "parameters": [{"type": "text", "text": v("name", "Dear User")}]}]
    elif name in ("job_apply", "job_matched"):
        if err := _require(p, "name", "worker_id", "job_id", "employer_id"):
            return err
        if name == "job_apply":
            components.append({"type": "body", "parameters": [{"type": "text", "text": v("name", "User")}]})
            components.append(_button_param(std_query))
        else:
            components.append({"type": "body"})
            components.append(_button_param(f"?page=interview&worker_id={h(_g(p, 'worker_id'))}&job_id={h(_g(p, 'job_id'))}&employer_id={h(_g(p, 'employer_id'))}"))
        button = find("BUTTONS")
        content = body_text().replace("{{1}}", v("name"), 1) + "\n" + content
        content += _link(button, std_query)
    elif name == "apply_job_after_suggestion":
        if err := _require(p, "name", "worker_id", "job_id", "employer_id"):
            return err
        components.append({"type": "body", "parameters": [{"type": "text", "text": v("agency_name", "Overseas Group Inc")}, {"type": "text", "text": v("name", "User")}]})
        components.append(_button_param(f"?page=interview&worker_id={h(_g(p, 'worker_id'))}&job_id={h(_g(p, 'job_id'))}&employer_id={h(_g(p, 'employer_id'))}"))
        button = find("BUTTONS")
        content = body_text().replace("{{1}}", v("agency_name"), 1).replace("{{2}}", v("name"), 1) + "\n" + content
        content += _link(button, std_query)
    elif name == "upcoming_interview_instant":
        if err := _require(p, "interview_date", "interview_time", "interview_details", "interview_location", "interview_link"):
            return err
        keys = ["interview_date", "interview_time", "interview_details", "interview_location", "interview_link"]
        components.append({"type": "body", "parameters": [{"type": "text", "text": v(k)} for k in keys]})
        button = find("BUTTONS")
        text = body_text()
        for index, key in enumerate(keys, start=1):
            text = text.replace("{{" + str(index) + "}}", v(key), 1)
        content = text + "\n" + content
        content += _link(button, std_query)
    elif name == "reschedule_request_send_by_worker":
        if err := _require(p, "interview_date", "interview_time", "interview_id"):
            return err
        components.append({"type": "body", "parameters": [{"type": "text", "text": v("interview_date")}, {"type": "text", "text": v("interview_time")}]})
        components.append(_button_param(f"?page=interview&worker_id={h(_g(p, 'worker_id'))}&job_id={h(_g(p, 'job_id'))}&interview_id={h(_g(p, 'interview_id'))}"))
        button = find("BUTTONS")
        content = body_text().replace("{{1}}", v("interview_date"), 1).replace("{{2}}", v("interview_time"), 1) + "\n" + content
        content += _link(button, std_query, trailing_newline=True)
    elif name == "interview_cancelled_by_worker":
        if err := _require(p, "interview_date", "interview_time", "worker_name"):
            return err
        components.append({"type": "body", "parameters": [{"type": "text", "text": v(k)} for k in ("interview_date", "interview_time", "worker_name")]})
        content = body_text().replace("{{1}}", v("interview_date"), 1).replace("{{2}}", v("interview_time"), 1).replace("{{3}}", v("worker_name"), 1) + "\n" + content
    elif name == "job_opened_after_4_days_notification_to_employer":
        components.append(_button_param(f"?job_id={h(_g(p, 'job_id'))}&employer_id={h(_g(p, 'employer_id'))}"))
        body, button = find("BODY"), find("BUTTONS")
        content = js_string(body.get("text")) if body and truthy(body.get("text")) else ""
        content += _link(button, f"job_id={h(_g(p, 'job_id'))}&employer_id={h(_g(p, 'employer_id'))}", trailing_newline=True)
    elif name in ("notification_to_worker_for_not_completing_registration", "notification_to_worker_for_not_uploading_photo",
                  "visa_expiry_date_expired", "contract_end_date_expired"):
        components.append(_button_param(f"?worker_id={h(_g(p, 'worker_id'))}&job_id={h(_g(p, 'worker_id'))}"))
        body, button = find("BODY"), find("BUTTONS")
        content = js_string(body.get("text")) if body and truthy(body.get("text")) else ""
        content += _link(button, f"?worker_id={h(_g(p, 'worker_id'))}", trailing_newline=True)
    elif name == "upcoming_interview_24_hour_prior":
        if err := _require(p, "worker_id", "job_id"):
            return err
        components.append({"type": "body", "parameters": [{"type": "text", "text": v("interview_details")}, {"type": "text", "text": v("interview_location")}]})
        query = f"?page=interview&worker_id={h(_g(p, 'worker_id'))}&job_id={h(_g(p, 'job_id'))}"
        components.append(_button_param(query))
        button = find("BUTTONS")
        content = body_text().replace("{{1}}", v("name"), 1) + "\n" + content
        content += _link(button, query)
    elif name == "job_offer_expire_soon":
        if err := _require(p, "job_id", "employer_id"):
            return err
        components.append({"type": "body"})
        components.append(_button_param(std_query))
        button = find("BUTTONS")
        content = body_text().replace("{{1}}", v("name"), 1) + "\n" + content
        content += _link(button, std_query)
    return {"success": True, "data": components, "content": content, "message": "Success"}


async def send_whatsapp_template_notification(req) -> dict[str, Any]:
    body = req.body
    user_id, user_type = _g(body, "user_id"), _g(body, "user_type")
    sender_id, message_type = _g(body, "sender_id"), _g(body, "message_type")
    custom_params, language = _g(body, "custom_content_params"), _g(body, "language")
    if not truthy(sender_id):
        sender_id = 1
    friend_type, friend_id = user_type, user_id
    language_code = "zh_HK" if language == "zh_chs" else ("zh_CN" if language == "zh_hant" else language)
    req.body["platform"] = "whatsapp"
    req.body["friend_id"] = friend_id
    req.body["friend_type"] = friend_type
    try:
        if friend_type != "user":
            if message_type == "job_offer_expire_soon":
                receiver_rows = await users_repo.get_employer_info(friend_id, "whatsapp_country_code,whatsapp_number,name as full_name,user_id, user_id as id")
            else:
                receiver_rows = await users_repo.get_user_info_in_view(friend_id, VIEW_ATTRS)
        else:
            receiver_rows = await users_repo.get_user_staff_info_in_view(friend_id, STAFF_WA_ATTRS)
        template_info = await whatsapp_service.get_template_details_by_name(message_type, language_code)
        if not truthy(template_info):
            return {"success": False, "data": [], "message": req.t("TEMPLATE_NOT_FOUND")}
        params = json.loads(custom_params) if truthy(custom_params) else {}
        components_response = get_template_body(template_info, params)
        if not components_response.get("success"):
            return {"success": False, "data": [], "message": components_response.get("message") or req.t("TEMPLATE_NOT_FOUND")}
        components = components_response["data"]
        sender_rows = await users_repo.get_user_info_in_view(sender_id if truthy(sender_id) else _g(req.user_details, "id"), VIEW_ATTRS)
        sender = _first(sender_rows)
        req.user_details = sender
        group_response = await create_conversation_whatsapp_action_required(req)
        if not group_response.get("success"):
            return {"success": False, "data": [], "message": group_response.get("message") or req.t("SEND_MESSAGE_FAILED")}
        group_info = group_response["data"]
        receiver_info = _first(receiver_rows)
        to_number = _to_number(receiver_info)
        await whatsapp_service.send_template_message_whatsapp(to_number, template_info, components, language)
        template_content: Any = ""
        if template_info.get("components"):
            body_c, header_c = _template_text(template_info)
            template_content = body_c.get("text") if body_c else ""
            if header_c and header_c.get("format") == "TEXT":
                name = _g(params, "name")
                template_content = js_string(header_c.get("text")).replace("{{1}}", js_string(name) if truthy(name) else "", 1) + "\n" + js_string(template_content)
        final_content = components_response.get("content") or template_content
        message_data = {
            "group_id": group_info.get("id"), "user_id": _req_attr(sender, "id"), "sender_id": sender["id"],
            "receiver_id": parse_int(friend_id), "platform": "whatsapp", "content": final_content,
            "message_type": "text", "media_type": "text", "entity_id": template_info.get("name"), "entity_type": "template",
            "media_link": None, "created_by": sender["id"], "created_at": now_aware(), "response_time": 0,
        }
        messages = await aws_message_repo.create_message_with_multi_language(message_data)
        message_id = _g(messages, "insert_id")
        _apply_translations(message_data, messages)
        payload = _group_payload(final_content, sender["id"])
        payload["platform_two"] = "whatsapp"
        await group_repo.update_group_by_id(payload, group_info.get("id"))
        await users_repo.update_details_by_user_id(friend_id, {"last_whatsapp_message_ceated_at": utcnow()})
        message_data["conversation_sid"] = group_info.get("id")
        message_data["profile_image"] = sender.get("profile_image") if truthy(sender.get("profile_image")) else get_settings().default_profile_image
        message_data["read_status"] = "unread"
        message_data["id"] = message_id
        message_data["message_id"] = message_id
        receiver = _receiver_base(message_data, message_id, sender)
        receiver.update({"read_by": "", "replies": "", "status": "sent", "receiver_id": parse_int(group_info.get("unique_name")),
                         "mobile_number": js_string(_g(receiver_info, "whatsapp_country_code")) + js_string(_g(receiver_info, "whatsapp_number"))})
        return {"success": True, "data": receiver, "message": req.t("SEND_MESSAGE_SUCCESS")}
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": developer_message_or_message(error)}


_ = (asyncio, parse_date)
