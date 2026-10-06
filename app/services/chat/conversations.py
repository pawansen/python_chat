"""Conversation (group) creation services (port of aws.service.js:
fetchFriendDetails, createConversation, createConversationWhatsappActionRequired,
createConversationGroup, validateChangeUserGroupCsInterpreter, createConversationLine).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.core.js import UNDEFINED, js_string, parse_date, truthy
from app.core.tasks import spawn
from app.db import raw
from app.repositories import aws_message as aws_message_repo
from app.repositories import group as group_repo
from app.repositories import group_participant as gp_repo
from app.repositories import people
from app.repositories import users as users_repo
from app.services.chat.common import build_duplicate_group_filter, utcnow
from app.utils.identity import get_friend_identity, get_user_identity

_SET_ERR = "Cannot set properties of undefined (setting 'conversation_sid')"


def _g(obj: Any, key: str) -> Any:
    return obj.get(key, UNDEFINED) if isinstance(obj, dict) else UNDEFINED


def _trim_name(*parts: Any) -> str:
    return " ".join(js_string(p) if truthy(p) else "" for p in parts).strip()


async def fetch_friend_details(friend_type: Any, friend_id: Any, platform: Any, req) -> dict[str, Any]:
    user_info: Any = None
    friendly_name: Any = ""
    friend_role_id: Any = ""
    to_user_id: Any = None
    if friend_type == "employer":
        user_info = await people.employer_get_details_by_user_id(friend_id)
        if not truthy(user_info):
            return {"success": False, "message": req.t("EMPLOYER_NOT_ACTIVE")}
        friendly_name = _trim_name(_g(user_info, "name"), _g(user_info, "surname"))
        friend_role_id = _g(user_info, "role_id")
        to_user_id = _g(user_info, "user_id")
    elif friend_type == "worker":
        user_info = await people.worker_get_details_by_user_id(friend_id)
        if not truthy(user_info):
            return {"success": False, "message": req.t("INACTIVE_WORKER")}
        friendly_name = _trim_name(_g(user_info, "name"), _g(user_info, "surname"))
        friend_role_id = _g(user_info, "role_id")
        to_user_id = _g(user_info, "user_id")
    elif friend_type in ("user", "partner"):
        rows = await users_repo.get_user_info_in_view(
            friend_id,
            "CONCAT(country_code, mobile_number) as mobile_number,guard_name,related_name as full_name,role_id,user_id,whatsapp_country_code,whatsapp_number",
        )
        if not truthy(rows) or (isinstance(rows, list) and len(rows) == 0):
            return {"success": False, "message": req.t("EMPLOYER_NOT_ACTIVE")}
        user_info = rows[0] if isinstance(rows, list) else UNDEFINED
        friendly_name = _g(user_info, "full_name") if truthy(_g(user_info, "full_name")) else ""
        friend_role_id = _g(user_info, "role_id")
        to_user_id = _g(user_info, "user_id")
    if platform == "whatsapp" and truthy(user_info) and not truthy(_g(user_info, "whatsapp_number")) and not truthy(_g(user_info, "whatsapp_country_code")):
        return {"success": False, "message": req.t("WHATSAPP_NOT_VERIFIED_FOR_FRIEND")}
    friend_identity = await get_friend_identity(user_info, platform) if truthy(user_info) else ""
    return {"success": True, "friendly_name": friendly_name, "friend_role_id": friend_role_id, "toUserId": to_user_id, "friend_identity": friend_identity}


async def _create_pair(group_data: dict, user_participant: dict, friend_participant: dict) -> dict:
    async with raw.transaction() as conn:
        group = await group_repo.create_group(group_data, conn)
        group_id = _g(group, "id")
        await gp_repo.create_group_participant({**user_participant, "group_id": group_id}, conn)
        await gp_repo.create_group_participant({**friend_participant, "group_id": group_id}, conn)
        if group_id is UNDEFINED:
            raise TypeError(_SET_ERR)
        group["conversation_sid"] = group_id
        group["platform_two"] = group.get("platform_two")
        return group


async def check_whatsapp_session_activity(platform: Any, group_id: Any, friend_id: Any) -> dict[str, bool]:
    if platform != "whatsapp":
        return {"receiver_lastMessage": False, "isActiveSeason": False}
    last = await aws_message_repo.get_message_by_receiver_id(group_id, friend_id)
    if not truthy(last):
        return {"receiver_lastMessage": False, "isActiveSeason": False}
    created = parse_date(_g(last, "created_at"))
    diff_hours = (time.time() - created.timestamp()) / 3600 if created else float("nan")
    return {"receiver_lastMessage": True, "isActiveSeason": diff_hours < 24}


async def create_conversation(req) -> dict[str, Any]:
    try:
        user = req.user_details
        body = req.body
        platform, friend_id = _g(body, "platform"), _g(body, "friend_id")
        friend_type = _g(body, "friend_type")
        is_wa = platform in ("whatsapp", "action_required")
        unique_name = friend_id if is_wa else f"{js_string(user['id'])}-{js_string(friend_id)}"

        duplicate = await group_repo.check_duplicate_group_with_platform(build_duplicate_group_filter(unique_name, platform))
        if not truthy(duplicate) and friend_type in ("user", "partner"):
            duplicate = await group_repo.check_duplicate_group_with_platform(
                {"unique_name": f"{js_string(friend_id)}-{js_string(user['id'])}", "platform": platform}
            )
        if truthy(duplicate):
            return {"success": True, "data": duplicate, "message": req.t("SUCCESS_CREATED")}

        friend = await fetch_friend_details(friend_type, friend_id, platform, req)
        if not friend["success"]:
            return {"success": False, "data": [], "message": friend["message"]}
        if not truthy(friend["friend_identity"]) and friend_type not in ("user", "partner", "worker"):
            return {"success": False, "data": [], "message": req.t("FRIEND_IDENTITY_REQUIRED")}

        if friend_type == "user":
            rows = await users_repo.get_user_info_in_view(friend_id, "guard_name,user_id, user_id as id")
            if isinstance(rows, list) and rows and rows[0].get("guard_name") in ("worker", "employer"):
                friend_type = rows[0]["guard_name"]

        if platform not in ("app", "whatsapp", "action_required"):
            return {"success": True, "data": None, "message": req.t("SUCCESS_CREATED")}

        user_identity = await get_user_identity(user, platform)
        now = utcnow()
        group = await _create_pair(
            {
                "conversation_state": "Active",
                "conversation_bindings": None,
                "friendly_name": friend["friendly_name"],
                "unique_name": unique_name,
                "type": "ONE_TO_MANY" if is_wa else "ONE_TO_ONE",
                "platform": "whatsapp" if is_wa else "app",
                "platform_two": "whatsapp" if platform == "whatsapp" else ("action_required" if platform == "action_required" else "app"),
                "friend_type": friend_type,
                "created_by": user.get("id"),
                "created_at": now,
            },
            {"identity": user_identity, "user_id": user.get("id"), "is_host": 1, "role_id": user.get("role_id", UNDEFINED),
             "joined_on": now, "created_by": user.get("id"), "created_at": now},
            {"identity": friend["friend_identity"], "user_id": friend_id, "is_host": 0, "role_id": friend["friend_role_id"],
             "joined_on": now, "created_by": user.get("id"), "created_at": now, "account_sid": 1 if is_wa else 0},
        )
        return {"success": True, "data": group, "message": req.t("SUCCESS_CREATED")}
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": str(error)}


async def create_conversation_whatsapp_action_required(req) -> dict[str, Any]:
    try:
        user = req.user_details
        body = req.body
        platform, friend_id, friend_type = _g(body, "platform"), _g(body, "friend_id"), _g(body, "friend_type")
        unique_name = friend_id
        is_wa = platform in ("whatsapp", "action_required")
        duplicate = await group_repo.check_duplicate_group_with_platform(build_duplicate_group_filter(unique_name, platform))
        if truthy(duplicate):
            # Phase 8: INCLUSIVE re-add lookup (must see a soft-deleted row).
            participant = await gp_repo.get_group_participant_details(
                {"group_id": duplicate.get("id"), "user_id": user.get("id")}, ["id", "identity", "group_id", "user_id", "role_id"]
            )
            if not truthy(participant):
                now = utcnow()
                await gp_repo.create_group_participant_insert({
                    "identity": await get_user_identity(user, platform), "group_id": duplicate.get("id"), "user_id": user.get("id"),
                    "is_host": 1, "role_id": user.get("role_id", UNDEFINED), "joined_on": now, "created_by": user.get("id"), "created_at": now,
                })
            session = await check_whatsapp_session_activity(platform, duplicate.get("id"), friend_id)
            return {"success": True, "data": {**duplicate, **session}, "message": req.t("SUCCESS_CREATED")}

        staff, friend = await asyncio.gather(
            people.staff_get_details_by_user_id(user.get("id"), ["id", "first_name", "last_name"]),
            fetch_friend_details(friend_type, friend_id, platform, req),
        )
        if not friend["success"]:
            return {"success": False, "data": [], "message": friend["message"]}
        friendly_name = _trim_name(_g(staff, "first_name"), _g(staff, "last_name")) + "/" + js_string(friend["friendly_name"])
        if not truthy(friend["friend_identity"]) and friend_type not in ("user", "partner", "worker"):
            return {"success": False, "data": [], "message": req.t("FRIEND_IDENTITY_REQUIRED")}
        if not is_wa and platform != "app":
            return {"success": True, "data": None, "message": req.t("SUCCESS_CREATED")}
        user_identity = await get_user_identity(user, platform)
        now = utcnow()
        group = await _create_pair(
            {
                "conversation_state": "Active",
                "conversation_bindings": None,
                "friendly_name": friendly_name,
                "unique_name": unique_name,
                "type": "ONE_TO_MANY" if is_wa else "ONE_TO_ONE",
                "platform": "whatsapp" if is_wa else "app",
                "platform_two": "whatsapp" if platform == "whatsapp" else ("action_required" if platform == "action_required" else "app"),
                "friend_type": friend_type,
                "created_by": user.get("id"),
                "created_at": now,
            },
            {"identity": user_identity, "user_id": user.get("id"), "is_host": 1, "role_id": user.get("role_id", UNDEFINED),
             "joined_on": now, "created_by": user.get("id"), "created_at": now},
            {"identity": friend["friend_identity"], "user_id": friend_id, "is_host": 0, "role_id": friend["friend_role_id"],
             "joined_on": now, "created_by": user.get("id"), "created_at": now, "account_sid": 1 if is_wa else 0},
        )
        session = await check_whatsapp_session_activity(platform, group["id"], friend_id)
        return {"success": True, "data": {**group, **session}, "message": req.t("SUCCESS_CREATED")}
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": str(error)}


def _participant(identity: Any, group_id: Any, user_id: Any, role_sid: str, created_by: Any, now) -> dict[str, Any]:
    return {
        "identity": identity if truthy(identity) else "",
        "group_id": group_id, "user_id": user_id, "is_host": 1, "role_id": 0, "role_sid": role_sid,
        "joined_on": now, "created_by": created_by, "created_at": now,
    }


INTERPRETER_SQL = (
    "SELECT worker_interpreter_assignment_log.staff_id, worker_interpreter_assignment_log.user_id, users.email, "
    "staffs.first_name, staffs.last_name FROM worker_interpreter_assignment_log "
    "INNER JOIN staffs ON staffs.id = worker_interpreter_assignment_log.staff_id "
    "INNER JOIN users ON users.id = worker_interpreter_assignment_log.user_id "
    "WHERE worker_interpreter_assignment_log.worker_id = :worker_id AND worker_interpreter_assignment_log.{job_col} = :job_id "
    "AND worker_interpreter_assignment_log.status = 1{extra} LIMIT 1"
)


async def create_conversation_group(req) -> dict[str, Any]:
    try:
        user = req.user_details
        body = req.body
        platform, group_key = _g(body, "platform"), _g(body, "id")
        job_worker_id, job_interview_id = _g(body, "job_worker_id"), _g(body, "job_interview_id")
        duplicate = await group_repo.check_duplicate_group_with_participants(group_key)
        if truthy(duplicate):
            participants = duplicate.get("groupParticipantModel") or []
            if participants:
                group_users = [p["user_id"] for p in participants]
                interpreters = await raw.select(
                    INTERPRETER_SQL.format(job_col="job_id", extra=" AND worker_interpreter_assignment_log.user_id NOT IN (:groupUsers)"),
                    {"worker_id": job_worker_id, "job_id": job_interview_id, "groupUsers": group_users},
                )
                if interpreters:
                    now = utcnow()
                    await gp_repo.bulk_create_group_participants_custom([
                        _participant(_trim_name(i.get("first_name"), i.get("last_name")), duplicate["id"], i["user_id"], "interpreter", user.get("id"), now)
                        for i in interpreters
                    ])
            return {"success": True, "data": duplicate, "message": req.t("SUCCESS_CREATED")}

        info_rows = await raw.select(
            "SELECT IB.job_id, IB.employer_id, IB.worker_id, IB.staff_id, IB.status, E.cs_owner_id, E.user_id as employer_user_id, "
            "E.surname as employer_name, W.user_id as worker_user_id, W.surname as worker_name, S.user_id as staff_user_id, "
            "S.last_name as staff_name, J.reference_id as job_reference_id FROM interview_bookings as IB "
            "LEFT JOIN employers as E ON IB.employer_id = E.id LEFT JOIN workers as W ON IB.worker_id = W.id "
            "LEFT JOIN staffs as S ON E.cs_owner_id = S.id LEFT JOIN jobs as J ON IB.job_id = J.id "
            "WHERE IB.job_stage_id = :job_stage_id LIMIT 1",
            {"job_stage_id": group_key},
        )
        if len(info_rows) != 1:
            return {"success": False, "data": [], "message": req.t("NOT_FOUND")}
        job = info_rows[0]
        friendly_name = f"{job.get('job_reference_id') or ''}-{job.get('employer_name') or ''}-{job.get('worker_name') or ''}"
        if platform not in ("app", "whatsapp"):
            return {"success": True, "data": None, "message": req.t("SUCCESS_CREATED")}
        interpreters = await raw.select(
            INTERPRETER_SQL.format(job_col="job_id", extra=""), {"worker_id": job.get("worker_id"), "job_id": job.get("job_id")}
        )
        now = utcnow()
        async with raw.transaction() as conn:
            group = await group_repo.create_group(
                {
                    "conversation_state": "Active", "conversation_bindings": None, "friendly_name": friendly_name,
                    "unique_name": group_key, "type": "GROUP", "platform": platform, "job_id": group_key,
                    "job_worker_id": job_worker_id if truthy(job_worker_id) else 0, "job_description": job.get("job_reference_id"),
                    "friend_type": "group_team", "created_by": user.get("id"), "created_at": now,
                },
                conn,
            )
            group_id = _g(group, "id")
            rows = []
            if job.get("employer_user_id"):
                rows.append(_participant(job.get("employer_name"), group_id, job["employer_user_id"], "employer", user.get("id"), now))
            if job.get("worker_user_id"):
                rows.append(_participant(job.get("worker_name"), group_id, job["worker_user_id"], "worker", user.get("id"), now))
            if job.get("staff_user_id"):
                rows.append(_participant(job.get("staff_name"), group_id, job["staff_user_id"], "cs", user.get("id"), now))
            for interpreter in interpreters:
                rows.append(_participant(_trim_name(interpreter.get("first_name"), interpreter.get("last_name")), group_id, interpreter["user_id"], "interpreter", user.get("id"), now))
            if rows:
                await gp_repo.bulk_create_group_participants(rows, conn)
            if group_id is UNDEFINED:
                raise TypeError(_SET_ERR)
            group["conversation_sid"] = group_id
        return {"success": True, "data": group, "message": req.t("SUCCESS_CREATED")}
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": str(error)}


def _cs_payload(last_name: Any, group_id: Any, user_id: Any, role_sid: str, created_by: Any) -> dict[str, Any]:
    now = utcnow()
    return {"identity": js_string(last_name).strip(), "group_id": group_id, "user_id": user_id, "is_host": 1, "role_id": 0,
            "role_sid": role_sid, "joined_on": now, "created_by": created_by, "created_at": now}


async def _add_if_missing(group_id: Any, user_id: Any, payload: dict[str, Any]) -> None:
    existing = await gp_repo.check_user_added_in_group(group_id, user_id)
    if not truthy(existing):
        await gp_repo.create_group_participant_insert(payload)


async def validate_change_user_group_cs_interpreter(req) -> Any:
    try:
        user = req.user_details
        body = req.body
        user_id, group_id = _g(body, "user_id"), _g(body, "group_id")
        user_type, job_stage_id, employer_id = _g(body, "user_type"), _g(body, "job_stage_id"), _g(body, "employer_id")
        if truthy(group_id):
            group = await group_repo.get_group_with_participants(group_id)
        else:
            group = await group_repo.get_group_with_participants_by_job(job_stage_id)
        if truthy(group) and not truthy(employer_id):
            job_id = _g(group, "unique_name")
            participants = group.get("groupParticipantModel") or []
            group_user = next((gp for gp in participants if gp.get("user_id") == user_id), None)
            worker_info = next((gp for gp in participants if gp.get("role_sid") == "worker"), None)
            employer_info = next((gp for gp in participants if gp.get("role_sid") == "employer"), None)
            if not group_user:
                if user_type == "interpreter" and worker_info:
                    worker = await raw.select("SELECT id FROM workers WHERE user_id = :user_id LIMIT 1", {"user_id": worker_info["user_id"]})
                    worker_id = worker[0]["id"] if worker else None
                    if not worker_id:
                        return {"success": False, "data": [], "message": req.t("NOT_FOUND")}
                    interpreters = await raw.select(
                        INTERPRETER_SQL.format(job_col="job_stage_id", extra=""), {"worker_id": worker_id, "job_id": job_id}
                    )
                    for interpreter in interpreters:
                        payload = _participant(_trim_name(interpreter.get("first_name"), interpreter.get("last_name")), group["id"],
                                               interpreter["user_id"], "interpreter", user.get("id"), utcnow())
                        await _add_if_missing(group["id"], interpreter["user_id"], payload)
                elif user_type == "cs" and employer_info:
                    rows = await raw.select(
                        "SELECT e.id, e.cs_owner_id, s.last_name FROM employers e LEFT JOIN staffs s ON e.cs_owner_id = s.id "
                        "WHERE e.user_id = :user_id LIMIT 1",
                        {"user_id": employer_info["user_id"]},
                    )
                    if not rows:
                        return {"success": False, "data": [], "message": req.t("NOT_FOUND")}
                    cs_owner = rows[0]["cs_owner_id"]
                    await _add_if_missing(group["id"], cs_owner, _cs_payload(rows[0].get("last_name"), group["id"], cs_owner, "cs", user.get("id")))
            return {"success": True, "data": participants, "message": req.t("SUCCESS_CREATED")}
        rows = await raw.select(
            "SELECT e.id, e.cs_owner_id, s.last_name ,e.user_id as employer_user_id FROM employers e "
            "LEFT JOIN staffs s ON e.cs_owner_id = s.id WHERE e.id = :id LIMIT 1",
            {"id": employer_id},
        )
        if rows:
            cs_owner, last_name, employer_user_id = rows[0]["cs_owner_id"], rows[0].get("last_name"), rows[0]["employer_user_id"]
            groups_rows = await group_repo.get_chat_group_by_employer_id(employer_user_id)
            if isinstance(groups_rows, list) and groups_rows:
                for group in groups_rows:  # Node: forEach(async ...) — not awaited
                    spawn(_add_if_missing(group["id"], cs_owner, _cs_payload(last_name, group["id"], cs_owner, "cs", user.get("id"))))
            return {"success": True, "data": [], "message": req.t("SUCCESS_CREATED")}
        return None
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": str(error)}


async def create_conversation_line(req) -> dict[str, Any]:
    try:
        user = req.user_details
        platform, friend_id = _g(req.body, "platform"), _g(req.body, "friend_id")
        if platform == "action_required":
            duplicate = await group_repo.check_duplicate_group_with_platform({"unique_name": friend_id, "platform_two": platform})
        else:
            duplicate = await group_repo.check_duplicate_group_with_platform({"unique_name": friend_id, "platform": platform, "platform_two": platform})
        if not truthy(duplicate):
            return {"success": False, "data": [], "message": req.t("LINE_ACCOUNT_NOT_LINKED")}
        participant = await gp_repo.get_group_participant_details(
            {"group_id": duplicate.get("id"), "user_id": user.get("id")}, ["id", "identity", "group_id", "user_id", "role_id"]
        )
        if not truthy(participant):
            now = utcnow()
            await gp_repo.create_group_participant_insert({
                "identity": await get_user_identity(user, platform), "group_id": duplicate.get("id"), "user_id": user.get("id"),
                "is_host": 1, "role_id": user.get("role_id", UNDEFINED), "joined_on": now, "created_by": user.get("id"), "created_at": now,
            })
        return {"success": True, "data": duplicate, "message": req.t("SUCCESS_CREATED")}
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": str(error)}
