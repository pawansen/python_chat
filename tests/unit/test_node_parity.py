"""Parity with the ORIGINAL Node modules (fixtures from tests/fixtures/generate_node_fixtures.mjs).

Covers: every celebrate/Joi validator over a 1,700-case corpus (status, error
envelope, converted values), hashids, the content filter, template
placeholder resolution and cache-key hashing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.errors import CelebrateValidationError
from app.core.jsonx import stringify
from app.redis import cache_keys
from app.schemas import chat as schemas
from app.schemas.joi import validate_segment
from app.services.chat.common import hashids_encode
from app.socket import content_filter
from app.utils.template_placeholder import resolve_template_placeholders

FIXTURES = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "node_parity.json").read_text(encoding="utf-8"))


def _id(case: dict) -> str:
    return f"{case['validator']}:{json.dumps(case['value'], ensure_ascii=False)[:60]}"


@pytest.mark.parametrize("case", FIXTURES["validation"], ids=_id)
def test_validation_matches_celebrate(case: dict) -> None:
    model = getattr(schemas, case["model"])
    value = None if case["value"] == "__undefined__" else case["value"]
    expected = case["result"]
    try:
        result = validate_segment(model, case["segment"], value)
    except CelebrateValidationError as error:
        assert not expected["ok"], f"python rejected, node accepted: {error.body()}"
        assert expected["status"] == 400
        assert error.body() == expected["body"]
        return
    assert expected["ok"], f"python accepted, node rejected with {expected.get('body')}"
    node_value = None if expected["value"] == "__undefined__" else expected["value"]
    assert json.loads(stringify(result)) == node_value


@pytest.mark.parametrize("case", FIXTURES["hashids"], ids=lambda c: repr(c["input"]))
def test_hashids(case: dict) -> None:
    value = None if case["input"] == "__undefined__" else case["input"]
    if case["input"] == "__undefined__":
        from app.core.js import UNDEFINED

        value = UNDEFINED
    assert hashids_encode(value) == case["output"]


@pytest.mark.parametrize("case", FIXTURES["filter"], ids=lambda c: c["input"][:20])
def test_content_filter(case: dict) -> None:
    norm = content_filter.normalize_tight(case["input"])
    assert norm == case["norm"]
    assert content_filter.has_blocked_term(case["input"], norm) == case["blocked"]


@pytest.mark.parametrize("case", FIXTURES["blockedMessages"], ids=lambda c: str(c["locale"]))
def test_blocked_messages(case: dict) -> None:
    assert content_filter.get_blocked_message(case["locale"]) == case["message"]


async def _fetch_sender(_):
    return {"first_name": "Amy", "country_code": "+852", "mobile_number": "60000000", "email": "amy@oec.com"}


async def _fetch_worker(_):
    return {"name": "Siti", "surname": "Rahayu", "whatsapp_country_code": "+62", "whatsapp_number": "81234567", "hk_id": "A1",
            "passport_number": "P1", "country_code": "+852", "mobile_number": "9", "email": "s@x"}


async def _fetch_employer(_):
    return {"name": "Ka", "surname": "Chan", "chinese_name": "陳", "hk_id": "Z1"}


async def _fetch_view(_):
    return {"user_id": 1, "related_name": "Rel Name"}


FETCHERS = {"fetchSenderProfile": _fetch_sender, "fetchWorkerProfile": _fetch_worker,
            "fetchEmployerProfile": _fetch_employer, "fetchFriendView": _fetch_view}


@pytest.mark.parametrize("case", FIXTURES["templates"], ids=lambda c: c["content"][:25])
async def test_template_placeholders(case: dict) -> None:
    out = await resolve_template_placeholders(
        {"template_content": case["content"], "friend_type": case["friend_type"], "friend_id": 5, "sender_user_id": 1}, FETCHERS
    )
    assert out == case["output"]


@pytest.mark.parametrize("case", FIXTURES["cacheHash"], ids=lambda c: json.dumps(c["input"])[:30])
def test_cache_key_hash(case: dict) -> None:
    assert cache_keys.cache_key_hash(case["input"]) == case["hash"]


def test_cache_key_formats() -> None:
    expected = FIXTURES["cacheKeys"]
    assert cache_keys.message_list_key(12, "3", "h") == expected["message"]
    assert cache_keys.user_detail_key(5, "h") == expected["userDetail"]
    assert cache_keys.version_conversation_key(5) == expected["version"]
    assert cache_keys.jwt_blocklist_key("jwt:blocklist:", "abc") == expected["jwt"]
