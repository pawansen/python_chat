"""Message-list / read-state / legacy Twilio services (port of aws.service.js:
enrichMessagesWithUserDetails, messageList, messageListApp, chatHistoryList,
getMessageListGroupedByDate, sendFile, readAllMessage, getAccessToken,
addRemoveGroupParticipant, fetchConversation, getMessageById, allMessage,
updateReadMessageCount, fetchAllParticipant, getParticipantById,
createWhatsappGroup, sendMessageText).

Functions that called ``next(error)`` in Node raise here; the global error
handler turns that into the same response (500 serverError, or the
StatusError mapping).
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from app.core.config import get_settings
from app.core.js import UNDEFINED, js_string, parse_date, sanitize_filename, truthy
from app.core.jsonx import stringify
from app.core.tasks import spawn
from app.redis import cache_keys, cache_service
from app.repositories import aws_message as aws_message_repo
from app.repositories import group as group_repo
from app.repositories import group_participant as gp_repo
from app.repositories import users as users_repo
from app.services import uploader
from app.services.chat.common import resolve_profile_image, utcnow
from app.utils.delivered import deliver_pending_messages

FULL_ATTRS = "user_id,CONCAT(country_code, mobile_number) as mobile_number,guard_name,related_name as full_name,guard_name,profile_image,role_name,email"
SHORT_ATTRS = "user_id,CONCAT(country_code, mobile_number) as mobile_number,guard_name,related_name as full_name,guard_name,profile_image"


def _g(obj: Any, key: str, default: Any = UNDEFINED) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else default


def _or_empty(value: Any) -> Any:
    return value if truthy(value) else ""


async def enrich_messages_with_user_details(messages: list[dict], attributes: str, prefix_s3: bool = True) -> list[dict]:
    if not messages:
        return []
    user_ids = []
    for m in messages:
        uid = m.get("user_id")
        if truthy(uid) and uid not in user_ids:
            user_ids.append(uid)
    user_map = await users_repo.get_user_info_bulk(user_ids, attributes)
    enriched = []
    base = js_string(get_settings().aws_s3_base_url)
    for message in messages:
        message["message_id"] = message.get("id", UNDEFINED)
        message["conversation_sid"] = message.get("group_id", UNDEFINED)
        detail = user_map.get(message.get("user_id"))
        if detail:
            user = dict(detail)
            user["profile_image"] = resolve_profile_image(user.get("profile_image"))
            if prefix_s3 and truthy(user["profile_image"]) and not js_string(user["profile_image"]).startswith("http"):
                user["profile_image"] = base + js_string(user["profile_image"])
            enriched.append({**message, **user})
        else:
            enriched.append(message)
    return enriched


def _sort_chronological(items: list[dict]) -> None:
    items.sort(key=lambda m: js_string(m.get("created_at")))


def _message_limit(body: dict) -> Any:
    limit = _g(body, "limit")
    return get_settings().chat_message_limit if limit is UNDEFINED else limit


async def message_list(req) -> dict[str, Any]:
    try:
        body = req.body
        sid = _g(body, "sid")
        page = _g(body, "page")
        page = 1 if page is UNDEFINED else page
        limit = _message_limit(body)
        start_key, platform = _g(body, "ExclusiveStartKey"), _g(body, "platform")
        group_id = sid
        if not truthy(await gp_repo.check_valid_participant_by_sid(sid, _g(req.user_details, "id"))):
            return {"success": False, "data": [], "message": req.t("SID_INVALID")}
        spawn(deliver_pending_messages(req.io, group_id, _g(req.user_details, "id")))
        options = {
            "startKey": start_key if truthy(start_key) else None,
            "limit": limit,
            "ascending": False,
            "filters": {"is_deleted": 0, "group_id": group_id, "platform": platform if truthy(platform) else "app"},
        }
        pagination = stringify(start_key) if truthy(start_key) else None
        version = await cache_service.get_version(cache_keys.version_message_key(group_id))
        key = cache_keys.message_list_key(group_id, version, cache_keys.cache_key_hash({
            "scope": "message-list",
            "platform": _or_empty(platform),
            "page": _or_empty(page),
            "limit": _or_empty(limit),
            "paginationFilter": _or_empty(pagination),
        }))
        cached = await cache_service.get(key)
        if cached:
            return json.loads(cached)
        data = await aws_message_repo.get_messages_with_filter_sort_pagination(group_id, options)
        if not data.get("success"):
            return {"success": False, "data": [], "message": data.get("message"), "current_page": 0, "limit": 0}
        _sort_chronological(data["Items"])
        items = await enrich_messages_with_user_details(data["Items"], FULL_ATTRS, True)
        result = {"current_page": 0, "limit": limit, "LastEvaluatedKey": data.get("lastEvaluatedKey") or "", "data": items}
        await cache_service.set(key, stringify(result), get_settings().redis.cache_ttl_long)
        return result
    except Exception as error:  # noqa: BLE001
        print(f"messageList error: {error}", flush=True)
        return {"success": False, "data": [], "message": str(error)}


async def message_list_app(req) -> dict[str, Any]:
    try:
        body = req.body
        sid = _g(body, "sid")
        page = _g(body, "page")
        page = 1 if page is UNDEFINED else page
        limit = _message_limit(body)
        start_key, platform = _g(body, "ExclusiveStartKey"), _g(body, "platform")
        action_type, only_action = _g(body, "action_type"), _g(body, "only_action_required")
        group_id = sid
        if not truthy(await gp_repo.check_valid_participant_by_sid(sid, _g(req.user_details, "id"))):
            return {"success": False, "data": [], "message": req.t("SID_INVALID")}
        spawn(deliver_pending_messages(req.io, group_id, _g(req.user_details, "id")))
        options = {
            "startKey": start_key if truthy(start_key) else None,
            "limit": limit,
            "ascending": truthy(action_type),
            "filters": {
                "is_deleted": 0,
                "group_id": group_id,
                "platform": platform if truthy(platform) else "app",
                "entity_type": action_type if truthy(action_type) else None,
                "only_action_required": only_action if truthy(only_action) else None,
            },
        }
        pagination = stringify(start_key) if truthy(start_key) else None
        version = await cache_service.get_version(cache_keys.version_message_key(group_id))
        key = cache_keys.message_list_key(group_id, version, cache_keys.cache_key_hash({
            "scope": "message-list-action",
            "platform": _or_empty(platform),
            "page": _or_empty(page),
            "limit": _or_empty(limit),
            "action_type": _or_empty(action_type),
            "only_action_required": _or_empty(only_action),
            "paginationFilter": _or_empty(pagination),
        }))
        cached = await cache_service.get(key)
        if cached:
            return json.loads(cached)
        data = await aws_message_repo.get_messages_with_filter_sort_pagination(group_id, options)
        if not data.get("success"):
            return {"success": False, "data": {"message": data.get("message"), "current_page": 0, "limit": 0, "LastEvaluatedKey": None, "data": []}}
        _sort_chronological(data["Items"])
        items = await enrich_messages_with_user_details(data["Items"], SHORT_ATTRS, False)
        result = {
            "message": req.t("SUCCESS_LISTED"),
            "data": {"data": items, "LastEvaluatedKey": data.get("lastEvaluatedKey") or None, "current_page": 0, "limit": limit},
        }
        await cache_service.set(key, stringify(result), get_settings().redis.cache_ttl_long)
        return result
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": str(error), "LastEvaluatedKey": None}


async def chat_history_list(req) -> dict[str, Any]:
    try:
        body = req.body
        group_id, user_id, platform = _g(body, "group_id"), _g(body, "user_id"), _g(body, "platform")
        limit = _message_limit(body)
        start_key, start_date, end_date = _g(body, "ExclusiveStartKey"), _g(body, "startDate"), _g(body, "endDate")
        search, page = _g(body, "search"), _g(body, "page")
        options = {
            "startKey": start_key if truthy(start_key) else None,
            "limit": limit,
            "ascending": False,
            "startDate": start_date,
            "endDate": end_date,
            "search": search,
            "page": page,
            "filters": {"is_deleted": 0, "platform": platform},
        }
        pagination = stringify(start_key) if truthy(start_key) else None
        version = await cache_service.get_version(cache_keys.version_message_key(group_id))
        key = cache_keys.message_list_key(group_id, version, cache_keys.cache_key_hash({
            "scope": "chat-history",
            "platform": _or_empty(platform),
            "page": _or_empty(page),
            "limit": _or_empty(limit),
            "user_id": _or_empty(user_id),
            "startDate": _or_empty(start_date),
            "endDate": _or_empty(end_date),
            "search": _or_empty(search),
            "paginationFilter": _or_empty(pagination),
        }))
        cached = await cache_service.get(key)
        if cached:
            return json.loads(cached)
        data = await aws_message_repo.get_messages_history_combine(group_id, user_id, options)
        if not data.get("success"):
            return {"success": False, "data": [], "message": data.get("message"), "current_page": 0, "limit": 0}
        items = await enrich_messages_with_user_details(data["Items"], SHORT_ATTRS, False)
        last_keys = [r["LastEvaluatedKey"] for r in data.get("results") or [] if r.get("LastEvaluatedKey")]
        result = {"current_page": 0, "limit": limit, "LastEvaluatedKey": last_keys, "data": items}
        await cache_service.set(key, stringify(result), get_settings().redis.cache_ttl_long)
        return result
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": str(error)}


def group_messages_by_date(messages: list[dict]) -> list[dict]:
    def day(m: dict) -> str:
        parsed = parse_date(m.get("created_at"))
        if parsed is None:
            raise ValueError("Invalid time value")
        return parsed.strftime("%Y-%m-%d")

    order: list[str] = []
    for m in messages:
        d = day(m)
        if d not in order:
            order.append(d)
    return [{"date": d, "body": [m for m in messages if day(m) == d]} for d in order]


async def get_message_list_grouped_by_date(req) -> dict[str, Any]:
    try:
        body = req.body
        sid = _g(body, "sid")
        page = _g(body, "page")
        page = 1 if page is UNDEFINED else page
        limit = _message_limit(body)
        start_key = _g(body, "ExclusiveStartKey")
        group_id = sid
        if not truthy(await gp_repo.check_valid_participant_by_sid(sid, _g(req.user_details, "id"))):
            return {"success": False, "data": [], "message": req.t("SID_INVALID")}
        spawn(deliver_pending_messages(req.io, group_id, _g(req.user_details, "id")))
        options = {"startKey": start_key if truthy(start_key) else None, "limit": limit, "ascending": True,
                   "filters": {"is_deleted": 0, "group_id": group_id}}
        pagination = stringify(start_key) if truthy(start_key) else None
        version = await cache_service.get_version(cache_keys.version_message_key(group_id))
        key = cache_keys.message_list_key(group_id, version, cache_keys.cache_key_hash({
            "scope": "message-list-grouped",
            "page": _or_empty(page),
            "limit": _or_empty(limit),
            "paginationFilter": _or_empty(pagination),
        }))
        cached = await cache_service.get(key)
        if cached:
            return json.loads(cached)
        data = await aws_message_repo.get_messages_with_filter_sort_pagination(group_id, options)
        if not data.get("success"):
            return {"success": False, "data": [], "message": data.get("message"), "current_page": 0, "limit": 0}
        items = await enrich_messages_with_user_details(data["Items"], SHORT_ATTRS, False)
        result = {"current_page": 0, "limit": limit, "LastEvaluatedKey": data.get("lastEvaluatedKey") or "", "data": group_messages_by_date(items)}
        await cache_service.set(key, stringify(result), get_settings().redis.cache_ttl_long)
        return result
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": str(error)}


async def send_file(req) -> dict[str, Any]:
    file = req.file
    if file is None:
        raise TypeError("Cannot read properties of undefined (reading 'originalname')")
    ext = file.originalname.split(".")[-1]
    name = sanitize_filename(file.originalname.split(".")[0])
    result = await uploader.upload(file, "profile/cropped", ext, name)
    out = file.to_dict()
    out.pop("buffer", None)
    out["mediaUrl"] = result
    return {"data": out}


async def read_all_message(req) -> dict[str, Any]:
    try:
        sid = _g(req.body, "sid")
        user = req.user_details
        if not truthy(await group_repo.check_valid_sid(sid)):
            return {"success": False, "data": [], "message": req.t("SID_INVALID")}
        if not truthy(await gp_repo.check_valid_participant_by_sid(sid, user["id"])):
            return {"success": False, "data": [], "message": req.t("SID_INVALID")}
        from app.core.js import number

        result = await aws_message_repo.update_message_read_status(
            {"group_id": number(sid), "user_id": number(user["id"])}, {"read_status": "read", "updated_by": user["id"]}
        )
        if truthy(result):
            return {"success": True, "data": [], "message": req.t("READ_SUCCESS")}
        return {"success": False, "data": [], "message": req.t("READ_FAILED")}
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": str(error)}


# ── Twilio-era endpoints (still mounted) ───────────────────────────────────
_twilio = None


def _twilio_client():
    global _twilio
    if _twilio is None:
        from twilio.rest import Client

        cfg = get_settings().twilio
        _twilio = Client(cfg["account_sid"], cfg["auth_token"])
    return _twilio


async def get_access_token(req) -> dict[str, Any]:
    from twilio.jwt.access_token import AccessToken
    from twilio.jwt.access_token.grants import ChatGrant

    user = req.user_details
    identity = (js_string(user.get("country_code")) + js_string(user.get("mobile_number"))) if truthy(user.get("mobile_number")) and truthy(user.get("country_code")) else user.get("email", UNDEFINED)
    if not truthy(identity):
        return {"success": False, "data": [], "message": req.t("USER_IDENTITY_REQUIRED")}
    cfg = get_settings().twilio
    if not cfg["account_sid"] or not cfg["api_key"] or not cfg["api_secret"]:
        raise ValueError("accountSid, keySid and secret are required")  # twilio-node throws -> next(error)
    token = AccessToken(cfg["account_sid"], cfg["api_key"], cfg["api_secret"], identity=identity, ttl=int(get_settings().jwt.expiry))
    token.add_grant(ChatGrant(service_sid=cfg["chat_service_sid"]))
    return {"success": True, "data": {"access-token": token.to_jwt()}, "message": req.t("CHAT_TOKEN_SUCCESS")}


async def add_remove_group_participant(req) -> dict[str, Any]:
    try:
        caller = req.user_details
        body = req.body
        group_id, user_id, action = _g(body, "groupId"), _g(body, "userId"), _g(body, "action")
        sid, pid = _g(body, "sid"), _g(body, "pid")
        user = await users_repo.get_details_by_user_id(user_id, ["id", "role_id", "mobile_number", "country_code", "email"])
        if not truthy(user):
            return {"success": False, "data": [], "message": req.t("USER_NOT_FOUND")}
        mobile = _g(user, "mobile_number")
        identity = js_string(_g(user, "country_code")) + js_string(mobile) if truthy(mobile) and mobile != "" else _g(user, "email")
        if not truthy(identity):
            return {"success": False, "data": [], "message": req.t("USER_IDENTITY_REQUIRED")}
        group = await group_repo.get_details_by_group_id(group_id)
        if not truthy(group):
            return {"success": False, "data": [], "message": req.t("GROUP_NOT_FOUND")}
        caller_row = await gp_repo.check_user_added_in_group(group_id, _g(caller, "id"))
        if not truthy(caller_row) or truthy(_g(caller_row, "error")):
            return {"success": False, "data": [], "message": req.t("INVALID_ACCESS")}
        count = await gp_repo.group_participants_count(group_id)
        if action == "ADD" and isinstance(count, int | float) and count >= get_settings().chat_group_participants_limit:
            return {"success": False, "data": [], "message": req.t("GROUP_PARTICIPANT_LIMIT")}
        added = await gp_repo.check_user_added_in_group(group_id, user_id)
        if action == "ADD" and truthy(added):
            return {"success": False, "data": [], "message": req.t("USER_ALREADY_IN_GROUP")}
        if action == "REMOVE" and not truthy(added):
            return {"success": False, "data": [], "message": req.t("USER_NOT_FOUND_IN_GROUP")}
        if action == "ADD":
            participant = await asyncio.to_thread(
                lambda: _twilio_client().conversations.v1.conversations(sid).participants.create(identity=identity)
            )
            if not participant:
                return {"success": False, "data": [], "message": req.t("GROUP_PARTICIPANT_ADD_FAILED")}
            now = utcnow()
            data = {
                "participant_sid": participant.sid,
                "account_sid": participant.account_sid,
                "chat_service_sid": _g(group, "chat_service_sid"),
                "conversation_sid": participant.conversation_sid,
                "identity": participant.identity,
                "messagingBinding": stringify(participant.messaging_binding),
                "role_sid": participant.role_sid,
                "lastReadMessageIndex": participant.last_read_message_index,
                "lastReadTimestamp": participant.last_read_timestamp,
                "group_id": group_id,
                "user_id": user_id,
                "is_host": 0,
                "role_id": _g(user, "role_id"),
                "joined_on": now,
                "created_by": caller["id"],
                "created_at": now,
            }
            await gp_repo.create_group_participant(data)
            return {"success": True, "data": data, "message": req.t("GROUP_PARTICIPANT_ADDED")}
        if not truthy(await gp_repo.check_pid(group_id, user_id, pid)):
            return {"success": False, "data": [], "message": req.t("PID_INVALID")}
        removed = await asyncio.to_thread(lambda: _twilio_client().conversations.v1.conversations(sid).participants(pid).delete())
        if removed:
            await gp_repo.update_group_participant_by_id(
                {"is_deleted": 1, "deleted_by": caller["id"], "deleted_at": utcnow()}, _g(added, "id")
            )
            return {"success": True, "data": [], "message": req.t("GROUP_PARTICIPANT_REMOVED")}
        return {"success": False, "data": [], "message": req.t("GROUP_PARTICIPANT_REMOVE_FAILED")}
    except Exception as error:  # noqa: BLE001
        print(f"error {error}", flush=True)
        return {"success": False, "data": [], "message": req.t("GENERAL_ERROR")}


async def fetch_conversation(req) -> dict[str, Any]:
    if not truthy(await group_repo.check_valid_sid(_g(req.body, "sid"))):
        return {"success": False, "data": [], "message": req.t("SID_INVALID")}
    return {"success": True, "data": [], "message": req.t("SUCCESS_LISTED")}


async def get_message_by_id(req) -> dict[str, Any]:
    return {"success": True, "data": [], "message": req.t("SUCCESS_LISTED")}


async def all_message(req) -> dict[str, Any]:
    sid = _g(req.body, "sid")
    if not truthy(await group_repo.check_valid_sid("" if sid is UNDEFINED else sid)):
        return {"success": False, "data": [], "message": req.t("SID_INVALID")}
    return {"success": True, "data": [], "message": req.t("SUCCESS_LISTED")}


async def update_read_message_count(req) -> dict[str, Any]:
    result = await gp_repo.reset_unread_count(_g(req.body, "sid"), req.user_details["id"])
    if truthy(result) and not (isinstance(result, dict) and "error" in result):
        await cache_service.bump_version(cache_keys.version_conversation_key(req.user_details["id"]))
        return {"success": True, "data": [], "message": req.t("RECORD_UPDATED")}
    return {"success": False, "data": [], "message": req.t("GENERAL_ERROR")}


async def fetch_all_participant(req) -> dict[str, Any]:
    if not truthy(await group_repo.check_valid_sid(_g(req.body, "sid"))):
        return {"success": False, "data": [], "message": req.t("SID_INVALID")}
    return {"success": True, "data": [], "message": req.t("SUCCESS_LISTED")}


async def get_participant_by_id(req) -> dict[str, Any]:
    sid, psid = _g(req.body, "sid"), _g(req.body, "psid")
    if not truthy(await group_repo.check_valid_sid(sid)):
        return {"success": False, "data": [], "message": req.t("SID_INVALID")}
    if not truthy(await gp_repo.check_valid_psid(sid, psid)):
        return {"success": False, "data": [], "message": req.t("PSID_INVALID")}
    return {"success": True, "data": [], "message": req.t("SUCCESS_LISTED")}


_E164 = re.compile(r"^\+[1-9]\d{1,14}$")


async def create_whatsapp_group(req) -> Any:
    user = req.user_details
    body = req.body
    friendly_name, type_ = _g(body, "friendly_name"), _g(body, "type")
    platform = _g(body, "platform")
    platform = 2 if platform is UNDEFINED else platform
    image = _g(body, "image")
    image = "" if image is UNDEFINED else image
    job_id = _g(body, "job_id")
    job_id = 0 if job_id is UNDEFINED else job_id
    identity = js_string(user.get("country_code")) + js_string(user.get("mobile_number")) if truthy(user.get("mobile_number")) and truthy(user.get("country_code")) else ""
    if not identity:
        return {"success": False, "data": [], "message": req.t("IDENTITY_REQUIRED")}
    if not _E164.match(identity):
        return {"success": False, "data": [], "message": req.t("MOBILE_INVALID")}
    import random
    import string

    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=11))
    unique_key = identity if type_ == "ONE_TO_ONE" else rand
    conversation = await asyncio.to_thread(lambda: _twilio_client().conversations.v1.conversations.create(friendly_name=friendly_name))
    if not conversation or not conversation.sid:
        return None
    response: dict[str, Any] = {"sid": conversation.sid, "friendly_name": conversation.friendly_name, "accountSid": conversation.account_sid}
    group = await group_repo.create_group({
        "account_sid": conversation.account_sid,
        "chat_service_sid": conversation.chat_service_sid,
        "messaging_service_sid": conversation.messaging_service_sid,
        "conversation_sid": conversation.sid,
        "conversation_state": conversation.state,
        "conversation_bindings": conversation.bindings,
        "friendly_name": conversation.friendly_name,
        "unique_name": unique_key,
        "image": image,
        "job_id": job_id,
        "type": type_,
        "platform": platform,
        "created_by": user["id"],
        "created_at": utcnow(),
    })
    if truthy(_g(group, "error")):
        return None
    response["groupId"] = group["id"]
    whatsapp_from = get_settings().twilio["whatsapp_from"]
    participant = await asyncio.to_thread(
        lambda: _twilio_client().conversations.v1.conversations(conversation.sid).participants.create(
            identity=identity,
            messaging_binding_address=f"whatsapp:{identity}",
            messaging_binding_proxy_address=f"whatsapp:{whatsapp_from}",
        )
    )
    now = utcnow()
    await gp_repo.create_group_participant({
        "identity": participant.identity, "group_id": group["id"], "user_id": user["id"], "is_host": 1,
        "role_id": user.get("role_id"), "role_sid": participant.role_sid, "account_sid": participant.account_sid,
        "joined_on": now, "created_by": user["id"], "created_at": now,
    })
    return {"success": True, "data": response, "message": req.t("CHAT_TOKEN_SUCCESS")}


def _camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


async def send_message_text(req) -> dict[str, Any]:
    message, phone = _g(req.body, "message"), _g(req.body, "phone_number")
    whatsapp_from = get_settings().twilio["whatsapp_from"]
    sent = await asyncio.to_thread(lambda: _twilio_client().messages.create(from_=whatsapp_from, body=message, to=phone))
    data = {_camel(k): v for k, v in getattr(sent, "_properties", {}).items()}
    return {"success": True, "data": data, "message": req.t("SEND_MESSAGE_SUCCESS")}
