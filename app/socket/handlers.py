"""Socket.IO chat core (port of src/services/awsSocket.service.js).

Events (unchanged names / payloads / emits):
``join_branch_room``, ``join_room_user``, ``branch_checkin``, ``join_room``,
``send_message``, ``chat_message_read``, ``delete_message``, ``disconnect``.

Auth: the handshake (``connect``) verifies the token (HI-09); each event
resolves ``payload.token`` first, falling back to the handshake token, and
re-verifies it (signature + revocation + account status) at most every
SOCKET_AUTH_CACHE_TTL_MS (SOC-02).

Rooms: conversation id, personal user id and ``branch_<id>``, always as
strings (v2-compat semantics; ``parseInt`` failures address room "NaN").

Differences from Node (documented in MIGRATION_REPORT.md): a handler error
never crashes the process (Node had no unhandledRejection handler, so e.g. a
failed ``branch_checkin`` auth threw a ReferenceError inside a ``.then`` and
could take a worker down) — the error is logged and the emit that would have
thrown is skipped.
"""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings
from app.core.http import developer_message_or_message
from app.core.i18n import t_default
from app.core.js import (
    UNDEFINED,
    js_string,
    loose_eq,
    nullish,
    number,
    or_,
    parse_date,
    parse_int,
    truthy,
)
from app.core.logging import logger
from app.core.security import is_token_revoked, verify_token
from app.core.tasks import spawn
from app.redis import cache_keys, cache_service
from app.repositories import aws_message as aws_message_repo
from app.repositories import group as group_repo
from app.repositories import group_participant as gp_repo
from app.repositories import users as users_repo
from app.services import line as line_service
from app.services import push
from app.services import whatsapp as whatsapp_service
from app.socket import content_filter, rate_limiter
from app.socket.emitter import io
from app.utils.delivered import deliver_pending_messages

AUTH_USER_ATTRS = (
    "CONCAT(country_code, mobile_number) as mobile_number,guard_name,related_name as full_name,"
    "guard_name,profile_image,user_id, user_id as id"
)


def p(data: Any, key: str) -> Any:
    """``data.key`` with JS semantics: TypeError on null/undefined, undefined for missing."""
    if data is None or data is UNDEFINED:
        raise TypeError(f"Cannot read properties of {js_string(data)} (reading '{key}')")
    if isinstance(data, dict):
        return data.get(key, UNDEFINED)
    return UNDEFINED


def g(obj: Any, key: str) -> Any:
    """``obj?.key``."""
    return obj.get(key, UNDEFINED) if isinstance(obj, dict) else UNDEFINED


def _pi(value: Any) -> Any:
    """``parseInt`` result usable as a room key / payload value (NaN stays NaN)."""
    return parse_int(value)


def resolve_profile_image(profile_image: Any) -> Any:
    settings = get_settings()
    if not truthy(profile_image):
        return settings.default_profile_image
    if isinstance(profile_image, str) and profile_image.startswith("http"):
        return profile_image
    return js_string(settings.aws_s3_base_url) + js_string(profile_image)


def response_time_seconds(last_message: Any) -> float:
    """``(currentDate - new Date(dayjs(last.created_at).format('YYYY-MM-DD HH:mm:ss'))) / 1000``.

    ``getLastMessage`` returns ``[]`` (truthy in JS) when there is no message,
    making ``dayjs(undefined)`` = now; the format truncates to whole seconds.
    """
    now_ms = time.time() * 1000
    if not truthy(last_message):
        return 0
    created = g(last_message, "created_at")
    prev = datetime.now(UTC) if created is UNDEFINED else parse_date(created)
    if prev is None:
        return math.nan
    prev_ms = math.floor(prev.timestamp()) * 1000
    return (math.floor(now_ms) - prev_ms) / 1000


def build_message_data(data: Any, is_user: dict, receiver_id: Any, platform: Any, extra: dict | None = None) -> dict:
    media_link = g(data, "media_link")
    media_type = g(data, "media_type")
    message = {
        "group_id": g(data, "conversation_sid"),
        "user_id": is_user.get("id"),
        "sender_id": is_user.get("id"),
        "receiver_id": _pi(receiver_id),
        "platform": platform or or_(g(data, "platform"), "app"),
        "content": or_(g(data, "body"), None),
        "message_type": or_(g(data, "message_type"), "text"),
        "media_type": or_(media_type, "text"),
        "entity_id": or_(g(data, "entity_id"), None),
        "entity_type": or_(g(data, "entity_type"), None),
        "media_link": media_link if truthy(media_link) and media_type != "text" else None,
        "created_by": is_user.get("id"),
        "created_at": datetime.now(UTC),
        "response_time": 0,
    }
    message.update(extra or {})
    return message


