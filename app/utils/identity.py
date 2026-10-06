"""Identity/name helpers (ports of get.user.identity / get.friend.identity /
get.author.name / get.friend.details / get.unread.message.count utils)."""

from __future__ import annotations

from typing import Any

from app.core.js import UNDEFINED, js_string, truthy
from app.repositories import group_participant as gp_repo
from app.repositories import legacy_messages, people
from app.repositories import users as users_repo


def _g(obj: Any, key: str) -> Any:
    return obj.get(key, UNDEFINED) if isinstance(obj, dict) else UNDEFINED


async def get_user_identity(user_details: Any, platform: Any) -> str:
    identity = ""
    if platform == "whatsapp":
        mobile = _g(user_details, "mobile_number")
        if truthy(mobile) and mobile != "":
            identity = js_string(_g(user_details, "country_code")) + js_string(mobile)
    else:
        if truthy(_g(user_details, "country_code")) and truthy(_g(user_details, "mobile_number")):
            identity = js_string(user_details["country_code"]) + js_string(user_details["mobile_number"])
    return identity


async def get_friend_identity(user_details: Any, platform: Any) -> str:
    identity = ""
    if platform == "whatsapp":
        if truthy(_g(user_details, "whatsapp_number")) and _g(user_details, "whatsapp_country_code") != "":
            identity = js_string(_g(user_details, "whatsapp_country_code")) + js_string(user_details["whatsapp_number"])
    else:
        if truthy(_g(user_details, "mobile_number")):
            identity = user_details["mobile_number"]
    return identity


async def get_author_name(user_id: Any, role_guard_name: Any) -> str:
    if role_guard_name == "employer":
        details = await people.employer_get_details_by_user_id(user_id)
        return f"{js_string(_g(details, 'name'))} {js_string(_g(details, 'surname'))}" if truthy(details) else ""
    if role_guard_name == "worker":
        details = await people.worker_get_details_by_user_id(user_id)
        return f"{js_string(_g(details, 'name'))} {js_string(_g(details, 'surname'))}" if truthy(details) else ""
    details = await people.staff_get_details_by_user_id(user_id, ["id", "first_name", "last_name"])
    return f"{js_string(_g(details, 'first_name'))} {js_string(_g(details, 'last_name'))}" if truthy(details) else ""


async def get_friend_details(user_id: Any, conversation_sid: Any) -> dict[str, Any]:
    friend = await gp_repo.get_frind_participant_details(user_id, conversation_sid)
    user = await users_repo.get_details_by_user_id(_g(friend, "user_id"), ["id", "role_id"])
    role = await people.role_get_by_role_id(_g(user, "role_id"), ["id", "guard_name"])
    return {**(friend if isinstance(friend, dict) else {}), "guard_name": _g(role, "guard_name")}


async def get_unread_message_count(user_id: Any, conversation_sid: Any) -> int:
    count = 0
    rows = await legacy_messages.get_unread_message_count_user(user_id, conversation_sid)
    if isinstance(rows, list) and rows:
        count += sum((r.get("count") or 0) for r in rows)
    return count
