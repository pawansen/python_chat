"""users / user_data_view repository (port of src/repository/users.repository.js).

NOTE: functions that were NOT `async` in Node (getUserDevicesToken, getEmployerInfo,
getTemplateInfo, getUserInfoInViewByCustom, getAllUsers, getTemplateCategoryContent,
getUserStaffInfoInView, getUserInfoInViewByFilter) returned the query promise from
inside try/catch, so DB errors REJECTED to the caller; only the synchronous allowlist
checks became `{ error }`. The ports keep that split.

* ``user_data_view`` projections are cached under
  ``chat:user:detail:{id}:{sha1(attrs)}`` (6h TTL) and invalidated only by
  writes touching a cached column.
* LO-13 allowlists: attribute lists and dynamic column names are developer
  constants interpolated into SQL, so they are validated against strict
  patterns; values are always bound parameters.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.core.js import nullish
from app.core.jsonx import stringify
from app.core.tasks import spawn
from app.db import raw
from app.redis import cache_keys, cache_service
from app.repositories._sql import error

USER_DETAIL_TTL = 3600 * 6

CACHED_USER_COLUMNS = {
    "country_code",
    "mobile_number",
    "whatsapp_country_code",
    "whatsapp_number",
    "guard_name",
    "related_name",
    "profile_image",
    "role_id",
    "role_name",
    "email",
    "line_user_id",
    "staff_type",
    "employer_worker_staff_common_id",
}

SAFE_SQL_ATTRIBUTES = re.compile(r"^[A-Za-z0-9_,.()*\s]+$")
SAFE_SQL_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.]+$")

DEFAULT_VIEW_ATTRS = "CONCAT(country_code, mobile_number) as mobile_number,guard_name,related_name as full_name"


def assert_safe_sql_fragment(fragment: Any, label: str = "SQL fragment") -> str:
    if not isinstance(fragment, str) or not SAFE_SQL_ATTRIBUTES.match(fragment):
        raise ValueError(f"Unsafe {label} rejected")
    return fragment


def assert_safe_identifier(name: Any) -> str:
    if not isinstance(name, str) or not SAFE_SQL_IDENTIFIER.match(name):
        raise ValueError(f"Unsafe SQL identifier rejected: {name}")
    return name


def invalidate_user_detail_caches(user_id: Any, updates: dict[str, Any]) -> None:
    try:
        if nullish(user_id):
            return
        if not any(column in CACHED_USER_COLUMNS for column in (updates or {})):
            return
        cache_service.delete_pattern_bg(cache_keys.user_detail_pattern(str(user_id)))
    except Exception:  # noqa: BLE001
        pass


def _columns(attributes: list[Any]) -> str:
    return ", ".join(f"`{assert_safe_identifier(a)}`" for a in attributes)


async def get_details_by_user_id(user_id: Any, attributes: list[Any] | None = None) -> Any:
    """``usersModel.findOne({ where: { id }, attributes, raw: true })``."""
    try:
        return await raw.select_one(
            f"SELECT {_columns(attributes or ['id'])} FROM `users` AS `usersModel` WHERE `usersModel`.`id` = :id LIMIT 1",
            {"id": user_id},
        )
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def get_user_details(where: dict[str, Any], attributes: list[Any] | None = None) -> Any:
    try:
        params: dict[str, Any] = {}
        clauses = []
        for index, (column, value) in enumerate(where.items()):
            clauses.append(f"`{assert_safe_identifier(column)}` = :w{index}")
            params[f"w{index}"] = value
        return await raw.select_one(
            f"SELECT {_columns(attributes or ['id'])} FROM `users` WHERE {' AND '.join(clauses) or '1=1'} LIMIT 1", params
        )
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def update_details_by_user_id(user_id: Any, updates: dict[str, Any]) -> Any:
    try:
        params = {**updates, "user_id": user_id}
        set_clause = ", ".join(f"{assert_safe_identifier(k)} = :{k}" for k in updates)
        result = await raw.execute(f"UPDATE users SET {set_clause} WHERE id = :user_id", params)
        invalidate_user_detail_caches(user_id, updates)
        return result
    except Exception as exc:  # noqa: BLE001
        return error(exc)


def update_details_by_user_id_bg(user_id: Any, updates: dict[str, Any]) -> None:
    spawn(update_details_by_user_id(user_id, updates))


async def get_user_devices_token(user_id: Any) -> Any:
    try:
        return await raw.select(
            "SELECT u.id, u.device_token, u.role_id, u.notification_status, u.device_type,udv.guard_name, "
            "udv.related_name as full_name,udv.employer_worker_staff_common_id "
            "FROM users u INNER JOIN user_data_view udv ON u.id = udv.user_id WHERE u.id = :user_id",
            {"user_id": user_id},
        )
    except ValueError as exc:  # allowlist rejection (sync throw in Node -> { error })
        return error(exc)


async def get_user_devices_info_notification(user_id: Any) -> Any:
    try:
        return await raw.select(
            "SELECT u.device_token,u.device_type FROM user_devices u WHERE u.user_id = :user_id "
            "AND u.notification_status = 1 AND (u.logged_out_at IS NULL OR u.logged_out_at < NOW()) "
            "ORDER BY u.id DESC LIMIT 5",
            {"user_id": user_id},
        )
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def _read_user_detail_cache(key: str) -> list | None:
    cached = await cache_service.get(key)
    if not cached:
        return None
    try:
        parsed = json.loads(cached) if isinstance(cached, str) else cached
        return parsed if isinstance(parsed, list) else None
    except (TypeError, ValueError):
        return None


async def get_user_info_in_view(user_id: Any, attributes: str = DEFAULT_VIEW_ATTRS) -> Any:
    """Cached ``SELECT <attrs> FROM user_data_view WHERE user_id = ? LIMIT 1`` (row list)."""
    try:
        key = cache_keys.user_detail_key(user_id, cache_keys.cache_key_hash(attributes or ""))
        cached = await _read_user_detail_cache(key)
        if cached is not None:  # a cached [] is truthy in JS: negative results are served from cache
            return cached
        result = await raw.select(
            f"SELECT {assert_safe_sql_fragment(attributes, 'attributes')} from user_data_view where user_id = :user_id LIMIT 1",
            {"user_id": user_id},
        )
        cache_service_set_bg(key, stringify(result), USER_DETAIL_TTL)
        return result
    except Exception as exc:  # noqa: BLE001
        return error(exc)


def cache_service_set_bg(key: str, value: str, ttl: int) -> None:
    spawn(cache_service.set(key, value, ttl))


async def get_user_info_bulk(user_ids: list[Any], attributes: str) -> dict[Any, dict[str, Any]]:
    """Batch fetch (N+1 -> 1) keyed by user_id; shares the per-user cache entries."""
    try:
        if not user_ids:
            return {}
        unique_ids: list[Any] = []
        for uid in user_ids:
            if uid not in unique_ids:
                unique_ids.append(uid)
        user_map: dict[Any, dict[str, Any]] = {}
        attrs_hash = cache_keys.cache_key_hash(attributes or "")
        uncached = []
        for uid in unique_ids:
            parsed = await _read_user_detail_cache(cache_keys.user_detail_key(uid, attrs_hash))
            if parsed is not None:
                if len(parsed) > 0:
                    user_map[uid] = parsed[0]
            else:
                uncached.append(uid)
        if uncached:
            rows = await raw.select(
                f"SELECT user_id, {assert_safe_sql_fragment(attributes, 'attributes')} from user_data_view where user_id IN (:userIds)",
                {"userIds": uncached},
            )
            for row in rows:
                user_map[row["user_id"]] = row
                cache_service_set_bg(cache_keys.user_detail_key(row["user_id"], attrs_hash), stringify([row]), USER_DETAIL_TTL)
        return user_map
    except Exception:  # noqa: BLE001
        return {}


async def get_employer_info(employer_id: Any, attributes: str = "*") -> Any:
    try:
        return await raw.select(
            f"SELECT {assert_safe_sql_fragment(attributes, 'attributes')} from employers where id = :id LIMIT 1",
            {"id": employer_id},
        )
    except ValueError as exc:  # allowlist rejection (sync throw in Node -> { error })
        return error(exc)


async def get_template_info(slug: Any, attributes: str = "name_en") -> Any:
    try:
        return await raw.select(
            f"SELECT {assert_safe_sql_fragment(attributes, 'attributes')} from template_categories where slug = :slug LIMIT 1",
            {"slug": slug},
        )
    except ValueError as exc:  # allowlist rejection (sync throw in Node -> { error })
        return error(exc)


CUSTOM_LOOKUP_COLUMNS = ["line_user_id", "email"]


async def get_user_info_in_view_by_custom(column: str, value: Any, attributes: str = DEFAULT_VIEW_ATTRS) -> Any:
    try:
        if column not in CUSTOM_LOOKUP_COLUMNS:
            return {"error": f"Invalid lookup column: {column}"}
        return await raw.select(
            f"SELECT {assert_safe_sql_fragment(attributes, 'attributes')} from user_data_view where {column} = :value LIMIT 1",
            {"value": value},
        )
    except ValueError as exc:  # allowlist rejection (sync throw in Node -> { error })
        return error(exc)


async def get_all_users(attributes: str = DEFAULT_VIEW_ATTRS) -> Any:
    try:
        return await raw.select(
            f"SELECT {assert_safe_sql_fragment(attributes, 'attributes')} from user_data_view "
            "where whatsapp_number IS NOT NULL AND whatsapp_number != ''"
        )
    except ValueError as exc:  # allowlist rejection (sync throw in Node -> { error })
        return error(exc)


async def get_template_category_content(language: Any, action_module: Any, reason: Any) -> Any:
    """``reason === 2`` is a strict comparison on the Joi-converted number."""
    try:
        if action_module == "passport":
            slug = "passport_lost_expired_action_required"
        else:
            slug = "birth_certificate_dont_have_action_required"
            if isinstance(reason, int | float) and not isinstance(reason, bool) and reason == 2:
                slug = "birth_certificate_date_not_matched_action_required"
        return await raw.select(
            "SELECT tc.id, tc.module_key, tc.slug, tct.template_content,tct.id as template_content_id, "
            "tct.file_path as template_file_path FROM template_categories tc "
            "INNER JOIN template_contents tct ON tc.id = tct.category_id "
            "WHERE tc.module_key = 'worker' AND tc.slug = :slug LIMIT 1",
            {"slug": slug},
        )
    except ValueError as exc:  # allowlist rejection (sync throw in Node -> { error })
        return error(exc)


async def get_user_staff_info_in_view(user_id: Any, attributes: str = "whatsapp_country_code,whatsapp") -> Any:
    try:
        return await raw.select(
            f"SELECT {assert_safe_sql_fragment(attributes, 'attributes')} from staffs where user_id = :user_id LIMIT 1",
            {"user_id": user_id},
        )
    except ValueError as exc:  # allowlist rejection (sync throw in Node -> { error })
        return error(exc)


async def get_user_info_in_view_by_filter(where: dict[str, Any], attributes: str = DEFAULT_VIEW_ATTRS) -> Any:
    try:
        clauses = " AND ".join(f"{assert_safe_identifier(k)} = :{k}" for k in where)
        return await raw.select(
            f"SELECT {assert_safe_sql_fragment(attributes, 'attributes')} FROM user_data_view WHERE {clauses} LIMIT 1",
            dict(where),
        )
    except ValueError as exc:  # allowlist rejection (sync throw in Node -> { error })
        return error(exc)