def build_receiver_data(message_data: dict, message_id: Any, is_user: dict, receiver_id: Any, mobile: Any, group_info: Any, messages: Any) -> dict:
    receiver = dict(message_data)
    receiver["id"] = message_id
    receiver["message_id"] = message_id
    receiver["attachments"] = None
    receiver["guard_name"] = is_user.get("guard_name", UNDEFINED)
    receiver["full_name"] = is_user.get("full_name", UNDEFINED)
    receiver["read_by"] = ""
    receiver["replies"] = ""
    receiver["status"] = 1
    receiver["receiver_id"] = _pi(receiver_id)
    receiver["mobile_number"] = mobile
    receiver["groupInfo"] = group_info
    for key in ("content_en", "content_id", "content_zh_cn", "content_zh_tw", "source_language"):
        receiver[key] = g(messages, key)
    return receiver


def build_group_payload(data: Any, is_user: dict, extra: dict | None = None) -> dict:
    now = datetime.now(UTC).replace(tzinfo=None)
    payload = {
        "last_message": g(data, "body"),
        "last_msgtype": g(data, "message_type"),
        "last_message_ceated_at": now,
        "message_send_by": is_user.get("id"),
        "updated_at": now,
        "updated_by": is_user.get("id"),
    }
    payload.update(extra or {})
    return payload


def _apply_message_fields(message_data: dict, messages: Any) -> None:
    for key in ("content_en", "content_id", "content_zh_cn", "content_zh_tw", "source_language"):
        message_data[key] = g(messages, key)


async def is_recipient_socket_connected(group_id: Any, user_id: Any) -> bool:
    personal = await io.fetch_room_users(js_string(user_id))
    if personal:
        return True
    room_sockets = await io.fetch_room_users(js_string(group_id))
    for entry in room_sockets:
        user = entry.get("user")
        if user:
            ident = user.get("id") if not nullish(user.get("id")) else user.get("user_id")
            if js_string("" if nullish(ident) else ident) == js_string(user_id):
                return True
    return False


async def mark_delivered_if_recipient_connected(group_id: Any, receiver_ids: list[Any]) -> None:
    try:
        for receiver_id in receiver_ids:
            if nullish(receiver_id):
                continue
            if await is_recipient_socket_connected(group_id, receiver_id):
                await deliver_pending_messages(io, group_id, receiver_id)
                return
    except Exception as error:  # noqa: BLE001
        logger.debug("markDeliveredIfRecipientConnected error", {"groupId": group_id, "error": str(error)})


