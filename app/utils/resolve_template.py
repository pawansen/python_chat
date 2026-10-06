"""DB-aware wrapper around :mod:`template_placeholder`
(port of src/utils/resolve.template.placeholder.util.js).

At most one query per needed source; the friend view reuses the cached
``get_user_info_in_view``. Any resolution error returns the original content
so sending never breaks.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import logger
from app.db import raw
from app.repositories import users as users_repo
from app.utils.template_placeholder import resolve_template_placeholders


async def _sender(user_id: Any) -> Any:
    rows = await raw.select(
        "SELECT s.first_name, u.country_code, u.mobile_number, u.email FROM `users` u "
        "LEFT JOIN `staffs` s ON s.user_id = u.id WHERE u.id = :user_id LIMIT 1",
        {"user_id": user_id},
    )
    return rows[0] if rows else None


async def _worker(user_id: Any) -> Any:
    rows = await raw.select(
        "SELECT workers.name, workers.surname, workers.whatsapp_country_code, workers.whatsapp_number, "
        "workers.hk_id, workers.passport_number, users.country_code, users.mobile_number, users.email "
        "FROM `workers` LEFT JOIN `users` ON workers.user_id = users.id WHERE workers.user_id = :user_id LIMIT 1",
        {"user_id": user_id},
    )
    return rows[0] if rows else None


async def _employer(user_id: Any) -> Any:
    rows = await raw.select(
        "SELECT employers.name, employers.surname, employers.chinese_name, employers.hk_id "
        "FROM `employers` WHERE employers.user_id = :user_id LIMIT 1",
        {"user_id": user_id},
    )
    return rows[0] if rows else None


async def _friend_view(user_id: Any) -> Any:
    rows = await users_repo.get_user_info_in_view(user_id, "user_id, related_name")
    return rows[0] if isinstance(rows, list) and rows else None


FETCHERS = {
    "fetchSenderProfile": _sender,
    "fetchWorkerProfile": _worker,
    "fetchEmployerProfile": _employer,
    "fetchFriendView": _friend_view,
}


async def resolve_template_content(params: dict[str, Any]) -> Any:
    template_content = params.get("template_content")
    if not isinstance(template_content, str) or "{{" not in template_content:
        return template_content
    try:
        return await resolve_template_placeholders(params, FETCHERS)
    except Exception as error:  # noqa: BLE001
        logger.warn("template placeholder resolution failed; sending content unresolved", error)
        return template_content
