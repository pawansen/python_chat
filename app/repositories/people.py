"""employers / workers / staffs / roles repositories
(ports of employer.repository.js, worker.repository.js, staffs.repository.js,
role.repository.js).
"""

from __future__ import annotations

from typing import Any

from app.db import raw
from app.repositories._sql import attrs_sql, error, where_sql


async def employer_get_details_by_user_id(employer_user_id: Any) -> Any:
    """employersModel.findOne + REQUIRED users include (flattened columns)."""
    try:
        return await raw.select_one(
            "SELECT `employersModel`.`id`, `employersModel`.`whatsapp_number`, `employersModel`.`whatsapp_country_code`, "
            "`usersModel`.`mobile_number` AS `mobile_number`, `usersModel`.`whatsapp_verified` AS `whatsapp_verified`, "
            "`usersModel`.`country_code` AS `country_code`, `usersModel`.`email` AS `email`, "
            "`usersModel`.`role_id` AS `role_id`, `employersModel`.`name`, `employersModel`.`surname` "
            "FROM `employers` AS `employersModel` "
            "INNER JOIN `users` AS `usersModel` ON `employersModel`.`user_id` = `usersModel`.`id` "
            "WHERE `employersModel`.`user_id` = :u LIMIT 1",
            {"u": employer_user_id},
        )
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def employer_get_info(where: dict[str, Any], attributes: list[Any]) -> Any:
    try:
        params: dict[str, Any] = {}
        return await raw.select_one(
            f"SELECT {attrs_sql(attributes)} FROM `employers` WHERE {where_sql(where, params)} LIMIT 1", params
        )
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def worker_get_details_by_user_id(worker_user_id: Any) -> Any:
    try:
        return await raw.select_one(
            "SELECT `workersModel`.`id`, `workersModel`.`whatsapp_number`, `workersModel`.`whatsapp_country_code`, "
            "`usersModel`.`mobile_number` AS `mobile_number`, `usersModel`.`country_code` AS `country_code`, "
            "`usersModel`.`email` AS `email`, `usersModel`.`role_id` AS `role_id`, "
            "`workersModel`.`name`, `workersModel`.`surname`, `workersModel`.`user_id` "
            "FROM `workers` AS `workersModel` "
            "INNER JOIN `users` AS `usersModel` ON `workersModel`.`user_id` = `usersModel`.`id` "
            "WHERE `workersModel`.`user_id` = :u LIMIT 1",
            {"u": worker_user_id},
        )
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def staff_get_details_by_user_id(user_id: Any, attributes: list[Any]) -> Any:
    try:
        return await raw.select_one(
            f"SELECT {attrs_sql(attributes)} FROM `staffs` WHERE `user_id` = :u LIMIT 1", {"u": user_id}
        )
    except Exception as exc:  # noqa: BLE001
        return error(exc)


async def role_get_by_role_id(role_id: Any, attributes: list[Any] | None = None) -> Any:
    try:
        return await raw.select_one(
            f"SELECT {attrs_sql(attributes or ['id'])} FROM `roles` WHERE `id` = :id LIMIT 1", {"id": role_id}
        )
    except Exception as exc:  # noqa: BLE001
        print(str(exc), flush=True)
        return error(exc)
