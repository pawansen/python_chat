"""Conversation-list services (port of aws.service.js: conversationList,
getGroupTeamRowMeta, getConversationListAllCounts, conversationListAll,
unreadMessageCount, conversationListApp, conversationListNoReplay,
conversactions, getConversactionsHistory, getActionList, filterListConversation).

SQL is ported verbatim (same joins, filters, ordering, LIMIT arithmetic).
Response envelopes, key order and cache keys/TTLs are unchanged.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta
from typing import Any

from app.core.config import get_settings
from app.core.js import UNDEFINED, date_ms, js_string, number, or_, parse_date, parse_int, truthy
from app.core.jsonx import stringify
from app.core.logging import logger
from app.db import raw
from app.redis import cache_keys, cache_service
from app.repositories import aws_message as aws_message_repo
from app.repositories import group as group_repo
from app.repositories import group_participant as gp_repo
from app.services.chat.common import (
    map_with_concurrency,
    profile_image_inline,
    resolve_profile_image,
    resolve_scoped_user_id,
    sha1,
)


def _g(obj: Any, key: str, default: Any = UNDEFINED) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else default


def _paging(body: dict) -> tuple[Any, Any]:
    page = _g(body, "page")
    page = 1 if page is UNDEFINED else page
    limit = _g(body, "limit")
    limit = get_settings().chat_conversation_limit if limit is UNDEFINED else limit
    return page, limit


def _offset(page: Any, limit: Any) -> Any:
    return parse_int((number(page) - 1) * number(limit))


def _sql_int(value: Any) -> str:
    """String interpolation of ``parseInt(x)`` (NaN -> "NaN", like Node)."""
    return js_string(parse_int(value))


def _p(value: Any) -> str:
    return "" if value is UNDEFINED or value is None or value == "" else js_string(value)


def _is_num(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _hash_part(value: Any) -> str:
    """``${x || ""}`` inside the sha1 cache-key material."""
    return js_string(value) if truthy(value) else ""


GROUP_TEAM_SQL = (
    "SELECT `groupModel`.* FROM `groups` AS `groupModel` WHERE `groupModel`.`is_deleted` = 0 "
    "AND `groupModel`.`platform` = 'app' AND `groupModel`.`type` = 'GROUP' "
    "AND (SELECT `group_id` FROM `group_participants` AS `groupParticipantModel` "
    "WHERE `groupParticipantModel`.`is_deleted` = 0 AND `groupParticipantModel`.`user_id` = {uid} "
    "AND `groupParticipantModel`.`group_id` = `groupModel`.`id` LIMIT 1) IS NOT NULL "
)


async def conversation_list(req) -> dict[str, Any]:
    try:
        user = req.user_details
        body = req.body
        search, user_id, friend_type = _g(body, "search"), _g(body, "user_id"), _g(body, "friend_type")
        page, limit = _paging(body)
        uid = number(user["id"])
        offset = _offset(page, limit)
        version = await cache_service.get_version(cache_keys.version_conversation_key(uid))
        digest = sha1(f"p{js_string(page)}:l{js_string(limit)}:s{_hash_part(search)}:f{_hash_part(friend_type)}:u{_hash_part(user_id)}")
        key = cache_keys.conversation_list_user_key(uid, version, digest)
        cached = await cache_service.get(key)
        if cached:
            logger.debug("conversationList", {"cache": "hit", "userId": uid})
            return json.loads(cached)

        params: dict[str, Any] = {"userId": uid, "offset": offset, "limit": number(limit)}
        filt = ""
        if truthy(search):
            filt = " AND (`groupModel`.`friendly_name` LIKE :searchPattern)"
            params["searchPattern"] = f"%{js_string(search)}%"
        if truthy(friend_type):
            filt += " AND `groupModel`.`friend_type` = :friendType"
            params["friendType"] = "user" if friend_type == "partner" else friend_type
        order = "ORDER BY `groupModel`.`last_message_ceated_at` DESC"
        if truthy(user_id):
            order = "ORDER BY `groupModel`.`unique_name` = :uniqueName DESC, `groupModel`.`last_message_ceated_at` DESC"
            params["uniqueName"] = f"{js_string(uid)}-{js_string(user_id)}"

        if friend_type == "group_team":
            rows = await raw.select(GROUP_TEAM_SQL.format(uid=":userId") + f"{filt} {order} LIMIT :offset, :limit", params)
        else:
            rows = await raw.select(
                "SELECT `groupModel`.`id`, `groupModel`.`job_description`, `groupModel`.`unique_name`, `groupModel`.`friendly_name`, "
                "`groupModel`.`friend_type`, `groupModel`.`last_message_ceated_at`, `groupModel`.`last_msgtype`, `groupModel`.`last_message`, "
                "MAX(`user_data_view`.`role_id`) AS `role_id`, MAX(`user_data_view`.`staff_type`) AS `staff_type`, "
                "MAX(`user_data_view`.`employer_worker_staff_common_id`) AS `employer_worker_staff_common_id`, "
                "MAX(`user_data_view`.`user_id`) AS `user_id`, MAX(`user_data_view`.`related_name`) AS `name`, "
                "MAX(`user_data_view`.`guard_name`) AS `guard_name`, MAX(`user_data_view`.`profile_image`) AS `profile_image`, "
                "MAX(`user_data_view`.`mobile_number`) AS `mobile_number`, MAX(`user_data_view`.`country_code`) AS `country_code` "
                "FROM `groups` AS `groupModel` "
                "INNER JOIN (SELECT `group_id` FROM `group_participants` WHERE `is_deleted` = 0 AND `user_id` = :userId) AS `groupParticipantModel` "
                "ON `groupModel`.`id` = `groupParticipantModel`.`group_id` "
                "LEFT OUTER JOIN `group_participants` AS `friendParticipant` ON `groupModel`.`id` = `friendParticipant`.`group_id` "
                "AND `friendParticipant`.`is_deleted` = 0 AND `friendParticipant`.`user_id` != :userId "
                "LEFT OUTER JOIN `user_data_view` AS `user_data_view` ON `friendParticipant`.`user_id` = `user_data_view`.`user_id` "
                "WHERE `groupModel`.`is_deleted` = 0 AND `groupModel`.`platform` = 'app' "
                "AND (SELECT `group_id` FROM `group_participants` AS `gp2` WHERE `gp2`.`is_deleted` = 0 AND `gp2`.`user_id` = :userId "
                "AND `gp2`.`group_id` = `groupModel`.`id` LIMIT 1) IS NOT NULL "
                f"{filt} GROUP BY `groupModel`.`id` {order} LIMIT :offset, :limit",
                params,
            )
        if not rows:
            return {"success": False, "data": [], "message": req.t("NOT_FOUND"), "current_page": 0, "limit": 0}
        counts = await asyncio.gather(*(
            aws_message_repo.get_group_unread_message_count(chat["id"]) if chat.get("friend_type") == "group_team"
            else aws_message_repo.get_unread_message_count(_g(chat, "user_id"), chat["id"])
            for chat in rows
        ))
        items = []
        for chat, unread in zip(rows, counts, strict=True):
            item = dict(chat)
            item["conversation_sid"] = chat["id"]
            item["created_at"] = chat.get("last_message_ceated_at")
            item["profile_image"] = resolve_profile_image(chat.get("profile_image"))
            item["unread_message"] = unread
            item["reference_id"] = or_(chat.get("job_description"), None)
            items.append(item)
        result = {"current_page": page, "limit": limit, "data": items}
        if all(_is_num(i["unread_message"]) for i in items):
            await cache_service.set(key, stringify(result), get_settings().cache_ttl_list)
        return result
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": str(error)}


GROUP_MEMBER_ROLE_ORDER = ["employer", "worker", "cs", "interpreter"]


def _empty_meta() -> dict[str, Any]:
    return {"employer_name": None, "employer_chinese_name": None, "worker_name": None, "members": [], "last_message_sender_name": None}


async def get_group_team_row_meta(group_rows: list[dict]) -> dict[int, dict]:
    meta: dict[Any, dict] = {}
    try:
        rows = [r for r in group_rows if r and r.get("id") is not None]
        if not rows:
            return meta
        group_ids = []
        for r in rows:
            gid = number(r["id"])
            if gid not in group_ids:
                group_ids.append(gid)
        for gid in group_ids:
            meta[gid] = _empty_meta()
        participants = await raw.select(
            "SELECT `gp`.`group_id`, `gp`.`user_id`, `gp`.`role_sid`, `u`.`related_name` AS `name`, `e`.`chinese_name` AS `employer_chinese_name` "
            "FROM `group_participants` `gp` LEFT JOIN `user_data_view` `u` ON `u`.`user_id` = `gp`.`user_id` "
            "LEFT JOIN `employers` `e` ON `e`.`user_id` = `gp`.`user_id` "
            "WHERE `gp`.`is_deleted` = 0 AND `gp`.`group_id` IN (:groupIds) ORDER BY `gp`.`group_id`, `gp`.`id`",
            {"groupIds": group_ids},
        )
        names: dict[Any, Any] = {}
        for p in participants:
            m = meta.get(number(p["group_id"]))
            if m is None:
                continue
            if p.get("name"):
                names[number(p["user_id"])] = p["name"]
            role = js_string(p.get("role_sid") or "").strip().lower()
            if role in GROUP_MEMBER_ROLE_ORDER and role not in m["members"]:
                m["members"].append(role)
            if role == "employer" and not m["employer_name"]:
                m["employer_name"] = p.get("name") or None
                m["employer_chinese_name"] = p.get("employer_chinese_name") or None
            elif role == "worker" and not m["worker_name"]:
                m["worker_name"] = p.get("name") or None
        for m in meta.values():
            m["members"] = [r for r in GROUP_MEMBER_ROLE_ORDER if r in m["members"]]
        missing = []
        for r in rows:
            sender = r.get("message_send_by")
            if sender is None:
                continue
            if number(sender) not in names and number(sender) not in missing:
                missing.append(number(sender))
        if missing:
            senders = await raw.select(
                "SELECT `user_id`, `related_name` AS `name` FROM `user_data_view` WHERE `user_id` IN (:senderIds)", {"senderIds": missing}
            )
            for sender in senders:
                if sender.get("name"):
                    names[number(sender["user_id"])] = sender["name"]
        for r in rows:
            m = meta.get(number(r["id"]))
            if m is not None and r.get("message_send_by") is not None:
                m["last_message_sender_name"] = names.get(number(r["message_send_by"])) or None
        return meta
    except Exception as error:  # noqa: BLE001
        logger.error("getGroupTeamRowMeta", {"error": str(error)})
        return meta


async def get_conversation_list_all_counts(self_user_id: Any) -> dict[str, int]:
    zero = {"all": 0, "employer": 0, "worker": 0, "user": 0, "partner": 0, "group_team": 0, "unread": 0, "not_replied": 0}
    try:
        params = {"selfUserId": self_user_id}
        one, grp = await asyncio.gather(
            raw.select(
                "SELECT COUNT(*) AS total, "
                "COALESCE(SUM(t.friend_type = 'employer'), 0) AS employer_count, "
                "COALESCE(SUM(t.friend_type = 'worker'), 0) AS worker_count, "
                "COALESCE(SUM(t.friend_type = 'user' AND (t.staff_type IS NULL OR t.staff_type != 'partner')), 0) AS user_count, "
                "COALESCE(SUM(t.friend_type = 'partner' OR (t.friend_type = 'user' AND t.staff_type = 'partner')), 0) AS partner_count, "
                "COALESCE(SUM(t.unread_count > 0), 0) AS unread_count, "
                "COALESCE(SUM(t.message_send_by IS NOT NULL AND t.message_send_by != :selfUserId), 0) AS not_replied_count "
                "FROM (SELECT `groupModel`.`friend_type`, `groupModel`.`message_send_by`, MAX(`u`.`staff_type`) AS staff_type, "
                "MAX(`gp_self`.`unread_messages_count`) AS unread_count FROM `groups` `groupModel` "
                "INNER JOIN `group_participants` `gp_self` ON `groupModel`.`id` = `gp_self`.`group_id` AND `gp_self`.`user_id` = :selfUserId AND `gp_self`.`is_deleted` = 0 "
                "INNER JOIN `group_participants` `friendParticipant` ON `groupModel`.`id` = `friendParticipant`.`group_id` AND `friendParticipant`.`user_id` != :selfUserId AND `friendParticipant`.`is_deleted` = 0 "
                "INNER JOIN (SELECT `fp`.`user_id` AS friend_user_id, MAX(`g2`.`last_message_ceated_at`) AS last_msg_time FROM `groups` `g2` "
                "INNER JOIN `group_participants` `sp` ON `g2`.`id` = `sp`.`group_id` AND `sp`.`user_id` = :selfUserId AND `sp`.`is_deleted` = 0 "
                "INNER JOIN `group_participants` `fp` ON `g2`.`id` = `fp`.`group_id` AND `fp`.`user_id` != :selfUserId AND `fp`.`is_deleted` = 0 "
                "WHERE `g2`.`is_deleted` = 0 AND `g2`.`platform` IN ('app', 'whatsapp', 'line') GROUP BY `fp`.`user_id`) `latest` "
                "ON `latest`.`friend_user_id` = `friendParticipant`.`user_id` AND `latest`.`last_msg_time` = `groupModel`.`last_message_ceated_at` "
                "LEFT JOIN `user_data_view` `u` ON `u`.`user_id` = `friendParticipant`.`user_id` "
                "WHERE `groupModel`.`is_deleted` = 0 AND `groupModel`.`platform` IN ('app', 'whatsapp', 'line') AND `groupModel`.`platform_two` != 'action_required' "
                "AND (`groupModel`.`platform` NOT IN ('whatsapp', 'line') OR `friendParticipant`.`account_sid` = 1 OR `friendParticipant`.`user_id` = 1) "
                "GROUP BY `friendParticipant`.`user_id`, `groupModel`.`id`) t",
                params,
            ),
            raw.select(
                "SELECT COUNT(*) AS total, COALESCE(SUM(`gp`.`unread_messages_count` > 0), 0) AS unread_count, "
                "COALESCE(SUM(`g`.`message_send_by` IS NOT NULL AND `g`.`message_send_by` != :selfUserId), 0) AS not_replied_count "
                "FROM `groups` `g` INNER JOIN `group_participants` `gp` ON `g`.`id` = `gp`.`group_id` AND `gp`.`user_id` = :selfUserId AND `gp`.`is_deleted` = 0 "
                "WHERE `g`.`is_deleted` = 0 AND `g`.`platform` = 'app' AND `g`.`type` = 'GROUP'",
                params,
            ),
        )
        o = one[0] if one else {}
        gr = grp[0] if grp else {}

        def n(row: dict, key: str) -> Any:
            return number(row.get(key) or 0)

        return {
            "all": n(o, "total") + n(gr, "total"),
            "employer": n(o, "employer_count"),
            "worker": n(o, "worker_count"),
            "user": n(o, "user_count"),
            "partner": n(o, "partner_count"),
            "group_team": n(gr, "total"),
            "unread": n(o, "unread_count") + n(gr, "unread_count"),
            "not_replied": n(o, "not_replied_count") + n(gr, "not_replied_count"),
        }
    except Exception as error:  # noqa: BLE001
        logger.error("getConversationListAllCounts", {"userId": self_user_id, "error": str(error)})
        return zero


CONTACT_CHANNEL_ORDER = ["app", "whatsapp", "line"]


def resolve_contact_channels(channels_csv: Any, fallback_platform: Any) -> list[str]:
    found = {s.strip() for s in js_string(channels_csv or "").split(",") if s.strip() in CONTACT_CHANNEL_ORDER}
    if not found and fallback_platform in CONTACT_CHANNEL_ORDER:
        found.add(fallback_platform)
    return [p for p in CONTACT_CHANNEL_ORDER if p in found]


ONE_TO_ONE_ALL_SQL = (
    "SELECT groupModel.id, groupModel.platform, groupModel.job_description, groupModel.unique_name, groupModel.friendly_name, "
    "groupModel.friend_type, groupModel.last_message_ceated_at, groupModel.last_msgtype, groupModel.last_message, MAX(u.role_id) AS role_id, "
    "MAX(u.staff_type) AS staff_type, MAX(u.employer_worker_staff_common_id) AS employer_worker_staff_common_id, friendParticipant.user_id, "
    "MAX(u.related_name) AS name, MAX(u.guard_name) AS guard_name, MAX(u.profile_image) AS profile_image, MAX(u.mobile_number) AS mobile_number, "
    "MAX(u.country_code) AS country_code, COALESCE(MAX(w.reference_id), MAX(e.reference_id)) AS person_reference_id, "
    "COALESCE(MAX(w.chinese_name), MAX(e.chinese_name)) AS chinese_name, MAX(latest.channels_csv) AS channels_csv, MAX(ag.agency_code) AS partner_code "
    "FROM `groups` groupModel INNER JOIN group_participants gp_self ON groupModel.id = gp_self.group_id AND gp_self.user_id = :selfUserId AND gp_self.is_deleted = 0 "
    "INNER JOIN group_participants friendParticipant ON groupModel.id = friendParticipant.group_id AND friendParticipant.user_id != :selfUserId AND friendParticipant.is_deleted = 0 "
    "INNER JOIN(SELECT fp.user_id AS friend_user_id, MAX(g2.last_message_ceated_at) AS last_msg_time, "
    "GROUP_CONCAT(DISTINCT CASE WHEN g2.platform_two = 'action_required' THEN NULL ELSE g2.platform END) AS channels_csv FROM`groups` g2 "
    "INNER JOIN group_participants sp ON g2.id = sp.group_id AND sp.user_id = :selfUserId AND sp.is_deleted = 0 "
    "INNER JOIN group_participants fp ON g2.id = fp.group_id AND fp.user_id != :selfUserId AND fp.is_deleted = 0 "
    "WHERE g2.is_deleted = 0 AND g2.platform IN('app', 'whatsapp', 'line') GROUP BY fp.user_id) latest "
    "ON latest.friend_user_id = friendParticipant.user_id AND latest.last_msg_time = groupModel.last_message_ceated_at "
    "LEFT JOIN user_data_view u ON u.user_id = friendParticipant.user_id LEFT JOIN workers w ON w.user_id = friendParticipant.user_id "
    "LEFT JOIN employers e ON e.user_id = friendParticipant.user_id LEFT JOIN staffs st ON st.user_id = friendParticipant.user_id AND st.staff_type = 'partner' "
    "LEFT JOIN agencies ag ON ag.id = st.staff_agency_id WHERE groupModel.is_deleted = 0 {type_filter} "
    "AND groupModel.platform IN('app', 'whatsapp', 'line') AND groupModel.platform_two != 'action_required' "
    "AND(groupModel.platform NOT IN('whatsapp', 'line') OR friendParticipant.account_sid = 1 OR friendParticipant.user_id = 1) "
)


async def conversation_list_all(req) -> dict[str, Any]:
    try:
        user = req.user_details
        body = req.body
        search, user_id, platform = _g(body, "search"), _g(body, "user_id"), _g(body, "platform")
        friend_type, action_type = _g(body, "friend_type"), _g(body, "actionType")
        page, limit = _paging(body)
        started = time.monotonic()
        version = await cache_service.get_version(cache_keys.version_conversation_key(user["id"]))
        key = cache_keys.conversation_list_all_key(
            user["id"],
            version,
            sha1(
                f"p{js_string(page)}:l{js_string(limit)}:s{_hash_part(search)}:f{_hash_part(friend_type)}:u{_hash_part(user_id)}"
                f":pl{_hash_part(platform)}:a{_hash_part(action_type)}"
            ),
        )
        cached = await cache_service.get(key)
        if cached:
            logger.debug("conversationListAll", {"cache": "hit", "userId": user["id"], "totalMs": int((time.monotonic() - started) * 1000)})
            return json.loads(cached)

        filt, group_filt = "", ""
        params: dict[str, Any] = {"selfUserId": parse_int(user["id"])}
        counts_task = asyncio.ensure_future(get_conversation_list_all_counts(params["selfUserId"]))
        if truthy(search):
            filt = " AND (`u`.`related_name` LIKE :searchPattern)"
            group_filt = " AND (`groupModel`.`friendly_name` LIKE :searchPattern)"
            params["searchPattern"] = "%" + js_string(search) + "%"
        type_filter = " "
        if truthy(friend_type):
            group_filt += " AND `groupModel`.`friend_type` = :friendType"
            type_filter = " AND `groupModel`.`friend_type` = :friendType"
            params["friendType"] = "user" if friend_type == "partner" else friend_type
        action = js_string(action_type if truthy(action_type) else "").strip().lower()
        if action == "unread":
            filt += " AND `gp_self`.`unread_messages_count` > 0"
            group_filt += (
                " AND (SELECT `gp_a`.`unread_messages_count` FROM `group_participants` AS `gp_a` WHERE `gp_a`.`is_deleted` = 0 "
                "AND `gp_a`.`user_id` = :selfUserId AND `gp_a`.`group_id` = `groupModel`.`id` LIMIT 1) > 0"
            )
        elif action == "not_replied":
            clause = " AND `groupModel`.`message_send_by` IS NOT NULL AND `groupModel`.`message_send_by` != :selfUserId"
            filt += clause
            group_filt += clause
        offset = _offset(page, limit)
        order = " ORDER BY groupModel.last_message_ceated_at DESC "
        if truthy(user_id):
            order = "ORDER BY `groupModel`.`unique_name` = :uniqueName DESC, `groupModel`.`last_message_ceated_at` DESC"
            params["uniqueName"] = f"{js_string(user['id'])}-{js_string(user_id)}"
        lim = f" LIMIT {_sql_int(offset)}, {_sql_int(limit)}"

        sql_started = time.monotonic()
        if friend_type == "group_team":
            merged = await raw.select(GROUP_TEAM_SQL.format(uid=":selfUserId") + group_filt + " " + order + lim, params)
        else:
            one_to_one = await raw.select(
                ONE_TO_ONE_ALL_SQL.format(type_filter=type_filter) + filt + " GROUP BY friendParticipant.user_id, groupModel.id " + order + lim,
                params,
            )
            groups_rows: list[dict] = []
            if friend_type is UNDEFINED or friend_type == "":
                groups_rows = await raw.select(GROUP_TEAM_SQL.format(uid=":selfUserId") + group_filt + " " + order + lim, params)
            merged = list(one_to_one)
            existing = {c["id"] for c in merged}
            for chat in groups_rows:
                if chat["id"] not in existing:
                    merged.append(chat)
            merged.sort(key=lambda c: date_ms(c.get("last_message_ceated_at")) if c.get("last_message_ceated_at") else 0, reverse=True)
        sql_ms = int((time.monotonic() - sql_started) * 1000)

        dynamo_started = time.monotonic()
        needs_batch = any(c.get("friend_type") != "group_team" and c.get("platform") == "app" and c.get("user_id") is not None for c in merged)
        meta_task = asyncio.ensure_future(get_group_team_row_meta([c for c in merged if c.get("friend_type") == "group_team"]))
        by_group = await aws_message_repo.get_unread_counts_by_receiver(user["id"]) if needs_batch else {}

        async def per_chat(chat: dict) -> dict[str, Any]:
            if chat.get("friend_type") == "group_team":
                return {"unread_message": await aws_message_repo.get_group_unread_message_count(chat["id"])}
            last: Any = False
            active = False
            expires = None
            if chat.get("platform") == "whatsapp":
                last = await aws_message_repo.get_message_by_receiver_id(chat["id"], _g(chat, "user_id"))
                if truthy(last):
                    created = parse_date(_g(last, "created_at"))
                    if created is not None:
                        active = (time.time() - created.timestamp()) / 3600 < 24
                        expires = created + timedelta(hours=24)
            if chat.get("platform") == "app" and chat.get("user_id") is not None:
                unread = by_group.get(number(chat["id"]), 0)
            else:
                unread = await aws_message_repo.get_unread_message_count(_g(chat, "user_id"), chat["id"])
            return {"unread_message": unread, "lastMessage": last, "isActiveSeason": active, "wa_window_expires_at": expires}

        per_data = await map_with_concurrency(merged, get_settings().chat_list_dynamo_concurrency, per_chat)
        dynamo_ms = int((time.monotonic() - dynamo_started) * 1000)
        meta = await meta_task
        items = []
        for chat, data in zip(merged, per_data, strict=True):
            item = dict(chat)
            if chat.get("friend_type") == "group_team":
                item["conversation_sid"] = chat["id"]
                item["created_at"] = chat.get("last_message_ceated_at")
                item["profile_image"] = profile_image_inline(_g(chat, "profile_image"))
                item["unread_message"] = data["unread_message"]
                item["reference_id"] = _g(chat, "job_description")
                item["chinese_name"] = None
                item["partner_code"] = None
                item["channels"] = ["app"]
                item.update(meta.get(number(chat["id"])) or _empty_meta())
            else:
                staff_type = chat.get("staff_type")
                item["conversation_sid"] = chat["id"]
                item["name"] = f"{js_string(chat.get('name'))}({staff_type})" if staff_type is not None and staff_type == "partner" else chat.get("name")
                item["created_at"] = chat.get("last_message_ceated_at")
                item["profile_image"] = profile_image_inline(chat.get("profile_image"))
                item["unread_message"] = data["unread_message"]
                item["receiver_lastMessage"] = truthy(data["lastMessage"])
                item["reference_id"] = chat.get("person_reference_id") or _g(chat, "job_description")
                item["chinese_name"] = chat.get("chinese_name") or None
                item["partner_code"] = (chat.get("partner_code") or None) if staff_type == "partner" else None
                item["channels"] = resolve_contact_channels(chat.get("channels_csv"), chat.get("platform"))
                item["isActiveSeason"] = data["isActiveSeason"]
                item["wa_window_expires_at"] = data["wa_window_expires_at"]
            items.append(item)
        result = {"current_page": page, "limit": limit, "data": items, "counts": await counts_task}
        await cache_service.set(key, stringify(result), get_settings().redis.cache_ttl_long)
        logger.debug("conversationListAll", {"cache": "miss", "userId": user["id"], "conversations": len(items), "sqlMs": sql_ms,
                                             "dynamoMs": dynamo_ms, "totalMs": int((time.monotonic() - started) * 1000)})
        return result
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": str(error)}


async def unread_message_count(req) -> dict[str, Any]:
    try:
        platform = _g(req.body, "platform") if isinstance(req.body, dict) else UNDEFINED
        started = time.monotonic()
        total = await gp_repo.get_user_unread_total(req.user_details["id"], platform if truthy(platform) else None)
        logger.debug("unreadMessageCount", {"userId": req.user_details["id"], "platform": platform if truthy(platform) else None,
                                            "total": total, "totalMs": int((time.monotonic() - started) * 1000)})
        if total:
            return {"success": True, "data": {"total_count": total}, "message": req.t("SUCCESS")}
        return {"success": True, "data": {"total_count": 0}, "message": req.t("NOT_FOUND")}
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": str(error)}


async def conversation_list_app(req) -> dict[str, Any]:
    try:
        user = req.user_details
        body = req.body
        search, user_id, friend_type = _g(body, "search"), _g(body, "user_id"), _g(body, "friend_type")
        page, limit = _paging(body)
        version = await cache_service.get_version(cache_keys.version_conversation_key(user["id"]))
        digest = sha1(f"p{js_string(page)}:l{js_string(limit)}:s{_hash_part(search)}:f{_hash_part(friend_type)}:u{_hash_part(user_id)}")
        key = cache_keys.conversation_list_app_key(user["id"], version, digest)
        cached = await cache_service.get(key)
        if cached:
            logger.debug("conversationListApp", {"cache": "hit", "userId": user["id"]})
            return json.loads(cached)
        params: dict[str, Any] = {}
        filt = ""
        if truthy(search):
            filt += " AND (`groupModel`.`friendly_name` LIKE :searchPattern)"
            params["searchPattern"] = f"%{js_string(search)}%"
        if truthy(friend_type):
            filt += " AND `groupModel`.`friend_type` = :friendType"
            params["friendType"] = "user" if friend_type == "partner" else friend_type
        params["userId"] = user["id"]
        params["offset"] = _offset(page, limit)
        params["limit"] = parse_int(limit)
        order = "ORDER BY `groupModel`.`last_message_ceated_at` DESC"
        if truthy(user_id):
            order = "ORDER BY `groupModel`.`unique_name` = :uniqueName DESC, `groupModel`.`last_message_ceated_at` DESC"
            params["uniqueName"] = f"{js_string(user['id'])}-{js_string(user_id)}"
        if friend_type == "group_team":
            rows = await raw.select(GROUP_TEAM_SQL.format(uid=":userId") + f"{filt} {order} LIMIT :offset, :limit", params)
        else:
            rows = await raw.select(
                "SELECT `groupModel`.`id`, `groupModel`.`type`, `groupModel`.`platform`, `groupModel`.`platform_two`, "
                "`groupModel`.`job_description`, `groupModel`.`unique_name`, `groupModel`.`friendly_name`, "
                "`groupModel`.`friend_type`, `groupModel`.`last_message_ceated_at`, `groupModel`.`last_msgtype`, `groupModel`.`last_message`, "
                "MAX(`user_data_view`.`role_id`) AS `role_id`, MAX(`user_data_view`.`staff_type`) AS `staff_type`, "
                "MAX(`user_data_view`.`employer_worker_staff_common_id`) AS `employer_worker_staff_common_id`, "
                "MAX(`user_data_view`.`user_id`) AS `user_id`, MAX(`user_data_view`.`related_name`) AS `name`, "
                "MAX(`user_data_view`.`guard_name`) AS `guard_name`, MAX(`user_data_view`.`profile_image`) AS `profile_image`, "
                "MAX(`user_data_view`.`mobile_number`) AS `mobile_number`, MAX(`user_data_view`.`country_code`) AS `country_code` "
                "FROM `groups` AS `groupModel` INNER JOIN (SELECT `group_id` FROM `group_participants` "
                "WHERE `is_deleted` = 0 AND `user_id` = :userId) AS `groupParticipantModel` ON `groupModel`.`id` = `groupParticipantModel`.`group_id` "
                "LEFT OUTER JOIN `group_participants` AS `friendParticipant` ON `groupModel`.`id` = `friendParticipant`.`group_id` "
                "AND `friendParticipant`.`is_deleted` = 0 AND `friendParticipant`.`user_id` != :userId "
                "LEFT OUTER JOIN `user_data_view` AS `user_data_view` ON `friendParticipant`.`user_id` = `user_data_view`.`user_id` "
                "WHERE `groupModel`.`is_deleted` = 0 AND (`groupModel`.`platform_two` != 'whatsapp' OR `groupModel`.`platform_two` IS NULL) "
                "AND (SELECT `group_id` FROM `group_participants` WHERE `is_deleted` = 0 AND `user_id` = :userId "
                "AND `group_id` = `groupModel`.`id` LIMIT 1) IS NOT NULL "
                f"{filt} GROUP BY `groupModel`.`id` {order} LIMIT :offset, :limit",
                params,
            )
        if not rows:
            return {"success": False, "data": [], "message": req.t("NOT_FOUND"), "current_page": 0, "limit": 0}

        async def unread(chat: dict) -> Any:
            if chat.get("friend_type") == "group_team":
                return await aws_message_repo.get_group_unread_message_count(chat["id"])
            return await aws_message_repo.get_unread_message_count_receiver(user["id"], chat["id"])

        counts = await map_with_concurrency(rows, get_settings().chat_list_dynamo_concurrency, unread)
        items = []
        for chat, count in zip(rows, counts, strict=True):
            item = dict(chat)
            item["conversation_sid"] = chat["id"]
            item["created_at"] = chat.get("last_message_ceated_at")
            item["profile_image"] = resolve_profile_image(chat.get("profile_image"))
            item["unread_message"] = count
            item["reference_id"] = _g(chat, "job_description")
            if chat.get("friend_type") == "group_team":
                name = chat.get("friendly_name") or ""
                dash = name.find("-")
                item["friendly_name"] = "Group" + name[dash:] if dash != -1 else name
            else:
                item["name"] = f"{js_string(chat.get('name'))}({chat.get('staff_type')})" if chat.get("staff_type") == "partner" else chat.get("name")
                item["platform"] = "action_required" if _g(chat, "platform_two") == "action_required" else _g(chat, "platform_two")
            items.append(item)
        result = {"current_page": page, "limit": limit, "data": items}
        if all(_is_num(i["unread_message"]) for i in items):
            await cache_service.set(key, stringify(result), get_settings().cache_ttl_list)
        return result
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": str(error)}


async def conversation_list_no_replay(req) -> dict[str, Any]:
    try:
        user = req.user_details
        body = req.body
        search, user_id, friend_type = _g(body, "search"), _g(body, "user_id"), _g(body, "friend_type")
        page, limit = _paging(body)
        filt = ""
        params: dict[str, Any] = {"selfUserId": parse_int(user["id"])}
        if truthy(search):
            filt = " AND (`groupModel`.`friendly_name` LIKE :searchPattern)"
            params["searchPattern"] = "%" + js_string(search) + "%"
        if truthy(friend_type):
            filt += " AND `groupModel`.`friend_type` = :friendType"
            params["friendType"] = "user" if friend_type == "partner" else friend_type
        offset = _offset(page, limit)
        order = "ORDER BY  `groupModel`.`last_message_ceated_at` DESC"
        if truthy(user_id):
            order = "ORDER BY `groupModel`.`unique_name` = :uniqueName DESC, `groupModel`.`last_message_ceated_at` DESC"
            params["uniqueName"] = f"{js_string(user['id'])}-{js_string(user_id)}"
        rows = await raw.select(
            "SELECT `groupModel`.`id`, `groupModel`.`unique_name`,`groupModel`.`friendly_name`, `groupModel`.`friend_type`, "
            "`groupModel`.`last_message_ceated_at`, `groupModel`.`last_msgtype`, `groupModel`.`last_message`, "
            "MAX(`user_data_view`.`role_id`) AS `role_id`, MAX(`user_data_view`.`employer_worker_staff_common_id`) AS `employer_worker_staff_common_id`, "
            "MAX(`user_data_view`.`user_id`) AS `user_id`, MAX(`user_data_view`.`related_name`) AS `name`, MAX(`user_data_view`.`guard_name`) AS `guard_name`, "
            "MAX(`user_data_view`.`profile_image`) AS `profile_image`, MAX(`user_data_view`.`mobile_number`) AS `mobile_number`, "
            "MAX(`user_data_view`.`country_code`) AS `country_code` FROM `groups` AS `groupModel` "
            "INNER JOIN (SELECT * FROM `group_participants` AS `groupParticipantModel` WHERE `groupParticipantModel`.`is_deleted` = 0 "
            "AND `groupParticipantModel`.`user_id` = :selfUserId) AS `groupParticipantModel` ON `groupModel`.`id` = `groupParticipantModel`.`group_id` "
            "LEFT OUTER JOIN `group_participants` AS `friendParticipant` ON `groupModel`.`id` = `friendParticipant`.`group_id` "
            "AND `friendParticipant`.`is_deleted` = 0 AND `friendParticipant`.`user_id` != :selfUserId "
            "LEFT OUTER JOIN `user_data_view` AS `user_data_view` ON `friendParticipant`.`user_id` = `user_data_view`.`user_id` "
            "WHERE `groupModel`.`is_deleted` = 0 AND `groupModel`.`platform` = 'app' AND ( "
            "SELECT `group_id` FROM `group_participants` AS `groupParticipantModel` WHERE `groupParticipantModel`.`is_deleted` = 0 "
            "AND `groupParticipantModel`.`user_id` = :selfUserId AND `groupParticipantModel`.`group_id` = `groupModel`.`id` LIMIT 1) IS NOT NULL "
            + filt + " GROUP BY `groupModel`.`id` " + order + f" LIMIT {_sql_int(offset)}, {_sql_int(limit)}",
            params,
        )

        async def unread(chat: dict) -> Any:
            # `chat.type` is not selected by this query, so this is always the per-user count (unchanged).
            if chat.get("type") == "GROUP":
                return await aws_message_repo.get_group_unread_message_count(chat["id"])
            return await aws_message_repo.get_unread_message_count(chat.get("user_id"), chat["id"])

        counts = await map_with_concurrency(rows, get_settings().chat_list_dynamo_concurrency, unread)
        items = []
        for chat, count in zip(rows, counts, strict=True):
            item = dict(chat)
            item["conversation_sid"] = chat["id"]
            item["created_at"] = chat.get("last_message_ceated_at")
            item["profile_image"] = profile_image_inline(chat.get("profile_image"))
            item["unread_message"] = count
            items.append(item)
        return {"current_page": page, "limit": limit, "data": items}
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": str(error)}


async def conversactions(req) -> dict[str, Any]:
    try:
        body = req.body
        search, user_id = _g(body, "search"), _g(body, "user_id")
        page, limit = _paging(body)
        offset = _offset(page, limit)
        order = "ORDER BY  `groupModel`.`last_message_ceated_at` DESC"
        filt = ""
        params: dict[str, Any] = {"userId": resolve_scoped_user_id(req, user_id), "role": "Admin"}
        if truthy(search):
            filt = " AND (`groupModel`.`friendly_name` LIKE :searchPattern)"
            params["searchPattern"] = "%" + js_string(search) + "%"
        rows = await raw.select(
            "SELECT `groupModel`.*, `user_data_view`.`role_id` AS `role_id`, `user_data_view`.`employer_worker_staff_common_id`,"
            "`user_data_view`.`user_id` AS `user_id`, `user_data_view`.`related_name` AS `name`, `user_data_view`.`guard_name` AS `guard_name`, "
            "`user_data_view`.`profile_image` AS `profile_image`,`user_data_view`.`mobile_number` AS `mobile_number`, "
            "`user_data_view`.`country_code` AS `country_code` FROM( SELECT `groupModel`.`unique_name`,`groupModel`.`id`, "
            "`groupModel`.`last_message_ceated_at`, `groupModel`.`last_msgtype`, `groupModel`.`last_message` FROM `groups` AS `groupModel` "
            "WHERE `groupModel`.`is_deleted` = 0  AND( SELECT `group_id` FROM `group_participants` AS `groupParticipantModel` "
            "WHERE ( ( `groupParticipantModel`.`is_deleted` = 0 AND `groupParticipantModel`.`user_id` = :userId) "
            "AND `groupParticipantModel`.`group_id` = `groupModel`.`id` ) LIMIT 1) IS NOT NULL  " + filt + " " + order
            + f" LIMIT {_sql_int(offset)}, {_sql_int(limit)} ) AS `groupModel` INNER JOIN `group_participants` AS `groupParticipantModel` "
            "ON `groupModel`.`id` = `groupParticipantModel`.`group_id` AND `groupParticipantModel`.`is_deleted` = 0 AND `groupParticipantModel`.`user_id` = :userId"
            " LEFT OUTER JOIN `group_participants` AS `friendParticipant` ON `groupModel`.`id` = `friendParticipant`.`group_id` "
            "AND `friendParticipant`.`is_deleted` = 0 AND `friendParticipant`.`user_id` != :userId"
            " INNER JOIN `user_data_view` AS `user_data_view` ON `friendParticipant`.`user_id` = `user_data_view`.`user_id` "
            "AND `user_data_view`.`guard_name` = :role  " + order,
            params,
        )
        return {"current_page": page, "limit": limit, "data": rows}
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": str(error)}


async def get_conversactions_history(req) -> dict[str, Any]:
    try:
        body = req.body
        search, user_id = _g(body, "search"), _g(body, "user_id")
        page, limit = _paging(body)
        offset = _offset(page, limit)
        order = "ORDER BY  `groupModel`.`last_message_ceated_at` DESC"
        filt = ""
        params: dict[str, Any] = {"userId": resolve_scoped_user_id(req, user_id)}
        if truthy(search):
            filt = " AND (`groupModel`.`friendly_name` LIKE :searchPattern)"
            params["searchPattern"] = "%" + js_string(search) + "%"
        rows = await raw.select(
            "SELECT `groupModel`.`id`,`groupModel`.`platform`,`groupModel`.`platform_two`,`groupModel`.`type`, `groupModel`.`job_description`,"
            "`groupModel`.`unique_name`,`groupModel`.`friendly_name`, `groupModel`.`friend_type`, `groupModel`.`last_message_ceated_at`, "
            "`groupModel`.`last_msgtype`, `groupModel`.`last_message`, MAX(`user_data_view`.`role_id`) AS `role_id`, "
            "MAX(`user_data_view`.`employer_worker_staff_common_id`) AS `employer_worker_staff_common_id`, MAX(`user_data_view`.`user_id`) AS `user_id`, "
            "MAX(`user_data_view`.`related_name`) AS `name`, MAX(`user_data_view`.`guard_name`) AS `guard_name`,MAX(`user_data_view`.`role_name`) AS `role_name`, "
            "MAX(`user_data_view`.`profile_image`) AS `profile_image`, MAX(`user_data_view`.`mobile_number`) AS `mobile_number`, "
            "MAX(`user_data_view`.`country_code`) AS `country_code` FROM `groups` AS `groupModel` "
            "INNER JOIN (SELECT * FROM `group_participants` AS `groupParticipantModel` WHERE `groupParticipantModel`.`is_deleted` = 0 "
            "AND `groupParticipantModel`.`user_id` = :userId) AS `groupParticipantModel` ON `groupModel`.`id` = `groupParticipantModel`.`group_id` "
            "LEFT OUTER JOIN `group_participants` AS `friendParticipant` ON `groupModel`.`id` = `friendParticipant`.`group_id` "
            "AND `friendParticipant`.`is_deleted` = 0 AND `friendParticipant`.`user_id` != :userId "
            "LEFT OUTER JOIN `user_data_view` AS `user_data_view` ON `friendParticipant`.`user_id` = `user_data_view`.`user_id` "
            "WHERE `groupModel`.`is_deleted` = 0 AND (`groupModel`.`platform` = 'app' OR `groupModel`.`platform` = 'whatsapp' OR `groupModel`.`platform` = 'line') "
            "AND `groupModel`.`platform_two` != 'action_required' AND ( SELECT `group_id` FROM `group_participants` AS `groupParticipantModel` "
            "WHERE `groupParticipantModel`.`is_deleted` = 0 AND `groupParticipantModel`.`user_id` = :userId AND `groupParticipantModel`.`group_id` = `groupModel`.`id` LIMIT 1) IS NOT NULL "
            + filt + " GROUP BY `groupModel`.`id` " + order + f" LIMIT {_sql_int(offset)}, 50",
            params,
        )
        return {"current_page": page, "limit": limit, "data": rows}
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": str(error)}


async def get_action_list(req) -> dict[str, Any]:
    try:
        query = req.query
        user_id, action_type = _g(query, "user_id"), _g(query, "action_type")
        page = _g(query, "page")
        page = 1 if page is UNDEFINED else page
        limit = _g(query, "limit")
        limit = get_settings().chat_conversation_limit if limit is UNDEFINED else limit
        filt = ""
        params: dict[str, Any] = {}
        scoped = resolve_scoped_user_id(req, user_id)
        if truthy(scoped):
            filt += " AND `user_id` = :user_id"
            params["user_id"] = scoped
        offset = parse_int((number(page) - 1) * number(limit)) if number(page) > 1 else 0
        order = "ORDER BY  `created_at` DESC"
        lim = f" LIMIT {_sql_int(offset)}, {_sql_int(limit)}"
        has_action = action_type is not UNDEFINED and action_type != ""
        if has_action:
            rows = await raw.select(
                "SELECT id,user_id,worker_id,employer_id,`entity_type`,action_type,content,created_at FROM `action_logs` "
                "WHERE status = 1 AND action_type = :action_type " + filt + " " + order + lim,
                {**params, "action_type": action_type},
            )
            if rows:
                return {"message": "Successfully listed", "data": {"data": rows, "current_page": page, "limit": limit}}
            return {"success": False, "data": {"message": "Records not found", "current_page": 0, "limit": 0, "data": []}}
        rows = await raw.select(
            "SELECT id,user_id,worker_id,employer_id,`entity_type`,action_type,content,created_at FROM `action_logs` WHERE status = 1 "
            + filt + " " + order + lim,
            params,
        )
        if rows:
            return {"current_page": page, "limit": limit, "data": rows}
        return {"success": False, "data": [], "message": req.t("NOT_FOUND"), "current_page": 0, "limit": 0}
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": str(error)}


async def filter_list_conversation(req) -> dict[str, Any]:
    """Legacy Twilio-era list (paginated, BA-M4). Errors propagate to next(error) -> 500."""
    user = req.user_details
    identity = (js_string(user.get("country_code")) + js_string(user.get("mobile_number"))) if truthy(user.get("mobile_number")) and truthy(user.get("country_code")) else user.get("email")
    if not truthy(identity):
        return {"success": False, "data": [], "message": req.t("USER_IDENTITY_REQUIRED")}
    body = req.body
    type_, not_reply, role_id = _g(body, "type"), _g(body, "not_reply"), _g(body, "role_id")
    page = _g(body, "page")
    page = 1 if page is UNDEFINED else page
    limit = _g(body, "limit")
    limit = 100 if limit is UNDEFINED else limit
    parsed_limit = parse_int(limit)
    bounded = min(parsed_limit if truthy(parsed_limit) else 100, 500)
    parsed_page = parse_int(page)
    offset = max(0, (parsed_page if truthy(parsed_page) else 1) - 1) * bounded
    group_ids = await gp_repo.get_all_groups_ids_by_user(user["id"])
    data: Any = []
    total_not_reply: Any = 0
    if isinstance(group_ids, list) and group_ids:
        params: dict[str, Any] = {"ids": group_ids, "uid": user["id"]}
        conds = ["`groupModel`.`id` IN :ids", "`groupModel`.`platform` = 1"]
        if type_ == "ALL":
            pass
        elif truthy(not_reply) and type_ in ("ONE_TO_ONE", "GROUP"):
            conds += ["`groupModel`.`type` = :type", "`groupModel`.`message_send_by` != :uid"]
            params["type"] = type_
        elif role_id is not UNDEFINED and type_ == "ONE_TO_ONE":
            pass
        else:
            conds.append("`groupModel`.`type` = :type")
            params["type"] = type_
        groups_rows = await raw.select(
            "SELECT `groupModel`.*, `UnReadCount`.`id` AS `UnReadCount.id`, `UnReadCount`.`unread_messages_count` AS `UnReadCount.unread_messages_count` "
            "FROM `groups` AS `groupModel` INNER JOIN `group_participants` AS `UnReadCount` ON `groupModel`.`id` = `UnReadCount`.`group_id` "
            f"AND `UnReadCount`.`user_id` = :uid WHERE {' AND '.join(conds)} ORDER BY last_message_ceated_at DESC LIMIT {offset}, {bounded}",
            params,
        )
        data = []
        for row in groups_rows:
            group = {k: v for k, v in row.items() if not k.startswith("UnReadCount.")}
            group["UnReadCount"] = {"unread_messages_count": row.get("UnReadCount.unread_messages_count")}
            participants = await raw.select(
                "SELECT `groupParticipantModel`.`id`, `groupParticipantModel`.`user_id`, `usersModel`.`id` AS `u_id`, `usersModel`.`email`, "
                "`usersModel`.`country_code`, `usersModel`.`mobile_number`, `usersModel`.`role_id`, `usersModel`.`image` "
                "FROM `group_participants` AS `groupParticipantModel` LEFT OUTER JOIN `users` AS `usersModel` "
                "ON `groupParticipantModel`.`user_id` = `usersModel`.`id` AND `usersModel`.`id` != :uid "
                "WHERE `groupParticipantModel`.`group_id` = :gid AND `groupParticipantModel`.`user_id` != :uid ORDER BY id DESC",
                {"gid": row["id"], "uid": user["id"]},
            )
            group["groupParticipantModel"] = [
                {
                    "user_id": p.get("user_id"),
                    "usersModel": None if p.get("u_id") is None else {
                        "id": p.get("u_id"), "email": p.get("email"), "country_code": p.get("country_code"),
                        "mobile_number": p.get("mobile_number"), "role_id": p.get("role_id"), "image": p.get("image"),
                    },
                }
                for p in participants
            ]
            # Node set `friendlyName` on the Sequelize INSTANCE (not dataValues), so it
            # never reached the JSON response; intentionally not reproduced.
            data.append(group)
        total_not_reply = await group_repo.get_all_list_count_from_promise()
    return {
        "success": True,
        "data": data,
        "message": req.t("SUCCESS_LISTED"),
        "totalNotReply": total_not_reply,
        "current_page": parsed_page if truthy(parsed_page) else 1,
        "limit": bounded,
    }


def _unused(*_: Any) -> None:  # keep linters quiet about helpers used by sibling modules
    return None


__all__ = [
    "conversation_list",
    "conversation_list_all",
    "conversation_list_app",
    "conversation_list_no_replay",
    "conversactions",
    "get_conversactions_history",
    "get_action_list",
    "filter_list_conversation",
    "unread_message_count",
    "_p",
]
