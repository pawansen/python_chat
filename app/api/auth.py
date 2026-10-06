"""HTTP authentication (port of src/middlewares/verify.token.middleware.js).

Authentication: Bearer JWT verified locally (HS256) -> user row -> active
status -> role (cached 60 s). Admin/sub-admin sessions idle >= 60 min are
expired (``users.login_expire = 1``, 400 LOGIN_EXPIRE_LOGIN_AGAIN) exactly like
Laravel's AdminAuthMiddleware; chatting refreshes ``last_activity_at`` at most
every 5 minutes (background, throttled).

Authorization (who may do what) stays in the services: participant checks,
staff-only /global endpoints, own-id scoping for end users.
"""

from __future__ import annotations

import re
import time
from typing import Any

from app.core.config import get_settings
from app.core.context import user_id_var
from app.core.errors import StatusError
from app.core.js import js_string, loose_eq, parse_date
from app.core.security import verify_access_token
from app.core.tasks import spawn
from app.repositories import people
from app.repositories import users as users_repo
from app.services.chat.common import utcnow

ROLE_CACHE_TTL_S = 60
SIXTY_MINUTES_S = 60 * 60
ACTIVITY_SYNC_THROTTLE_S = 5 * 60

_role_cache: dict[Any, tuple[Any, float]] = {}
_activity_sync: dict[str, float] = {}
_BEARER = re.compile(r"^Bearer ", re.IGNORECASE)


async def _role(role_id: Any) -> Any:
    now = time.monotonic()
    cached = _role_cache.get(role_id)
    if cached and cached[1] > now:
        return cached[0]
    role = await people.role_get_by_role_id(role_id, ["id", "guard_name"])
    if role:
        _role_cache[role_id] = (role, now + ROLE_CACHE_TTL_S)
    return role


def _schedule_activity_sync(user_id: Any) -> None:
    key = js_string(user_id)
    now = time.monotonic()
    last = _activity_sync.get(key)
    if last and now - last < ACTIVITY_SYNC_THROTTLE_S:
        return
    _activity_sync[key] = now
    if len(_activity_sync) > 10000:
        stale = now - ACTIVITY_SYNC_THROTTLE_S * 2
        for k in [k for k, t in _activity_sync.items() if t < stale]:
            _activity_sync.pop(k, None)

    async def sync() -> None:
        stamp = utcnow()
        result = await users_repo.update_details_by_user_id(
            user_id, {"login_expire": 0, "last_activity_at": stamp, "updated_at": stamp, "updated_by": user_id}
        )
        if isinstance(result, dict) and result.get("error"):
            _activity_sync.pop(key, None)
            print(f"Background admin activity sync failed for user {user_id}: {result['error']}", flush=True)

    spawn(sync())


async def verify_request_token(authorization: Any, translate) -> dict[str, Any]:
    if not authorization or not isinstance(authorization, str):
        raise StatusError.unauthorized(translate("TOKEN_INVALID"))
    token = _BEARER.sub("", authorization, count=1).strip() if _BEARER.match(authorization) else authorization.strip()
    if not token or len(token.split(".")) != 3:
        raise StatusError.unauthorized(translate("TOKEN_MALFORMED"))
    secret = get_settings().jwt.secret
    if not secret:
        raise StatusError.forbidden("JWT secret not configured")
    decoded = verify_access_token(token, secret)
    user = await users_repo.get_details_by_user_id(
        decoded.get("sub"),
        ["id", "status", "email", "mobile_number", "country_code", "role_id", "whatsapp_verified", "last_activity_at"],
    )
    if not user:
        raise StatusError.unauthorized(translate("TOKEN_INVALID"))
    if not loose_eq(user.get("status"), 1):
        raise StatusError.bad_request(translate("USER_NOT_ACTIVE"))
    role = await _role(user.get("role_id"))
    if not role:
        raise StatusError.bad_request(translate("USER_ROLE_INVALID"))
    last_activity = parse_date(user.get("last_activity_at")) if user.get("last_activity_at") else None
    if last_activity is not None and time.time() - last_activity.timestamp() >= SIXTY_MINUTES_S:
        stamp = utcnow()
        await users_repo.update_details_by_user_id(user["id"], {"login_expire": 1, "updated_at": stamp, "updated_by": user["id"]})
        raise StatusError.bad_request(translate("LOGIN_EXPIRE_LOGIN_AGAIN"))
    _schedule_activity_sync(user["id"])
    user_id_var.set(js_string(user["id"]))
    return {
        "id": user["id"],
        "email": user.get("email"),
        "mobile_number": user.get("mobile_number"),
        "country_code": user.get("country_code"),
        "role_id": user.get("role_id"),
        "role_guard_name": role.get("guard_name") if isinstance(role, dict) else None,
        "whatsapp_verified": user.get("whatsapp_verified"),
    }
