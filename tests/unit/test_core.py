"""Core helpers: JS semantics, JSON.stringify parity, i18n, JWT/API-key auth primitives."""

from __future__ import annotations

import math
import time
from datetime import UTC, date, datetime
from decimal import Decimal

import jwt
import pytest

from app.core import i18n
from app.core.config import RawEnv, build_settings
from app.core.errors import StatusError, error_response_body
from app.core.js import UNDEFINED, js_string, loose_eq, number, parse_int, room, strip_html, strip_phone, truthy
from app.core.jsonx import stringify
from app.core.security import extract_handshake_token, is_api_key_valid, verify_access_token, verify_token


@pytest.mark.parametrize(
    ("value", "expected"),
    [("12", 12), ("12abc", 12), ("  -7x", -7), ("abc", None), (1.9, 1), (-1.9, -1), (1e21, 1), (None, None), (True, None), ("", None)],
)
def test_parse_int(value, expected):
    result = parse_int(value)
    assert (math.isnan(result) if expected is None else result == expected)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("", 0), (" 12 ", 12), ("1e3", 1000), ("0x10", 16), ("12abc", None), (None, 0), (UNDEFINED, None), (True, 1), ([], 0), ([5], 5)],
)
def test_number(value, expected):
    result = number(value)
    assert (math.isnan(result) if expected is None else result == expected)


def test_truthiness_and_strings():
    assert truthy([]) and truthy({}) and truthy("0")
    assert not truthy("") and not truthy(0) and not truthy(None) and not truthy(UNDEFINED) and not truthy(float("nan"))
    assert js_string(True) == "true" and js_string(None) == "null" and js_string(UNDEFINED) == "undefined"
    assert js_string(5.0) == "5" and js_string(float("nan")) == "NaN" and js_string(1e21) == "1e+21"
    assert room(12) == "12" and room(parse_int("x")) == "NaN"
    assert loose_eq(5, "5") and not loose_eq(5, "6") and loose_eq(None, UNDEFINED)


def test_phone_and_html_sanitisers():
    assert strip_phone("+852 (9123)-45.67") == "8529123" + "4567"
    assert strip_html("<p>Hi</p>  <b>x</b>") == " Hi x "


def test_stringify_matches_json_stringify():
    payload = {
        "a": 1.0, "b": float("nan"), "c": UNDEFINED, "d": [UNDEFINED, 2], "e": datetime(2026, 1, 2, 3, 4, 5, 678900, tzinfo=UTC),
        "f": Decimal("3"), "g": Decimal("2.5"), "h": "陳/</script>", "i": date(2026, 1, 2), "j": None,
    }
    assert stringify(payload) == (
        '{"a":1,"b":null,"d":[null,2],"e":"2026-01-02T03:04:05.678Z","f":3,"g":2.5,"h":"陳/</script>","i":"2026-01-02","j":null}'
    )
    assert stringify(datetime(2026, 1, 2)) == '"2026-01-02T00:00:00.000Z"'  # naive = UTC (Sequelize timezone +00:00)


def test_i18n_behaviour_matches_i18n_node():
    i18n.load_catalogs()
    assert i18n.translate("NO_SUCH_KEY", "en") == "NO_SUCH_KEY"
    assert i18n.translate("You are not authorized to access this resource.", "en") == "You are not authorized to access this resource."
    assert i18n.translate("forbidden", "en") == "Access forbidden."
    assert i18n.guess_locale(None, None) == "en"
    assert i18n.guess_locale("zh_HK", None) == "en"  # lower-cased before matching (i18n-node)
    assert i18n.guess_locale("id", None) == "id"
    assert i18n.guess_locale(None, "zh-HK,zh;q=0.9") == "en"
    assert i18n.guess_locale(None, "zh_HK") == "zh_HK"
    assert i18n.guess_locale(None, "id-ID,id;q=0.9,en;q=0.8") == "id"
    assert i18n.guess_locale(None, "fr,id;q=0.5") == "id"
    assert i18n.resolve_override("zh_hant") == "zh_HK" and i18n.resolve_override("ZH-HANS") == "zh_CN"
    assert i18n.resolve_override("fr") is None


def test_error_envelopes():
    t = i18n.Translator("en")
    assert error_response_body(StatusError.forbidden("You are not authorized to access this resource."), t) == (
        403, {"success": False, "message": "You are not authorized to access this resource."})
    assert error_response_body(StatusError.unauthorized(t("TOKEN_INVALID")), t) == (401, {"success": False, "message": "Token Invalid"})
    assert error_response_body(RuntimeError("boom"), t) == (500, {"success": False, "message": "An internal server error occurred."})


def test_settings_defaults_follow_node_number_or_default():
    s = build_settings(RawEnv(_env_file=None, CHAT_CONVERSATION_LIMIT="0", PORT="abc", IS_REDIS_ENABLED="false", API_KEYS="new, old,"))
    assert s.chat_conversation_limit == 15 and s.port == 7091
    assert s.redis.is_redis_enabled is False
    assert s.api_keys == ["new", "old"]
    assert s.socket.handshake_auth_enforce is True


SECRET = "unit-secret"


def _tok(**claims):
    base = {"sub": 5, "exp": int(time.time()) + 60, "jti": "j1"}
    base.update(claims)
    return jwt.encode(base, SECRET, algorithm="HS256")


def test_jwt_verification_rules():
    assert verify_access_token(_tok(), SECRET)["sub"] == 5  # integer sub (Laravel tymon) accepted
    assert verify_token(_tok(exp=int(time.time()) - 5), SECRET) is not None  # 10s clock tolerance
    assert verify_token(_tok(exp=int(time.time()) - 60), SECRET) is None  # expired
    assert verify_token(_tok(), "other") is None  # bad signature
    assert verify_token(_tok(), "") is None  # no secret -> fail closed
    assert verify_token(_tok(iat=int(time.time()) + 3600), SECRET) is not None  # iat not validated (jsonwebtoken)
    none_token = jwt.encode({"sub": 5}, key=None, algorithm="none")
    assert verify_token(none_token, SECRET) is None  # alg:none rejected
    with pytest.raises(StatusError) as caught:
        verify_access_token("a.b.c", SECRET)
    assert caught.value.status_code == 403 and caught.value.message == "forbidden"


def test_api_key_compare():
    assert is_api_key_valid("k", "k")
    assert not is_api_key_valid("k", "k2")
    assert not is_api_key_valid("k", "k" * 1000)  # length mismatch never raises
    assert is_api_key_valid("", None)  # like Node; the middleware rejects a missing key before comparing


def test_handshake_token_sources():
    assert extract_handshake_token({"token": "Bearer abc"}, {}) == "abc"
    assert extract_handshake_token(None, {"QUERY_STRING": "EIO=4&token=q1"}) == "q1"
    assert extract_handshake_token({}, {"HTTP_AUTHORIZATION": "bearer h1"}) == "h1"
    assert extract_handshake_token({}, {"HTTP_TOKEN": "t1"}) == "t1"
    assert extract_handshake_token({}, {}) == ""
