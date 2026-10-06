"""/global services (port of aws.service.js: getDuplicateEmployers, getDuplicateWorkers,
getDuplicateEmployersCount, getDuplicateWorkersCount, getDuplicateJobsCount,
getDuplicateJobs, getWorkerPreviousImages, saveLanguageTranslate, getLanguageTranslate).

``require_staff`` runs outside the try (403 via the error handler). The Node
catch blocks of getDuplicateEmployers / getDuplicateWorkers / getDuplicateJobs
referenced block-scoped ``page``/``limit`` and therefore threw again
(ReferenceError -> 500 serverError); that outcome is preserved.
"""

from __future__ import annotations

import json
import math
from typing import Any

from app.core.config import get_settings
from app.core.js import UNDEFINED, js_string, number, parse_int, truthy
from app.core.jsonx import stringify
from app.db import raw
from app.redis import cache_keys, cache_service
from app.repositories import users as users_repo
from app.services.chat.common import require_staff, sha1


def _g(obj: Any, key: str, default: Any = UNDEFINED) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else default


def _paging(query: dict) -> tuple[Any, Any]:
    page = _g(query, "page")
    limit = _g(query, "limit")
    return (1 if page is UNDEFINED else page), (get_settings().chat_conversation_limit if limit is UNDEFINED else limit)


def _offset(page: Any, limit: Any) -> str:
    return js_string(parse_int((number(page) - 1) * number(limit)))


def _lower_full(name: Any, surname: Any) -> str:
    return (js_string(name) + " " + js_string(surname)).lower()


EMPLOYER_CURRENT = (
    "SELECT employers.alternate_mobile_verified,employers.alternate_mobile_number,employers.alternate_mobile_country_code,employers.name,"
    "employers.surname,employers.birth_date,employers.hk_id,employers.whatsapp_country_code,employers.whatsapp_number, users.mobile_number,"
    "users.email,users.country_code,users.mobile_verified,users.whatsapp_verified,users.social_type,users.social_id FROM `employers` "
    "LEFT JOIN `users` ON employers.user_id = users.id WHERE employers.user_id = :user_id LIMIT 1"
)
WORKER_CURRENT = (
    "SELECT workers.passport_number,workers.alternate_mobile_verified,workers.alternate_mobile_number,workers.alternate_mobile_country_code,"
    "workers.name,workers.surname,workers.date_of_birth,workers.hk_id,workers.whatsapp_country_code,workers.whatsapp_number, users.mobile_number,"
    "users.email,users.country_code,users.mobile_verified,users.whatsapp_verified,users.social_type,users.social_id FROM `workers` "
    "LEFT JOIN `users` ON workers.user_id = users.id WHERE workers.user_id = :user_id LIMIT 1"
)