class ChatSocketService:
    def __init__(self, sio) -> None:
        self.sio = sio

    # ── auth ─────────────────────────────────────────────────────────────
    async def _session(self, sid: str) -> dict:
        try:
            return await self.sio.get_session(sid)
        except KeyError:
            return {}

    async def resolve_token(self, sid: str, payload: Any) -> Any:
        token = g(payload, "token")
        if truthy(token):
            return token
        session = await self._session(sid)
        return session.get("token") or ""

    async def authorization_token(self, sid: str, token: Any) -> Any:
        if not truthy(token):
            return False
        session = await self._session(sid)
        now = time.time() * 1000
        cache = session.get("auth_cache")
        if cache and cache["token"] == token and cache["expires"] > now:
            return cache["user"]
        decoded = verify_token(token, get_settings().jwt.secret)
        if not decoded:
            return False
        if await is_token_revoked(decoded):
            return False
        account = await users_repo.get_details_by_user_id(decoded.get("sub"), ["id", "status"])
        if not truthy(account) or not loose_eq(g(account, "status"), 1):
            return False
        rows = await users_repo.get_user_info_in_view(decoded.get("sub"), AUTH_USER_ATTRS)
        if isinstance(rows, list) and rows:
            user = dict(rows[0])
            session["auth_cache"] = {"token": token, "user": user, "expires": now + get_settings().socket.auth_cache_ttl_ms}
            try:
                await self.sio.save_session(sid, session)
            except KeyError:
                pass
            return user
        return False

    # ── events ───────────────────────────────────────────────────────────
    async def join_branch_room(self, sid: str, data: Any) -> None:
        logger.debug("join_branch_room", {"branchId": g(data, "branch_id")})
        if not truthy(p(data, "branch_id")):
            return
        is_user = await self.authorization_token(sid, await self.resolve_token(sid, data))
        if truthy(is_user):
            branch_id = "branch_" + js_string(data["branch_id"])
            await self.sio.enter_room(sid, branch_id)
            await io.emit_to(branch_id, "join_branch_room", {"status": True, "message": t_default("JOINED_BRANCH_ROOM"), "data": {}})
        else:
            await self.sio.emit("join_branch_room", {"status": False, "message": t_default("TOKEN_DECODING_ERROR"), "data": {}}, to=sid)

    async def join_room_user(self, sid: str, data: Any) -> None:
        if not truthy(p(data, "userId")):
            return
        token = await self.resolve_token(sid, data)
        if not truthy(token):
            return
        is_user = await self.authorization_token(sid, token)
        user_room = js_string(data["userId"])
        if truthy(is_user):
            # Only your OWN personal room (receive_message_all / check_message_event carry PII).
            if js_string(is_user.get("id")) != js_string(data["userId"]):
                await self.sio.emit("join_room_user", {"status": False, "message": t_default("INVALID_ACCESS"), "data": {}}, to=sid)
                return
            await self.sio.enter_room(sid, user_room)
            await io.emit_to(user_room, "join_room_user", {"status": True, "message": t_default("JOINED_USER_ROOM"), "data": {}})
        else:
            await io.emit_to(user_room, "join_room_user", {"status": False, "message": t_default("TOKEN_DECODING_ERROR"), "data": {}})

    async def branch_checkin(self, sid: str, data: Any) -> None:
        is_user = await self.authorization_token(sid, await self.resolve_token(sid, data))
        if truthy(is_user):
            branch_id = "branch_" + js_string(p(data, "branch_id"))
            await self.sio.enter_room(sid, branch_id)
            payload = {"status": True, "message": t_default("BRANCH_ROOM_CHECKIN"), "data": data}
            await io.emit_to(branch_id, "branch_checkin", payload)
            await io.emit_to(branch_id, "check_in_all", payload)
        else:
            # Node: `io.to(branch_id)` with branch_id out of scope -> ReferenceError in a
            # .then (unhandled rejection). Nothing was ever emitted; we log instead of crashing.
            logger.error("branch_checkin auth failed (no emit; Node threw ReferenceError here)")

    async def join_room(self, sid: str, data: Any) -> None:
        if not truthy(p(data, "conversation_sid")):
            return
        conversation_sid = _pi(data["conversation_sid"])
        token = await self.resolve_token(sid, data)
        if not truthy(token):
            await io.emit_to(conversation_sid, "join_room", {"status": False, "message": t_default("TOKEN_DECODING_ERROR"), "data": {}})
            return
        session = await self._session(sid)
        session["presence"] = {"conversation_sid": data["conversation_sid"]}
        try:
            await self.sio.save_session(sid, session)
        except KeyError:
            pass
        is_user = await self.authorization_token(sid, token)
        response = await group_repo.get_group_details({"id": data["conversation_sid"]}, ["id", "friendly_name", "platform"])
        if not truthy(is_user):
            await io.emit_to(conversation_sid, "join_room", {"status": False, "message": t_default("TOKEN_DECODING_ERROR"), "data": {}})
            return
        if not truthy(response):
            await io.emit_to(conversation_sid, "join_room", {"status": False, "message": t_default("INVALID_CHAT_GROUP"), "data": {}})
            return
        participant = await gp_repo.get_group_participant_details(
            {"group_id": data["conversation_sid"], "user_id": is_user.get("id"), "is_deleted": 0}, ["id"]
        )
        if truthy(participant) and not (isinstance(participant, dict) and truthy(participant.get("error"))):
            await self.sio.enter_room(sid, js_string(conversation_sid))
            await io.emit_to(data["conversation_sid"], "join_room", {"status": True, "message": "Joined", "data": response})
        else:
            await self.sio.leave_room(sid, js_string(conversation_sid))
            await self.sio.emit("join_room", {"status": False, "message": t_default("INVALID_ACCESS"), "data": {}}, to=sid)

    async def send_message(self, sid: str, data: Any) -> None:
        logger.debug("send_message", {"conversationSid": g(data, "conversation_sid")})
        if not truthy(p(data, "conversation_sid")):
            return
        conversation_sid = _pi(data["conversation_sid"])
        token = await self.resolve_token(sid, data)
        if not truthy(token):
            await io.emit_to(conversation_sid, "receive_message", {"status": False, "message": t_default("TOKEN_DECODING_ERROR"), "data": {}})
            return
        import asyncio

        is_user, group_info, participants = await asyncio.gather(
            self.authorization_token(sid, token),
            group_repo.get_group_details(
                {"id": data["conversation_sid"]},
                ["id", "friendly_name", "platform", "unique_name", "platform_two", "last_message_ceated_at", "last_whatsapp_msg_ceated_at", "type"],
            ),
            gp_repo.get_group_participants_list({"group_id": data["conversation_sid"]}, ["id", "user_id", "role_sid"]),
        )
        if not truthy(is_user):
            await io.emit_to(conversation_sid, "receive_message", {"status": False, "message": t_default("TOKEN_DECODING_ERROR"), "data": {}})
            return
        if not truthy(group_info):
            await io.emit_to(conversation_sid, "receive_message", {"status": False, "message": t_default("INVALID_CHAT_GROUP"), "data": {}})
            return
        is_sender_participant = isinstance(participants, list) and any(
            js_string(p_.get("user_id")) == js_string(is_user.get("id")) for p_ in participants
        )
        if not is_sender_participant:
            await self.sio.emit("receive_message", {"status": False, "message": t_default("INVALID_ACCESS"), "data": {}}, to=sid)
            return
        is_employer_worker = False
        if isinstance(participants, list) and len(participants) > 1:
            roles = {p_.get("role_sid") for p_ in participants}
            is_employer_worker = "employer" in roles and "worker" in roles
        if is_employer_worker:
            raw_msg = js_string(or_(g(data, "body"), ""))
            norm_msg = content_filter.normalize_tight(raw_msg)
            if g(data, "action") == "app" and g(group_info, "type") == "GROUP" and content_filter.has_blocked_term(raw_msg, norm_msg):
                locale = or_(g(data, "locale"), or_(is_user.get("locale", UNDEFINED), "en"))
                await io.emit_to(conversation_sid, "receive_message", {"status": False, "message": content_filter.get_blocked_message(locale), "data": {}})
                return
        await self.route_message(data, is_user, group_info, participants)

    async def route_message(self, data: Any, is_user: dict, group_info: dict, participants: Any) -> None:
        platform = g(data, "platform")
        if group_info.get("platform") != "whatsapp" and platform != "whatsapp":
            if group_info.get("platform") == "line" and platform == "line":
                await self.sent_message_to_user_line(data, is_user, group_info)
            else:
                await self.sent_message_to_user_app(data, is_user, group_info, participants)
        elif group_info.get("platform_two") == "action_required" and platform == "action_required":
            await self.sent_message_to_user_action_required(data, is_user, group_info)
        else:
            await self.sent_message_to_user_whatsapp(data, is_user, group_info)

    async def chat_message_read(self, sid: str, data: Any) -> None:
        token = await self.resolve_token(sid, data)
        conversation = p(data, "conversation_sid")
        if not truthy(token):
            await io.emit_to(conversation, "chat_message_read", {"status": False, "message": t_default("TOKEN_DECODING_ERROR"), "data": {}})
            return
        is_user = await self.authorization_token(sid, token)
        if not truthy(is_user):
            await io.emit_to(conversation, "chat_message_read", {"status": False, "message": t_default("TOKEN_DECODING_ERROR"), "data": {}})
            return
        participant = await gp_repo.check_valid_participant_by_sid(conversation, is_user.get("id"))
        if not truthy(participant):
            await self.sio.emit("chat_message_read", {"status": False, "message": t_default("INVALID_ACCESS"), "data": {}}, to=sid)
            return
        spawn(aws_message_repo.read_message_to_user({"read_status": "read", "updated_by": is_user.get("id")}, conversation, is_user.get("id")))
        spawn(gp_repo.reset_unread_count(conversation, is_user.get("id")))
        cache_service.bump_version_bg(cache_keys.version_conversation_key(is_user.get("id")))
        cache_service.bump_version_bg(cache_keys.version_message_key(number(conversation)))
        await io.emit_to(conversation, "chat_message_read", {"status": True, "message": "All read.", "data": {}})

    async def delete_message(self, sid: str, data: Any) -> None:
        conversation = p(data, "conversation_sid")
        try:
            token = await self.resolve_token(sid, data)
            if not truthy(token):
                await io.emit_to(conversation, "delete_message", {"status": False, "message": t_default("TOKEN_DECODING_ERROR"), "data": {}})
                return
            is_user = await self.authorization_token(sid, token)
            if not truthy(is_user):
                await io.emit_to(conversation, "delete_message", {"status": False, "message": t_default("TOKEN_DECODING_ERROR"), "data": {}})
                return
            if not truthy(g(data, "message_id")):
                await io.emit_to(conversation, "delete_message", {"status": False, "message": t_default("INVALID_MESSAGE_ID"), "data": data})
                return
            result = await aws_message_repo.delete_message_to_user(
                {"is_deleted": 1, "deleted_by": is_user.get("id")},
                {"user_id": is_user.get("id"), "group_id": conversation, "id": data["message_id"]},
            )
            if not truthy(result):  # always an object in Node -> always truthy
                await io.emit_to(conversation, "delete_message", {"status": False, "message": t_default("INVALID_MESSAGE_ID"), "data": data})
                return
            last = await aws_message_repo.get_last_message(conversation)
            if truthy(last):
                spawn(
                    group_repo.update_group_by_id(
                        {
                            "last_message": g(last, "content"),
                            "last_msgtype": g(last, "message_type"),
                            "last_message_ceated_at": _db_datetime(g(last, "created_at")),
                        },
                        conversation,
                    )
                )

            async def bump_participants() -> None:
                participants = await gp_repo.get_group_participants_list({"group_id": conversation}, ["id", "user_id", "role_sid"])
                if isinstance(participants, list):
                    for participant in participants:
                        if not nullish(participant.get("user_id")):
                            cache_service.bump_version_bg(cache_keys.version_conversation_key(participant["user_id"]))

            spawn(bump_participants())
            payload = {
                "status": True,
                "message": t_default("SUCCESSFULLY_DELETED"),
                "data": {"conversation_sid": conversation, "message_id": data["message_id"]},
            }
            await io.emit_to(conversation, "delete_message", payload)
        except Exception as error:  # noqa: BLE001
            await io.emit_to(conversation, "delete_message", {"status": False, "message": str(error), "data": {}})

    async def disconnect(self, sid: str) -> None:
        session = await self._session(sid)
        presence = session.get("presence")
        if presence:
            await io.emit_to(
                presence["conversation_sid"],
                "message",
                {"user": "System", "text": f"{js_string(presence['conversation_sid'])} has left the room."},
            )
        rate_limiter.discard(sid)
        logger.debug("socket disconnected", {"socketId": sid})

    # ── platform senders ────────────────────────────────────────────────
    async def sent_message_to_user_app(self, data: Any, is_user: dict, group_info: dict, prefetched: Any = None) -> None:
        conversation_sid = _pi(g(data, "conversation_sid"))
        try:
            participants = prefetched if truthy(prefetched) else await gp_repo.get_group_participants_list(
                {"group_id": g(data, "conversation_sid")}, ["id", "user_id", "role_sid"]
            )
            if not truthy(participants):
                await io.emit_to(conversation_sid, "receive_message", {"status": False, "message": t_default("INVALID_ACCESS"), "data": {}})
                return
            sender_rows = [r for r in participants if r.get("user_id") == is_user.get("id")]
            receivers = [r for r in participants if r.get("user_id") != is_user.get("id")]
            if not receivers:
                await io.emit_to(conversation_sid, "receive_message", {"status": False, "message": t_default("INVALID_ACCESS"), "data": {}})
                return
            receiver_user_id = _pi(receivers[0].get("user_id"))
            import asyncio

            last, receiver_user = await asyncio.gather(
                aws_message_repo.get_last_message(g(data, "conversation_sid")),
                users_repo.get_user_devices_token(receiver_user_id),
            )
            diff = response_time_seconds(last)
            role_type = "member"
            if group_info.get("type") == "GROUP" and sender_rows:
                role_type = (
                    f"{js_string(sender_rows[0].get('role_sid'))}/{js_string(sender_rows[1].get('role_sid'))}"
                    if len(sender_rows) > 1
                    else sender_rows[0].get("role_sid")
                )
            message_data = build_message_data(
                data, is_user, receiver_user_id, or_(g(data, "platform"), "app"),
                {"response_time": diff, "role_type": role_type, "type": group_info.get("type", UNDEFINED)},
            )
            messages, _ = await asyncio.gather(
                aws_message_repo.create_message_with_multi_language(message_data),
                group_repo.update_group_by_id(build_group_payload(data, is_user), g(data, "conversation_sid")),
            )
            message_id = g(messages, "insert_id")
            message_data["conversation_sid"] = g(data, "conversation_sid")
            message_data["profile_image"] = resolve_profile_image(is_user.get("profile_image"))
            message_data["read_status"] = "unread"
            message_data["id"] = message_id
            message_data["message_id"] = message_id
            _apply_message_fields(message_data, messages)
            mobile = js_string(or_(receivers[0].get("usersModel.country_code"), "")) + js_string(or_(receivers[0].get("usersModel.mobile_number"), ""))
            receiver_data = build_receiver_data(message_data, message_id, is_user, receiver_user_id, mobile, group_info, messages)
            if isinstance(receiver_user, list) and receiver_user:
                receiver_data["employer_worker_staff_common_id"] = receiver_user[0].get("employer_worker_staff_common_id")
            await io.emit_to(conversation_sid, "send_message", {"status": True, "message": "Success", "data": message_data})
            await io.emit_to(conversation_sid, "receive_message", {"status": True, "message": "Success", "data": receiver_data})
            await io.emit_to(receiver_user_id, "check_message_event", {"status": True, "message": "Success", "data": receiver_data})
            await io.emit_to([receiver_user_id, is_user.get("id")], "receive_message_all", {"status": True, "message": "Success", "data": receiver_data})
            spawn(mark_delivered_if_recipient_connected(g(data, "conversation_sid"), [r.get("user_id") for r in receivers]))
            if isinstance(receiver_user, list) and receiver_user and receiver_user[0].get("role_id") in (3, 4):
                devices = await users_repo.get_user_devices_info_notification(receiver_user_id)
                tokens = [d.get("device_token") for d in devices if d.get("device_token")] if isinstance(devices, list) else []
                if tokens:
                    title = "New chat message from " + js_string(is_user.get("full_name", UNDEFINED))
                    spawn(push.send_fcm_notification(tokens, title, g(data, "body"), {
                        "event_id": js_string(g(data, "conversation_sid")),
                        "user_id": js_string(receiver_user[0].get("id")),
                        "event_type": "chat_app",
                    }))
        except Exception as error:  # noqa: BLE001
            await io.emit_to(conversation_sid, "receive_message", {"status": False, "message": str(error) or "An error occurred"})

    async def sent_message_to_user_line(self, data: Any, is_user: dict, group_info: dict) -> None:
        conversation_sid = _pi(g(data, "conversation_sid"))
        try:
            rows = await users_repo.get_user_info_in_view(
                group_info.get("unique_name"),
                "CONCAT(country_code, mobile_number) as mobile_number,whatsapp_country_code,whatsapp_number,guard_name,related_name as full_name,guard_name,profile_image,user_id, user_id as id, line_user_id",
            )
            if not truthy(rows) or (isinstance(rows, list) and len(rows) == 0):
                await io.emit_to(conversation_sid, "receive_message", {"status": False, "message": t_default("INVALID_RECEIVER"), "data": {}})
                return
            receiver = rows[0]
            if g(data, "message_type") == "image" and truthy(g(data, "media_link")):
                await line_service.send_line_image_message_sdk(receiver.get("line_user_id"), data["media_link"])
            else:
                await line_service.send_line_text_message(receiver.get("line_user_id"), g(data, "body"))
            message_data = build_message_data(data, is_user, group_info.get("unique_name"), or_(g(data, "platform"), "line"),
                                              {"wa_message_id": None, "read_status": "sent"})
            import asyncio

            messages, _ = await asyncio.gather(
                aws_message_repo.create_message_with_multi_language(message_data),
                group_repo.update_group_by_id(build_group_payload(data, is_user), g(data, "conversation_sid")),
            )
            message_id = g(messages, "insert_id")
            message_data["conversation_sid"] = g(data, "conversation_sid")
            message_data["profile_image"] = resolve_profile_image(is_user.get("profile_image"))
            message_data["read_status"] = "sent"
            message_data["id"] = message_id
            message_data["message_id"] = message_id
            _apply_message_fields(message_data, messages)
            mobile = js_string(or_(receiver.get("whatsapp_country_code"), "")) + js_string(or_(receiver.get("whatsapp_number"), ""))
            receiver_data = build_receiver_data(message_data, message_id, is_user, group_info.get("unique_name"), mobile, group_info, messages)
            await io.emit_to(conversation_sid, "send_message", {"status": True, "message": "Success", "data": message_data})
            await io.emit_to(conversation_sid, "receive_message", {"status": True, "message": "Success", "data": receiver_data})
        except Exception as error:  # noqa: BLE001
            await io.emit_to(conversation_sid, "receive_message", {"status": False, "message": developer_message_or_message(error)})

    async def sent_message_to_user_whatsapp(self, data: Any, is_user: dict, group_info: dict) -> None:
        conversation_sid = _pi(g(data, "conversation_sid"))
        try:
            rows = await users_repo.get_user_info_in_view(
                group_info.get("unique_name"),
                "CONCAT(country_code, mobile_number) as mobile_number,whatsapp_country_code,whatsapp_number,guard_name,related_name as full_name,guard_name,profile_image,user_id, user_id as id, employer_worker_staff_common_id",
            )
            if not truthy(rows) or (isinstance(rows, list) and len(rows) == 0):
                await io.emit_to(conversation_sid, "receive_message", {"status": False, "message": t_default("INVALID_RECEIVER"), "data": {}})
                return
            receiver = rows[0]
            if not truthy(receiver.get("whatsapp_country_code")) and not truthy(receiver.get("whatsapp_number")):
                await io.emit_to(conversation_sid, "receive_message", {"status": False, "message": "WhatsApp mobile number is required", "data": {}})
                return
            from app.core.js import strip_phone

            to_number = strip_phone(or_(receiver.get("whatsapp_country_code"), "")) + strip_phone(or_(receiver.get("whatsapp_number"), ""))
            last = await users_repo.get_details_by_user_id(receiver.get("id"), ["id", "last_whatsapp_message_ceated_at"])
            if last is None:
                raise TypeError("Cannot read properties of null (reading 'last_whatsapp_message_ceated_at')")
            last_time = g(last, "last_whatsapp_message_ceated_at")
            passed = False
            if truthy(last_time):
                parsed = parse_date(last_time)
                passed = parsed is not None and (time.time() - parsed.timestamp()) / 3600 >= 24
            if not truthy(last_time) or passed:
                await whatsapp_service.send_welcome_message_whatsapp(to_number, "en", receiver.get("full_name"))
                welcome = build_message_data(data, is_user, group_info.get("unique_name"), "whatsapp", {
                    "content": f"Hi {js_string(receiver.get('full_name', UNDEFINED))}, This is py_chat by Overseas Employment Centre. Your chat is currently paused. Please reply to this message to continue the conversation.",
                    "wa_message_id": "",
                    "read_status": "sent",
                })
                import asyncio

                await asyncio.gather(
                    users_repo.update_details_by_user_id(receiver.get("id"), {"last_whatsapp_message_ceated_at": datetime.now(UTC).replace(tzinfo=None)}),
                    aws_message_repo.create_message(welcome),
                )
            media_link = g(data, "media_link")
            media_type = g(data, "media_type")
            if truthy(media_link) and media_type != "text":
                response = await whatsapp_service.send_media_message_whatsapp(media_link, "document" if media_type == "file" else media_type, to_number)
            else:
                response = await whatsapp_service.send_text_message_whatsapp(g(data, "body"), to_number)
            body = response.data if response is not None else None
            if isinstance(body, dict):
                wa_messages = body.get("messages")
                if wa_messages is None:
                    raise TypeError("Cannot read properties of undefined (reading '0')")
                wa_id = g(wa_messages[0], "id") if wa_messages else UNDEFINED
            else:
                wa_id = UNDEFINED
            message_data = build_message_data(data, is_user, group_info.get("unique_name"), "whatsapp", {"wa_message_id": wa_id, "read_status": "sent"})
            import asyncio

            messages, _ = await asyncio.gather(
                aws_message_repo.create_message_with_multi_language(message_data),
                group_repo.update_group_by_id(build_group_payload(data, is_user), g(data, "conversation_sid")),
            )
            message_id = g(messages, "insert_id")
            _apply_message_fields(message_data, messages)
            message_data["conversation_sid"] = g(data, "conversation_sid")
            message_data["profile_image"] = resolve_profile_image(is_user.get("profile_image"))
            message_data["read_status"] = "sent"
            message_data["id"] = message_id
            message_data["message_id"] = message_id
            mobile = js_string(or_(receiver.get("whatsapp_country_code"), "")) + js_string(or_(receiver.get("whatsapp_number"), ""))
            receiver_data = build_receiver_data(message_data, message_id, is_user, group_info.get("unique_name"), mobile, group_info, messages)
            receiver_data["employer_worker_staff_common_id"] = receiver.get("employer_worker_staff_common_id", UNDEFINED)
            await io.emit_to(conversation_sid, "send_message", {"status": True, "message": "Success", "data": message_data})
            await io.emit_to(conversation_sid, "receive_message", {"status": True, "message": "Success", "data": receiver_data})
            await io.emit_to(is_user.get("id"), "receive_message_all", {"status": True, "message": "Success", "data": receiver_data})
        except Exception as error:  # noqa: BLE001
            await io.emit_to(conversation_sid, "receive_message", {"status": False, "message": developer_message_or_message(error)})

    async def sent_message_to_user_action_required(self, data: Any, is_user: dict, group_info: dict) -> None:
        conversation_sid = _pi(g(data, "conversation_sid"))
        try:
            rows = await users_repo.get_user_info_in_view(
                group_info.get("unique_name"),
                "CONCAT(country_code, mobile_number) as mobile_number,whatsapp_country_code,whatsapp_number,guard_name,related_name as full_name,guard_name,profile_image,user_id, user_id as id",
            )
            if not truthy(rows) or (isinstance(rows, list) and len(rows) == 0):
                await io.emit_to(conversation_sid, "receive_message", {"status": False, "message": t_default("INVALID_RECEIVER"), "data": {}})
                return
            receiver = rows[0]
            message_data = build_message_data(data, is_user, group_info.get("unique_name"), "action_required")
            import asyncio

            messages, _ = await asyncio.gather(
                aws_message_repo.create_message_with_multi_language(message_data),
                group_repo.update_group_by_id(build_group_payload(data, is_user, {"platform_two": "action_required"}), g(data, "conversation_sid")),
            )
            message_id = g(messages, "insert_id")
            message_data["conversation_sid"] = g(data, "conversation_sid")
            message_data["profile_image"] = resolve_profile_image(is_user.get("profile_image"))
            message_data["read_status"] = "unread"
            message_data["id"] = message_id
            message_data["message_id"] = message_id
            _apply_message_fields(message_data, messages)
            mobile = js_string(or_(receiver.get("whatsapp_country_code"), "")) + js_string(or_(receiver.get("whatsapp_number"), ""))
            receiver_data = build_receiver_data(message_data, message_id, is_user, group_info.get("unique_name"), mobile, group_info, messages)
            receiver_user = await users_repo.get_user_devices_token(_pi(receiver.get("id")))
            if isinstance(receiver_user, list) and receiver_user and receiver_user[0].get("role_id") in (3, 4):
                devices = await users_repo.get_user_devices_info_notification(_pi(receiver.get("id")))
                tokens = [d.get("device_token") for d in devices if d.get("device_token")] if isinstance(devices, list) else []
                if tokens:
                    spawn(push.send_fcm_notification(tokens, t_default("CHAT_NOTIFICATION_TITLE"), g(data, "body"), {
                        "event_id": js_string(g(data, "conversation_sid")),
                        "user_id": js_string(receiver_user[0].get("id")),
                        "event_type": "chat_app",
                    }))
            await io.emit_to(conversation_sid, "send_message", {"status": True, "message": "Success", "data": message_data})
            await io.emit_to(conversation_sid, "receive_message", {"status": True, "message": "Success", "data": receiver_data})
        except Exception as error:  # noqa: BLE001
            await io.emit_to(conversation_sid, "receive_message", {"status": False, "message": developer_message_or_message(error)})


def _db_datetime(value: Any) -> Any:
    parsed = parse_date(value)
    return parsed.astimezone(UTC).replace(tzinfo=None) if parsed else value
