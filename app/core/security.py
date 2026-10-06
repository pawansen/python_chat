"""Authentication primitives (ports of verify.access.token.util.js,
verify.apikey.middleware.js and socket.auth.util.js).

JWT verification mirrors ``jsonwebtoken.verify`` as configured in Node:

* algorithm pinned to HS256 (blocks ``alg: none`` / algorithm confusion);
* 10 s clock tolerance on ``exp``/``nbf``;
* issuer/audience enforced only when ACCESS_TOKEN_ISSUER / _AUDIENCE are set
  (current Laravel tymon tokens carry neither);
* claims are NOT type-policed beyond that — tymon tokens carry an integer
  ``sub`` that PyJWT would otherwise reject, and ``jsonwebtoken`` never
  validated ``iat``.

Authentication answers "who is the caller"; authorization (participant
checks, staff-only endpoints, own-room joins) lives in the services.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any
from urllib.parse import parse_qs

import jwt

from app.core.config import get_settings
from app.core.errors import ForbiddenEmptyMessage

_BEARER = re.compile(r"^Bearer\s+", re.IGNORECASE)

_DECODE_OPTIONS = {
    "verify_signature": True,
    "verify_exp": True,
    "verify_nbf": True,
    "verify_iat": False,
    "verify_sub": False,
    "verify_jti": False,
}


def _decode(token: str, secret: str, *, issuer: str = "", audience: str = "") -> dict[str, Any]:
    if not secret:
        raise jwt.InvalidKeyError("secret or public key must be provided")
    options = dict(_DECODE_OPTIONS)
    options["verify_aud"] = bool(audience)
    options["verify_iss"] = bool(issuer)
    kwargs: dict[str, Any] = {"algorithms": ["HS256"], "leeway": 10, "options": options}
    if audience:
        kwargs["audience"] = audience
    if issuer:
        kwargs["issuer"] = issuer
    return jwt.decode(token, secret, **kwargs)


def verify_access_token(token: str, secret: str) -> dict[str, Any]:
    """HTTP path: raises 403 (``StatusError.forbidden("")``) on any failure."""
    try:
        return _decode(token, secret)
    except Exception as error:  # noqa: BLE001
        print(f"token verification failed: {error}", flush=True)
        raise ForbiddenEmptyMessage() from error


def verify_token(token: str, secret: str) -> dict[str, Any] | None:
    """Socket path (HI-10): strong verification incl. optional iss/aud; None on failure."""
    jwt_cfg = get_settings().jwt
    try:
        return _decode(token, secret, issuer=jwt_cfg.issuer, audience=jwt_cfg.audience)
    except Exception as error:  # noqa: BLE001
        print(f"token verification failed: {error}", flush=True)
        return None


def is_api_key_valid(expected: Any, provided: Any) -> bool:
    """Constant-time compare of fixed-length SHA-256 digests (never throws on length mismatch)."""
    expected_hash = hashlib.sha256(("" if expected is None else str(expected)).encode()).digest()
    provided_hash = hashlib.sha256(("" if provided is None else str(provided)).encode()).digest()
    return hmac.compare_digest(expected_hash, provided_hash)


async def is_token_revoked(decoded: dict[str, Any] | None) -> bool:
    """``jwt:blocklist:{jti}`` (written by the token issuer). Infra errors never block."""
    jti = (decoded or {}).get("jti")
    if not jti:
        return False
    try:
        from app.redis import cache_keys
        from app.redis import client as redis_client

        client = await redis_client.connect()
        if client is None:
            return False
        exists = await client.exists(cache_keys.jwt_blocklist_key(get_settings().socket.jwt_blocklist_prefix, jti))
        return exists == 1
    except Exception as error:  # noqa: BLE001
        print(f"isTokenRevoked error: {error}", flush=True)
        return False


SOCKET_USER_FIELDS = (
    "CONCAT(country_code, mobile_number) as mobile_number,guard_name,related_name as full_name,"
    "guard_name,profile_image,user_id, user_id as id"
)


async def authenticate_socket_connection(token: str | None) -> dict[str, Any] | None:
    """Handshake auth: signature/expiry (+iss/aud) + revocation + user_data_view row."""
    if not token:
        return None
    decoded = verify_token(token, get_settings().jwt.secret)
    if not decoded:
        return None
    if await is_token_revoked(decoded):
        return None
    from app.repositories import users as users_repo

    rows = await users_repo.get_user_info_in_view(decoded.get("sub"), SOCKET_USER_FIELDS)
    if isinstance(rows, list) and rows:
        return dict(rows[0])
    return None


def extract_handshake_token(auth: Any, environ: dict[str, Any]) -> str:
    """auth.token (v3/v4) -> ?token= (v2 query form) -> Authorization / token header."""
    token: Any = ""
    if isinstance(auth, dict) and auth.get("token"):
        token = auth.get("token")
    if not token:
        query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        values = query.get("token")
        if values and values[0]:
            token = values[0]
    if not token:
        token = environ.get("HTTP_AUTHORIZATION") or environ.get("HTTP_TOKEN") or ""
    if isinstance(token, str) and _BEARER.match(token):
        token = _BEARER.sub("", token, count=1).strip()
    return token.strip() if isinstance(token, str) else ""