def _employer_where(e: dict, *, list_variant: bool, count_variant: bool = False) -> tuple[list[str], dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if truthy(e.get("name")) or truthy(e.get("surname")):
        clauses.append("LOWER(CONCAT(employers.name, ' ', employers.surname)) = :fullname" if count_variant else "LOWER(CONCAT(name, ' ', surname)) = :fullname")
        params["fullname"] = _lower_full(e.get("name"), e.get("surname"))
    if count_variant:
        if truthy(e.get("birth_date")):
            clauses.append("employers.birth_date = :birth_date")
            params["birth_date"] = e["birth_date"]
    elif truthy(e.get("birth_date")) and truthy(e.get("surname")):
        clauses.append("(employers.birth_date = :birth_date AND employers.surname = :surname)")
        params["birth_date"] = e["birth_date"]
        params["surname"] = js_string(e["surname"]).strip()
    if truthy(e.get("hk_id")):
        clauses.append("employers.hk_id = :hk_id")
        params["hk_id"] = e["hk_id"]
    if e.get("social_type") == "facebook" and truthy(e.get("social_id")):
        clauses.append("users.social_id = :social_id")
        params["social_id"] = e["social_id"]
    if e.get("mobile_verified") == 1 and truthy(e.get("mobile_number")):
        clauses.append("(users.mobile_number = :mobile_number AND users.country_code = :country_code)")
        params["mobile_number"] = e["mobile_number"]
        params["country_code"] = e.get("country_code")
    if e.get("alternate_mobile_verified") == 1 and truthy(e.get("alternate_mobile_number")):
        clauses.append("(employers.alternate_mobile_number = :alternate_mobile_number AND employers.alternate_mobile_country_code = :alternate_mobile_country_code)")
        params["alternate_mobile_number"] = e["alternate_mobile_number"]
        params["alternate_mobile_country_code"] = e.get("alternate_mobile_country_code")
    if e.get("whatsapp_verified") == 1 and truthy(e.get("whatsapp_country_code")) and truthy(e.get("whatsapp_number")):
        clauses.append("(employers.whatsapp_country_code = :whatsapp_country_code AND employers.whatsapp_number = :whatsapp_number)")
        params["whatsapp_country_code"] = e["whatsapp_country_code"]
        params["whatsapp_number"] = e["whatsapp_number"]
    return clauses, params


def _worker_where(w: dict, *, count_variant: bool = False) -> tuple[list[str], dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if truthy(w.get("name")) or truthy(w.get("surname")):
        clauses.append("LOWER(CONCAT(workers.name, ' ', workers.surname)) = :fullname")
        full = _lower_full(w.get("name"), w.get("surname"))
        params["fullname"] = full if count_variant else full.strip()
    if truthy(w.get("date_of_birth")) and truthy(w.get("surname")):
        clauses.append("(workers.date_of_birth = :date_of_birth AND workers.surname = :surname)")
        params["date_of_birth"] = w["date_of_birth"]
        params["surname"] = w["surname"] if count_variant else js_string(w["surname"]).strip()
    if truthy(w.get("passport_number")):
        clauses.append("workers.passport_number = :passport_number")
        params["passport_number"] = w["passport_number"]
    if truthy(w.get("hk_id")):
        clauses.append("workers.hk_id = :hk_id")
        params["hk_id"] = w["hk_id"]
    if w.get("social_type") == "facebook" and truthy(w.get("social_id")):
        clauses.append("users.social_id = :social_id")
        params["social_id"] = w["social_id"]
    if truthy(w.get("mobile_number")):
        clauses.append("(users.mobile_number = :mobile_number AND users.country_code = :country_code)")
        params["mobile_number"] = w["mobile_number"]
        params["country_code"] = w.get("country_code")
    if truthy(w.get("alternate_mobile_number")):
        clauses.append("(workers.alternate_mobile_number = :alternate_mobile_number AND workers.alternate_mobile_country_code = :alternate_mobile_country_code)")
        params["alternate_mobile_number"] = w["alternate_mobile_number"]
        params["alternate_mobile_country_code"] = w.get("alternate_mobile_country_code")
    if w.get("whatsapp_verified") == 1 and truthy(w.get("whatsapp_country_code")) and truthy(w.get("whatsapp_number")):
        clauses.append("(workers.whatsapp_country_code = :whatsapp_country_code AND workers.whatsapp_number = :whatsapp_number)")
        params["whatsapp_country_code"] = w["whatsapp_country_code"]
        params["whatsapp_number"] = w["whatsapp_number"]
    return clauses, params


def _where_sql(clauses: list[str]) -> str:
    return " AND (" + " OR ".join(clauses) + ")" if clauses else ""


class _ReThrow(Exception):
    """Node's catch block threw (ReferenceError on page/limit) -> next(error) -> 500."""


async def get_duplicate_employers(req) -> dict[str, Any]:
    require_staff(req)
    try:
        query = req.query
        user_id = _g(query, "user_id")
        page, limit = _paging(query)
        offset = _offset(page, limit)
        key = cache_keys.duplicate_key("employers", sha1(f"u{js_string(user_id)}:p{js_string(page)}:l{js_string(limit)}:lang:{req.get_locale()}"))
        cached = await cache_service.get(key)
        if cached:
            return json.loads(cached)
        rows = await raw.select(EMPLOYER_CURRENT, {"user_id": user_id})
        if not rows:
            return {"success": False, "data": [], "message": "Employer not found", "current_page": 0, "limit": 0}
        e = rows[0]
        clauses, params = _employer_where(e, list_variant=True)
        where = _where_sql(clauses)
        params["user_id"] = user_id
        count_rows = await raw.select(
            "SELECT COUNT(*) as count FROM `employers` LEFT JOIN `users` ON employers.user_id = users.id  WHERE employers.user_id != :user_id " + where,
            params,
        )
        total = count_rows[0]["count"] if count_rows else 0
        if total == 0:
            result = {"success": False, "data": [], "current_page": page, "per_page": limit, "total": 0, "last_updated": "", "last_page": 0}
            return {"data": result, "success": False, "message": req.t("NOT_FOUND")}
        items = await raw.select(
            "SELECT employers.id,(SELECT CONCAT(staffs.first_name, ' ', staffs.last_name) FROM staffs WHERE employers.cs_owner_id = staffs.id) as cs_name, "
            "(SELECT JSON_OBJECT('id', contracts.id, 'contract_number', contracts.contract_number, 'status', contracts.status, 'display_status', "
            "(SELECT name_en FROM contract_invoices_statuses WHERE contract_invoices_statuses.id = contracts.status)) FROM contracts "
            "WHERE contracts.employer_id = employers.id ORDER BY contracts.id DESC LIMIT 1) as latest_contract, employers.chinese_name, employers.reference_id, "
            "(SELECT status FROM jobs WHERE jobs.id = employers.last_opened_jo_id) as latest_jo_status, (SELECT reference_id FROM jobs WHERE jobs.id = employers.last_opened_jo_id) as latest_jo, "
            "employers.alternate_mobile_verified, employers.alternate_mobile_number, employers.alternate_mobile_country_code, employers.id as employer_id, "
            "employers.user_id, employers.name, employers.surname, employers.birth_date, employers.hk_id, employers.whatsapp_country_code, employers.whatsapp_number, "
            "users.mobile_number, users.email, users.country_code, users.mobile_verified, users.whatsapp_verified, users.social_type, users.social_id FROM `employers` "
            "LEFT JOIN `users` ON employers.user_id = users.id  WHERE employers.user_id != :user_id " + where + f" LIMIT {offset}, {js_string(limit)}",
            params,
        )
        out = []
        for item in items:
            matched = []
            if truthy(e.get("name")) and truthy(e.get("surname")) and _lower_full(e["name"], e["surname"]) == _lower_full(item.get("name"), item.get("surname")):
                matched.append("Given Name + Surname")
            if (truthy(e.get("birth_date")) and e.get("birth_date") == item.get("birth_date") and truthy(e.get("surname"))
                    and js_string(e["surname"]).lower().strip() == js_string(item.get("surname") or "").lower().strip()):
                matched.append("Surname + Date of Birth")
            if truthy(e.get("hk_id")) and e.get("hk_id") == item.get("hk_id"):
                matched.append("HKID")
            if e.get("social_type") == "facebook" and truthy(e.get("social_id")) and e.get("social_id") == item.get("social_id"):
                matched.append("Social ID")
            if e.get("mobile_verified") == 1 and truthy(e.get("mobile_number")) and e.get("mobile_number") == item.get("mobile_number") and e.get("country_code") == item.get("country_code"):
                matched.append("Primary Phone")
            if (e.get("alternate_mobile_verified") == 1 and truthy(e.get("alternate_mobile_number"))
                    and e.get("alternate_mobile_number") == item.get("alternate_mobile_number")
                    and e.get("alternate_mobile_country_code") == item.get("alternate_mobile_country_code")):
                matched.append("Secondary Phone")
            if (e.get("whatsapp_verified") == 1 and truthy(e.get("whatsapp_country_code")) and truthy(e.get("whatsapp_number"))
                    and e.get("whatsapp_country_code") == item.get("whatsapp_country_code") and e.get("whatsapp_number") == item.get("whatsapp_number")):
                matched.append("WhatsApp Number")
            out.append({**item, "matched_columns": ", ".join(matched)})
        result = {
            "success": False, "current_page": page, "per_page": limit, "total": total,
            "last_page": math.ceil(total / number(limit)) if number(limit) else None, "last_updated": "",
            "data": out, "employer_ids": ",".join(js_string(i["id"]) for i in out), "matched_columns": [],
        }
        response = {"data": result, "success": True, "message": req.t("SUCCESS_LISTED")}
        await cache_service.set(key, stringify(response), get_settings().cache_ttl_duplicate)
        return response
    except Exception as error:  # noqa: BLE001
        raise _ReThrow("page is not defined") from error


WORKER_STATUS = {
    1: "Incomplete", 2: "In Review", 3: "Available", 4: "Employed", 5: "Suspended", 6: "Banned", 7: "Hold", 9: "Partner Pending",
}


async def get_duplicate_workers(req) -> dict[str, Any]:
    require_staff(req)
    try:
        query = req.query
        user_id = _g(query, "user_id")
        page, limit = _paging(query)
        offset = _offset(page, limit)
        key = cache_keys.duplicate_key("workers", sha1(f"u{js_string(user_id)}:p{js_string(page)}:l{js_string(limit)}:lang:{req.get_locale()}"))
        cached = await cache_service.get(key)
        if cached:
            return json.loads(cached)

        def not_found() -> dict[str, Any]:
            return {"data": {"success": False, "data": [], "current_page": page, "message": req.t("NOT_FOUND"), "limit": limit,
                             "total": 0, "total_pages": 0, "last_updated": "", "matched_columns": []}, "success": False}

        rows = await raw.select(WORKER_CURRENT, {"user_id": user_id})
        if not rows:
            return not_found()
        w = rows[0]
        clauses, params = _worker_where(w)
        where = _where_sql(clauses)
        params["user_id"] = user_id
        count_rows = await raw.select(
            "SELECT COUNT(*) as count FROM `workers` LEFT JOIN `users` ON workers.user_id = users.id  WHERE workers.user_id != :user_id " + where, params
        )
        total = count_rows[0]["count"] if count_rows else 0
        if total == 0:
            return {"data": {"success": False, "data": [], "current_page": page, "message": req.t("NOT_FOUND"), "per_page": limit,
                             "total": 0, "total_pages": 0, "last_updated": "", "matched_columns": []}, "success": False}
        items = await raw.select(
            "SELECT workers.id,workers.status, workers.passport_number,workers.reference_id,workers.last_profile_updated_at,workers.alternate_mobile_country_code,"
            "workers.id as employer_id,workers.user_id,workers.name,workers.surname,workers.date_of_birth,workers.hk_id,workers.whatsapp_country_code,"
            "workers.whatsapp_number, users.mobile_number,users.email,users.country_code,users.mobile_verified,users.whatsapp_verified,users.social_type,"
            "users.social_id FROM `workers` LEFT JOIN `users` ON workers.user_id = users.id  WHERE workers.user_id != :user_id " + where
            + f" LIMIT {offset}, {js_string(limit)}",
            params,
        )
        out = []
        for item in items:
            item = {**item, "fullname": f"{js_string(item['name']) if truthy(item.get('name')) else ''} {js_string(item['surname']) if truthy(item.get('surname')) else ''}",
                    "worker_status": {"id": item.get("status"), "worker_status": WORKER_STATUS.get(item.get("status"), "")}}
            matched = []
            reason = ""
            if truthy(w.get("name")) and truthy(w.get("surname")) and _lower_full(w["name"], w["surname"]) == _lower_full(item.get("name"), item.get("surname")):
                matched.append("Given Name + Surname")
            if (truthy(w.get("date_of_birth")) and w.get("date_of_birth") == item.get("date_of_birth") and truthy(w.get("surname"))
                    and js_string(w["surname"]).lower().strip() == js_string(item.get("surname") or "").lower().strip()):
                matched.append("Surname + Date of Birth")
            if truthy(w.get("passport_number")) and w.get("passport_number") == item.get("passport_number"):
                matched.append("Passport Number")
            if truthy(w.get("hk_id")) and w.get("hk_id") == item.get("hk_id"):
                matched.append("HKID")
            if w.get("social_type") == "facebook" and truthy(w.get("social_id")) and w.get("social_id") == item.get("social_id"):
                matched.append("Social ID")
            if truthy(w.get("mobile_number")) and w.get("mobile_number") == item.get("mobile_number") and w.get("country_code") == item.get("country_code"):
                matched.append("Primary Phone")
            if (truthy(w.get("alternate_mobile_number")) and w.get("alternate_mobile_number") == item.get("alternate_mobile_number")
                    and w.get("alternate_mobile_country_code") == item.get("alternate_mobile_country_code")):
                matched.append("Secondary Phone")
            if (w.get("whatsapp_verified") == 1 and truthy(w.get("whatsapp_country_code")) and truthy(w.get("whatsapp_number"))
                    and w.get("whatsapp_country_code") == item.get("whatsapp_country_code") and w.get("whatsapp_number") == item.get("whatsapp_number")):
                matched.append("WhatsApp Number")
            reason = matched[0] + " · exact match" if len(matched) == 1 else " + ".join(matched) + " · exact match"
            out.append({**item, "matched_params": ", ".join(matched), "match_reason": reason})
        result = {"success": True, "current_page": page, "message": req.t("SUCCESS_LISTED"), "per_page": limit, "total": total,
                  "total_pages": math.ceil(total / number(limit)) if number(limit) else None, "last_updated": "", "data": out, "matched_columns": []}
        response = {"data": result, "success": True}
        await cache_service.set(key, stringify(response), get_settings().cache_ttl_duplicate)
        return response
    except Exception as error:  # noqa: BLE001
        raise _ReThrow("page is not defined") from error


async def get_duplicate_employers_count(req) -> dict[str, Any]:
    require_staff(req)
    try:
        user_id = _g(req.query, "user_id")
        key = cache_keys.duplicate_key("employers-count", sha1(f"u{js_string(user_id)}"))
        cached = await cache_service.get(key)
        if cached:
            return json.loads(cached)
        rows = await raw.select(EMPLOYER_CURRENT, {"user_id": user_id})
        if not rows:
            return {"success": False, "data": [], "message": req.t("NOT_FOUND"), "current_page": 0, "limit": 0}
        clauses, params = _employer_where(rows[0], list_variant=False, count_variant=True)
        where = _where_sql(clauses)
        if where == "":
            return {"success": False, "data": [], "message": req.t("NOT_FOUND"), "current_page": 0, "limit": 0}
        params["user_id"] = user_id
        result_rows = await raw.select(
            "SELECT COUNT(*) as count FROM `employers` LEFT JOIN `users` ON employers.user_id = users.id  WHERE employers.user_id != :user_id " + where, params
        )
        result = {"current_page": 0, "limit": 0, "data": dict(result_rows[0]) if result_rows else {}}
        await cache_service.set(key, stringify(result), get_settings().cache_ttl_duplicate)
        return result
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": str(error)}


async def get_duplicate_workers_count(req) -> dict[str, Any]:
    require_staff(req)
    try:
        user_id = _g(req.query, "user_id")
        key = cache_keys.duplicate_key("workers-count", sha1(f"u{js_string(user_id)}"))
        cached = await cache_service.get(key)
        if cached:
            return json.loads(cached)
        rows = await raw.select(WORKER_CURRENT, {"user_id": user_id})
        if not rows:
            return {"success": False, "data": [], "message": req.t("NOT_FOUND"), "current_page": 0, "limit": 0}
        clauses, params = _worker_where(rows[0], count_variant=True)
        where = _where_sql(clauses)
        if where == "":
            return {"success": False, "data": [], "message": req.t("NOT_FOUND"), "current_page": 0, "limit": 0}
        params["user_id"] = user_id
        result_rows = await raw.select(
            "SELECT COUNT(*) as count FROM `workers` LEFT JOIN `users` ON workers.user_id = users.id  WHERE workers.user_id != :user_id " + where, params
        )
        result = {"current_page": 0, "limit": 0, "data": dict(result_rows[0]) if result_rows else {}}
        await cache_service.set(key, stringify(result), get_settings().cache_ttl_duplicate)
        return result
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": str(error)}


def _employer_ids(employer_id: Any) -> str:
    if not isinstance(employer_id, str):
        raise TypeError("employer_id.split is not a function")
    return ",".join(js_string(number(part.strip())) for part in employer_id.split(","))


async def get_duplicate_jobs_count(req) -> dict[str, Any]:
    require_staff(req)
    try:
        employer_id = _g(req.query, "employer_id")
        key = cache_keys.duplicate_key("jobs-count", sha1(f"e{js_string(employer_id)}"))
        cached = await cache_service.get(key)
        if cached:
            return json.loads(cached)
        rows = await raw.select(
            f"SELECT COUNT(*) as count FROM jobs WHERE jobs.status <> 9 AND jobs.employer_id IN ({_employer_ids(employer_id)}) LIMIT 1"
        )
        result = {"current_page": 0, "limit": 0, "data": dict(rows[0]) if rows else {}}
        await cache_service.set(key, stringify(result), get_settings().cache_ttl_duplicate)
        return result
    except Exception as error:  # noqa: BLE001
        return {"success": False, "data": [], "message": str(error)}


async def get_duplicate_jobs(req) -> dict[str, Any]:
    require_staff(req)
    try:
        query = req.query
        employer_id = _g(query, "employer_id")
        page, limit = _paging(query)
        offset = _offset(page, limit)
        key = cache_keys.duplicate_key("jobs", sha1(f"e{js_string(employer_id)}:p{js_string(page)}:l{js_string(limit)}:lang:{req.get_locale()}"))
        cached = await cache_service.get(key)
        if cached:
            return json.loads(cached)
        ids = _employer_ids(employer_id)
        count_rows = await raw.select(f"SELECT COUNT(*) as count FROM jobs WHERE jobs.status <> 9 AND jobs.employer_id IN ({ids}) LIMIT 1")
        total = count_rows[0]["count"] if count_rows else 0
        if total == 0:
            result = {"success": False, "data": [], "current_page": page, "per_page": limit, "total": 0, "last_updated": "", "last_page": 0}
            return {"data": result, "success": False, "message": req.t("NOT_FOUND")}
        items = await raw.select(
            "SELECT jobs.id, jobs.reference_id, jobs.status, jobs.flexible_start_date, jobs.start_date, jobs.device_type, jobs.employer_id, jobs.created_at, "
            "jobs.cs_change_reason, jobs.cs_change_reason_updated_at, employers.name AS employer_name, employers.surname AS employer_surname, "
            "employers.location, employers.office_location, employers.cs_owner_id, employers.reference_id AS employer_reference_id, "
            "users.email AS employer_email, staffs.user_id AS cs_owner_user_id, GREATEST(DATEDIFF(CURDATE(), jobs.expiry_date), 0) AS days_open, "
            "CONCAT(employers.surname, ' ', employers.name) AS employer_fullname, (SELECT COUNT(*) FROM job_views WHERE job_views.job_id = jobs.id) AS view_count, "
            "(SELECT CONCAT(staffs.first_name, ' ', staffs.last_name) FROM staffs WHERE (SELECT employers.cs_owner_id FROM employers WHERE jobs.employer_id = employers.id) = staffs.id) AS cs_name, "
            "(SELECT job_stages.updated_at FROM job_stages WHERE job_stages.job_id = jobs.id ORDER BY job_stages.updated_at DESC LIMIT 1) AS job_stage_last_updated, "
            "(CASE WHEN (SELECT COUNT(*) FROM job_stages_logs WHERE jobs.id = job_stages_logs.job_id AND job_stages_logs.status = 1) > 0 "
            "THEN ROUND((SELECT COUNT(*) FROM job_stages_logs WHERE jobs.id = job_stages_logs.job_id AND job_stages_logs.status = 2) * 100.0 / "
            "(SELECT COUNT(*) FROM job_stages_logs WHERE jobs.id = job_stages_logs.job_id AND job_stages_logs.status = 1), 2) ELSE 0 END) AS apply_percentage "
            "FROM jobs INNER JOIN employers ON employers.id = jobs.employer_id LEFT JOIN staffs ON employers.cs_owner_id = staffs.id "
            f"INNER JOIN users ON employers.user_id = users.id WHERE jobs.status <> 9 AND jobs.employer_id IN ({ids}) LIMIT  {offset} , {js_string(limit)}"
        )
        result = {"success": False, "current_page": page, "per_page": limit, "total": total,
                  "last_page": math.ceil(total / number(limit)) if number(limit) else None, "last_updated": "", "data": items}
        response = {"data": result, "success": True, "message": req.t("SUCCESS_LISTED")}
        await cache_service.set(key, stringify(response), get_settings().cache_ttl_duplicate)
        return response
    except Exception as error:  # noqa: BLE001
        raise _ReThrow("page is not defined") from error


async def get_worker_previous_images(req) -> dict[str, Any]:
    try:
        rows = await raw.select(
            "SELECT id,user_id,uploaded,uploaded_without_bg,uploaded_crop,final_image,type,is_default,created_at FROM user_images "
            "WHERE user_images.user_id = :user_id AND type='profile' AND final_image != '' ORDER BY user_images.created_at DESC LIMIT 10",
            {"user_id": _g(req.query, "user_id")},
        )
        return {"success": True, "message": req.t("SUCCESS_LISTED"), "fileUrl": get_settings().aws_s3_base_url, "data": rows}
    except Exception as error:  # noqa: BLE001
        return {"success": False, "message": str(error) or req.t("ERROR_OCCURRED"), "data": []}


async def save_language_translate(req) -> dict[str, Any]:
    await users_repo.update_details_by_user_id(
        req.user_details["id"],
        {"chat_translate_language": _g(req.body, "chat_translate_language"), "chat_translate_view": _g(req.body, "chat_translate_view")},
    )
    return {"success": True, "data": [], "message": req.t("LANGUAGE_PREFERENCE_SAVED_SUCCESS")}


async def get_language_translate(req) -> dict[str, Any]:
    info = await users_repo.get_details_by_user_id(req.user_details["id"], ["chat_translate_language", "chat_translate_view"])
    return {"success": True, "data": info, "message": req.t("SUCCESS")}
