"""Node.js vs FastAPI regression comparison.

For each implementation (the ORIGINAL Node service and this port):
  1. reseed identical MySQL / DynamoDB fixtures and flush its Redis cache DB,
  2. start the server against that local infrastructure,
  3. run the same HTTP + Socket.IO scenarios in the same order,
  4. capture status, selected headers, body and every Socket.IO event.

Then diff the two captures after normalising only volatile values (timestamps
generated "now", request ids, ETags of bodies that contain them).

Usage:  python -m tests.e2e.regression_compare
Output: tests/e2e/regression_report.json (+ a summary on stdout)
Requires: local MySQL (3307), Redis (6379) and moto (5005); see tests/README.md.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import socketio

from tests.harness import env, seed, servers

REPORT = Path(__file__).resolve().parent / "regression_report.json"

ISO_NOW = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
FIXTURE_TIMES = {"2026-10-01"}  # fixture dates are stable and compared verbatim

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def H(user: int | None = None, **extra: str) -> dict[str, str]:
    headers = {"x-api-key": env.API_KEY}
    if user is not None:
        headers["authorization"] = "Bearer " + env.token(user)
    headers.update(extra)
    return headers


def scenarios() -> list[dict[str, Any]]:
    """(name, method, path, headers, json/query/content). Order matters (shared state)."""
    s: list[dict[str, Any]] = []

    def add(name: str, method: str, path: str, headers: dict | None = None, **kw: Any) -> None:
        s.append({"name": name, "method": method, "path": path, "headers": headers if headers is not None else H(), **kw})

    add("livez", "GET", "/livez", {})
    add("readyz", "GET", "/readyz", {})
    add("preflight allowed origin", "OPTIONS", "/chats/conversation-list", {"origin": "https://maidmatch.ai", "access-control-request-method": "POST"})
    add("preflight foreign origin", "OPTIONS", "/chats/conversation-list", {"origin": "https://evil.example", "access-control-request-method": "POST"})
    add("no api key", "POST", "/chats/conversation-list", {}, json={})
    add("bad api key", "POST", "/chats/conversation-list", {"x-api-key": "nope"}, json={})
    add("root path", "GET", "/", H())
    add("connect page", "GET", "/connect", H())
    add("unknown path", "GET", "/chats/nope", H())
    add("wrong method", "GET", "/chats/conversation-list", H())
    add("static avatar", "GET", "/images/user.png", {})
    add("missing token", "POST", "/chats/conversation-list", H(), json={"limit": 10, "page": 1, "platform": "app"})
    add("malformed token", "POST", "/chats/conversation-list", H(authorization="Bearer abc"), json={})
    add("bad signature", "POST", "/chats/conversation-list",
        H(authorization="Bearer " + env.token(100, secret="other-secret")), json={})
    add("expired token", "POST", "/chats/conversation-list", H(authorization="Bearer " + env.token(100, expired=True)), json={})
    add("token without Bearer prefix", "POST", "/chats/unread-message-count", H(authorization=env.token(100)), json={})
    add("inactive user", "POST", "/chats/unread-message-count", H(300), json={})
    add("validation: required", "POST", "/chats/create-conversation", H(100), json={})
    add("validation: enum", "POST", "/chats/create-conversation", H(100), json={"platform": "fax", "friend_id": 1, "friend_type": "x"})
    add("validation: number", "POST", "/chats/create-conversation", H(100), json={"platform": "app", "friend_id": "abc", "friend_type": "worker"})
    add("validation: empty string", "POST", "/chats/create-conversation", H(100), json={"platform": "app", "friend_id": 1, "friend_type": ""})
    add("validation: unknown key", "POST", "/chats/create-conversation", H(100),
        json={"platform": "app", "friend_id": 1, "friend_type": "worker", "hack": 1})
    add("validation: array body", "POST", "/chats/create-conversation", H(100), content=b"[1,2]",
        content_type="application/json")
    add("validation: query", "GET", "/chats/get-action-list", H(10), params={"limit": "x", "page": "1", "user_id": "200"})
    add("validation: when pid", "POST", "/chats/addRemoveGroupParticipant", H(100),
        json={"sid": "x", "groupId": 1001, "userId": 200, "action": "REMOVE"})
    add("malformed json", "POST", "/chats/conversation-list", H(100), content=b"{bad", content_type="application/json")
    add("form body", "POST", "/chats/unread-message-count", H(100), content=b"platform=app&platform=whatsapp",
        content_type="application/x-www-form-urlencoded")

    add("conversation-list", "POST", "/chats/conversation-list", H(100), json={"limit": 10, "page": 1, "platform": "app"})
    add("conversation-list cached", "POST", "/chats/conversation-list", H(100), json={"limit": 10, "page": 1, "platform": "app"})
    add("conversation-list group_team", "POST", "/chats/conversation-list", H(100),
        json={"limit": 10, "page": 1, "platform": "app", "friend_type": "group_team"})
    add("conversation-list search miss", "POST", "/chats/conversation-list", H(100),
        json={"limit": 10, "page": 1, "platform": "app", "search": "zzz"})
    add("conversation-list-all", "POST", "/chats/conversation-list-all", H(100), json={"limit": 10, "page": 1, "platform": "app"})
    add("conversation-list-all staff", "POST", "/chats/conversation-list-all", H(10), json={"limit": 10, "page": 1, "platform": "app"})
    add("conversation-list-all unread", "POST", "/chats/conversation-list-all", H(100),
        json={"limit": 10, "page": 1, "platform": "app", "actionType": "unread"})
    add("conversation-list-all not_replied", "POST", "/chats/conversation-list-all", H(10),
        json={"limit": 5, "page": 1, "platform": "whatsapp", "actionType": "not_replied", "user_id": 200})
    add("conversation-list-all worker filter", "POST", "/chats/conversation-list-all", H(100),
        json={"limit": 10, "page": 1, "platform": "app", "friend_type": "worker", "search": "Siti"})
    add("conversation-list-app", "POST", "/chats/conversation-list-app", H(200), json={"limit": 10, "page": 1, "platform": "app"})
    add("conversation-list-no-replay", "POST", "/chats/conversation-list-no-replay", H(100), json={"limit": 10, "page": 1, "platform": "app"})
    add("unread-message-count", "POST", "/chats/unread-message-count", H(100), json={})
    add("unread-message-count platform", "POST", "/chats/unread-message-count", H(100), json={"platform": "app"})
    add("unread-message-count none", "POST", "/chats/unread-message-count", H(201), json={})
    add("conversactions staff", "POST", "/chats/conversactions", H(10), json={"limit": 10, "page": 1, "user_id": 100})
    add("conversactions end-user scoped", "POST", "/chats/conversactions", H(100), json={"limit": 10, "page": 1, "user_id": 10})
    add("get-conversactions-history", "POST", "/chats/get-conversactions-history", H(10), json={"limit": 10, "page": 1, "user_id": 10})
    add("message-list", "POST", "/chats/message-list", H(100), json={"sid": 1001, "limit": 10})
    add("message-list not participant", "POST", "/chats/message-list", H(201), json={"sid": 1001, "limit": 10})
    add("message-list-app", "POST", "/chats/message-list-app", H(200), json={"sid": 1001, "limit": 2})
    add("message-list-group", "POST", "/chats/message-list-group", H(100), json={"sid": 1001, "limit": 10})
    add("chat-history-list", "POST", "/chats/chat-history-list", H(10),
        json={"user_id": 10, "group_id": "1001,1002,1003", "limit": 10, "platform": "app,whatsapp"})
    add("chat-history-list search", "POST", "/chats/chat-history-list", H(10),
        json={"user_id": 10, "group_id": 1001, "limit": 10, "search": "hi", "platform": "app"})
    add("read-all-message", "POST", "/chats/read-all-message", H(100), json={"sid": 1001})
    add("updateReadMessageCount", "PUT", "/chats/updateReadMessageCount", H(100), json={"sid": "1001"})
    add("unread after reset", "POST", "/chats/unread-message-count", H(100), json={})
    add("get-action-list staff", "GET", "/chats/get-action-list", H(10), params={"limit": "10", "page": "1", "user_id": "200"})
    add("get-action-list filtered miss", "GET", "/chats/get-action-list", H(10),
        params={"limit": "10", "page": "1", "user_id": "200", "action_type": "zzz"})
    add("create-conversation existing", "POST", "/chats/create-conversation", H(100),
        json={"platform": "app", "friend_id": 200, "friend_type": "worker"})
    add("create-conversation new app", "POST", "/chats/create-conversation", H(100),
        json={"platform": "app", "friend_id": 201, "friend_type": "worker"})
    add("create-conversation new whatsapp", "POST", "/chats/create-conversation", H(10),
        json={"platform": "whatsapp", "friend_id": 201, "friend_type": "worker"})
    add("create-conversation whatsapp existing", "POST", "/chats/create-conversation", H(10),
        json={"platform": "whatsapp", "friend_id": 200, "friend_type": "worker"})
    add("create-conversation line not linked", "POST", "/chats/create-conversation", H(10),
        json={"platform": "line", "friend_id": 200, "friend_type": "worker"})
    add("create-conversation inactive friend", "POST", "/chats/create-conversation", H(100),
        json={"platform": "app", "friend_id": 999, "friend_type": "worker"})
    add("create-conversation-group existing", "POST", "/chats/create-conversation-group", H(10),
        json={"platform": "app", "id": 555, "friend_type": "group_team", "job_worker_id": 70, "job_interview_id": 7001})
    add("create-conversation-group missing job", "POST", "/chats/create-conversation-group", H(10),
        json={"platform": "app", "id": 556, "friend_type": "group_team"})
    add("validate-change cs", "POST", "/chats/validate-change-user-group-cs-interpreter", H(1),
        json={"user_id": 10, "group_id": 1002, "user_type": "cs"})
    add("validate-change missing group", "POST", "/chats/validate-change-user-group-cs-interpreter", H(1),
        json={"user_id": 10, "group_id": 9999, "user_type": "cs"})
    add("dup employers", "GET", "/global/get-duplicate-employers", H(1), params={"limit": "10", "page": "1", "user_id": "100"})
    add("dup employers count", "GET", "/global/get-duplicate-employers-count", H(1), params={"user_id": "100"})
    add("dup workers", "GET", "/global/get-duplicate-workers", H(1), params={"limit": "10", "page": "1", "user_id": "200"})
    add("dup workers count", "GET", "/global/get-duplicate-workers-count", H(1), params={"user_id": "200"})
    add("dup jobs count", "GET", "/global/get-duplicate-jobs-count", H(1), params={"employer_id": "50,51"})
    add("dup jobs", "GET", "/global/get-duplicate-jobs", H(1), params={"limit": "10", "page": "1", "employer_id": "50"})
    add("dup end-user forbidden", "GET", "/global/get-duplicate-employers", H(100), params={"limit": "10", "page": "1", "user_id": "101"})
    add("previous images", "GET", "/global/get-worker-previous-images", H(1), params={"user_id": "200"})
    add("save language", "POST", "/global/save-language-translate", H(100),
        json={"chat_translate_view": "both", "chat_translate_language": "en"})
    add("get language", "GET", "/global/get-language-translate", H(100))
    add("fetchConversation", "POST", "/chats/fetchConversation", H(100), json={"sid": "CH1"})
    add("getMessageById", "POST", "/chats/getMessageById", H(100), json={"sid": "CH1", "msid": "IM1"})
    add("allMessage", "POST", "/chats/allMessage", H(100), json={"sid": "CH1", "limit": 5})
    add("fetchAllParticipant", "POST", "/chats/fetchAllParticipant", H(100), json={"sid": "CH1", "limit": 5})
    add("getParticipantById", "POST", "/chats/getParticipantById", H(100), json={"sid": "CH1", "psid": "MB1"})
    add("filterListConversation", "POST", "/chats/filterListConversation", H(100), json={"type": "ALL"})
    add("send-auto-message-whatsapp", "POST", "/chats/send-auto-message-whatsapp", H(100), json={})
    add("get-template-list (provider down)", "GET", "/chats/get-template-list-whatsapp", H())
    add("add-action-required", "POST", "/chats/add-action-required", H(10),
        json={"user_id": 201, "entity_id": 71, "entity_type": "worker", "action_type": "doc", "content": "Hi {{ worker_name }}"})
    add("send-chat-message app", "POST", "/chats/send-chat-message", H(10),
        json={"platform": "app", "user_id": 201, "user_type": "worker", "message": "hello from cs"})
    add("send-template-message app", "POST", "/chats/send-template-message", H(10),
        json={"platform": "app", "friend_id": 201, "friend_type": "worker", "template_content": "Dear {{ worker_name }} {{ unknown }}"})
    add("action-required passport", "POST", "/chats/send-template-message-action-required-passport", H(),
        json={"user_id": 201, "reason": 1, "language": "en", "action_module": "passport"})
    add("upload-media png", "POST", "/chats/upload-media", H(100), files={"file": ("photo one.png", PNG, "image/png")})
    add("upload-media wrong field", "POST", "/chats/upload-media", H(100), files={"other": ("a.png", PNG, "image/png")})
    add("upload-media disallowed mime", "POST", "/chats/upload-media", H(100), files={"file": ("a.txt", b"hello", "text/plain")})
    add("whatsapp webhook", "POST", "/chats/webhook", {},
        json={"entry": [{"changes": [{"value": {"messages": [{"from": "628120000200", "id": "wamid.1", "type": "text", "text": {"body": "inbound hi"}}]}}]}]})
    add("line webhook", "POST", "/chats/line-hook", {}, json={"events": []})
    add("send-template-message whatsapp", "POST", "/chats/send-template-message", H(10),
        json={"platform": "whatsapp", "friend_id": 200, "friend_type": "worker", "template_content": "<p>Hi {{ worker_name }}</p>",
              "template_file_path": "https://cdn.example.test/doc.pdf"})
    add("send-whatsapp-template-message", "POST", "/chats/send-whatsapp-template-message", H(10),
        json={"platform": "whatsapp", "friend_id": 200, "friend_type": "worker", "template_id": "e1",
              "custom_content_params": "{\"name\": \"Siti\"}"})
    add("send-whatsapp-template-message missing name", "POST", "/chats/send-whatsapp-template-message", H(10),
        json={"platform": "whatsapp", "friend_id": 200, "friend_type": "worker", "template_id": "t1"})
    add("whatsapp-template-notification job_apply", "POST", "/chats/whatsapp-template-notification", H(),
        json={"language": "en", "user_id": 100, "user_type": "employer", "message_type": "job_apply",
              "custom_content_params": "{\"name\": \"Chan\", \"worker_id\": 70, \"job_id\": \"7001\", \"employer_id\": 50}"})
    add("whatsapp-template-notification missing params", "POST", "/chats/whatsapp-template-notification", H(),
        json={"language": "en", "user_id": 100, "user_type": "employer", "message_type": "job_apply", "custom_content_params": "{}"})
    add("whatsapp-template-notification unknown template", "POST", "/chats/whatsapp-template-notification", H(),
        json={"language": "id", "user_id": 100, "user_type": "employer", "message_type": "job_matched"})
    add("send-template-message-assign-partner", "POST", "/chats/send-template-message-assign-partner", H(10),
        json={"platform": "whatsapp", "friend_id": 201, "friend_type": "worker", "template_content": "assigned",
              "worker_name": "Siti", "nationality": "ID", "reference_id": "LPF000071"})
    add("send-otp-whatsapp", "POST", "/chats/send-otp-whatsapp", H(10), json={"language": "en", "user_id": 200, "otp": 123456})
    add("send-otp-whatsapp-direct", "POST", "/whatsapp/send-otp-direct", H(10),
        json={"language": "zh_hk", "whatsapp_country_code": "+852", "whatsapp_number": 91234567, "otp": 4321})
    add("send-welcome-message-whatsapp", "POST", "/chats/send-welcome-message-whatsapp", H(),
        json={"language": "id", "whatsapp_country_code": "+62", "whatsapp_number": "81200"})
    add("whatsapp send-message", "POST", "/whatsapp/send-message", H(), json={"user_id": 201, "message": "<b>hi</b>  there"})
    add("whatsapp send-welcome-message", "POST", "/whatsapp/send-welcome-message", H(10), json={"user_id": 201})
    add("whatsapp send-message no wa", "POST", "/whatsapp/send-message", H(), json={"user_id": 10, "message": "x"})
    add("get-template-list", "GET", "/chats/get-template-list-whatsapp", H(), params={"name": "job_apply,welcome_message_template", "language": "en"})
    add("get-template-list zhh", "GET", "/chats/get-template-list-whatsapp", H(), params={"language": "zhh"})
    add("message-list after webhook", "POST", "/chats/message-list", H(10), json={"sid": 1003, "limit": 10, "platform": "whatsapp"}, delay=2.0)
    return s


VOLATILE_KEYS = {"ts", "x-request-id", "date", "etag", "delivered_at", "wa_window_expires_at", "isActiveSeason"}


def normalise(value: Any, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {k: normalise(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [normalise(v) for v in value]
    if isinstance(value, str):
        if key in VOLATILE_KEYS:
            return "<volatile>"
        if ISO_NOW.match(value) and value[:10] not in FIXTURE_TIMES:
            return "<now>"
        if key == "access-token":
            return "<jwt>"
        if key == "mediaUrl":
            return re.sub(r"\d{13}", "<ms>", value)
    if isinstance(value, float) and key == "response_time":
        return "<seconds>"
    return value


async def run_http(port: int, scenario: dict[str, Any]) -> dict[str, Any]:
    if scenario.get("delay"):
        await asyncio.sleep(scenario["delay"])
    url = f"http://127.0.0.1:{port}{scenario['path']}"
    headers = dict(scenario["headers"])
    kwargs: dict[str, Any] = {"headers": headers, "params": scenario.get("params")}
    if "json" in scenario:
        kwargs["json"] = scenario["json"]
    if "content" in scenario:
        kwargs["content"] = scenario["content"]
        headers["content-type"] = scenario["content_type"]
    if "files" in scenario:
        kwargs["files"] = scenario["files"]
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        try:
            response = await client.request(scenario["method"], url, **kwargs)
        except httpx.HTTPError as error:
            return {"error": str(error)}
    text = response.text
    try:
        body: Any = json.loads(text)
    except ValueError:
        body = text
    picked = {k: response.headers.get(k) for k in (
        "content-type", "access-control-allow-origin", "access-control-allow-credentials", "access-control-allow-methods",
        "access-control-allow-headers", "vary", "x-content-type-options", "x-frame-options", "content-security-policy",
        "strict-transport-security", "referrer-policy", "cross-origin-resource-policy", "ratelimit-limit", "ratelimit-policy",
    ) if response.headers.get(k) is not None}
    return {"status": response.status_code, "headers": picked, "body": normalise(body)}


async def run_socket(port: int) -> dict[str, Any]:
    """Socket.IO scenarios (python-socketio client speaks the v4 protocol both servers accept)."""
    url = f"http://127.0.0.1:{port}"
    results: dict[str, Any] = {}

    async def client(user: int | None, *, auth_token: str | None = None):
        sio = socketio.AsyncClient(reconnection=False)
        events: list[Any] = []

        @sio.on("*")
        async def any_event(event, *data):
            events.append([event, normalise(data[0] if data else None)])

        token = auth_token if auth_token is not None else (env.token(user) if user else None)
        try:
            await sio.connect(url, auth={"token": token} if token else None, transports=["websocket"], socketio_path="socket.io", wait_timeout=10)
            return sio, events, None
        except socketio.exceptions.ConnectionError as error:
            return None, events, str(error)

    _, _, err = await client(None)
    results["connect without token"] = err
    _, _, err = await client(None, auth_token="garbage")
    results["connect bad token"] = err

    a, a_events, err = await client(100)
    b, b_events, err_b = await client(200)
    staff, s_events, _ = await client(10)
    results["connect ok"] = [err, err_b]
    if a and b and staff:
        await b.emit("join_room_user", {"userId": 200})
        await a.emit("join_room_user", {"userId": 100})
        await a.emit("join_room_user", {"userId": 200})  # someone else's room -> INVALID_ACCESS
        await asyncio.sleep(0.8)
        await a.emit("join_room", {"conversation_sid": 1001})
        await b.emit("join_room", {"conversation_sid": 1001})
        await staff.emit("join_room", {"conversation_sid": 1001})  # not a participant
        await a.emit("join_room", {"conversation_sid": 999999})  # unknown group
        await asyncio.sleep(1.0)
        await a.emit("send_message", {"conversation_sid": 1001, "body": "socket hello", "message_type": "text", "platform": "app"})
        await asyncio.sleep(2.0)
        await b.emit("chat_message_read", {"conversation_sid": 1001})
        await asyncio.sleep(1.0)
        await a.emit("delete_message", {"conversation_sid": 1001, "message_id": 6})
        await a.emit("delete_message", {"conversation_sid": 1001})
        await asyncio.sleep(1.0)
        await a.emit("join_room", {"conversation_sid": 1002})
        await asyncio.sleep(0.8)
        await a.emit("send_message", {"conversation_sid": 1002, "body": "email me at gmail", "action": "app", "platform": "app"})
        await asyncio.sleep(1.0)
        await a.emit("join_branch_room", {"branch_id": 7})
        await asyncio.sleep(0.8)
        await staff.emit("join_room", {"conversation_sid": 1003})
        await asyncio.sleep(0.8)
        await staff.emit("send_message", {"conversation_sid": 1003, "body": "wa from staff", "platform": "whatsapp", "message_type": "text"})
        await asyncio.sleep(4.0)  # translation worker -> message_translation_updated
        key = lambda e: json.dumps(e, sort_keys=True, ensure_ascii=False)  # noqa: E731 - async timing may interleave
        results["employer events"] = sorted(a_events, key=key)
        results["worker events"] = sorted(b_events, key=key)
        results["staff events"] = sorted(s_events, key=key)
    for c in (a, b, staff):
        if c:
            await c.disconnect()
    return results


def provider_calls() -> list[Any]:
    path = env.RUN_DIR / "provider_calls.jsonl"
    if not path.exists():
        return []
    calls = [normalise(json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return sorted(calls, key=lambda c: json.dumps(c, sort_keys=True, ensure_ascii=False))


async def capture(kind: str) -> dict[str, Any]:
    seed.seed_all()
    seed.flush_redis()
    httpx.get(f"http://127.0.0.1:{env.MOCK_PORT}/reset", trust_env=False)
    proc = servers.start(kind)
    port = env.NODE_PORT if kind == "node" else env.PY_PORT
    try:
        http_results = {}
        for scenario in scenarios():
            http_results[scenario["name"]] = await run_http(port, scenario)
        socket_results = await run_socket(port)
        await asyncio.sleep(3)
        socket_results["outbound provider calls"] = provider_calls()
        return {"http": http_results, "socket": socket_results}
    finally:
        servers.stop(proc)


def diff(a: Any, b: Any, path: str = "") -> list[str]:
    out: list[str] = []
    if isinstance(a, dict) and isinstance(b, dict):
        if list(a.keys()) != list(b.keys()) and set(a.keys()) == set(b.keys()):
            out.append(f"{path}: key order differs")
        for key in sorted(set(a) | set(b), key=str):
            if key not in a:
                out.append(f"{path}.{key}: only in python = {json.dumps(b[key], ensure_ascii=False)[:200]}")
            elif key not in b:
                out.append(f"{path}.{key}: only in node = {json.dumps(a[key], ensure_ascii=False)[:200]}")
            else:
                out.extend(diff(a[key], b[key], f"{path}.{key}"))
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: length node={len(a)} python={len(b)}")
        for index, (x, y) in enumerate(zip(a, b, strict=False)):
            out.extend(diff(x, y, f"{path}[{index}]"))
        return out
    if a != b:
        out.append(f"{path}: node={json.dumps(a, ensure_ascii=False)[:300]} python={json.dumps(b, ensure_ascii=False)[:300]}")
    return out


def start_mock():
    import subprocess

    (env.RUN_DIR).mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "tests.harness.mock_providers:app", "--port", str(env.MOCK_PORT), "--log-level", "warning"],
        cwd=env.ROOT,
    )
    for _ in range(60):
        try:
            httpx.get(f"http://127.0.0.1:{env.MOCK_PORT}/reset", trust_env=False, timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.5)
    return proc


async def main() -> int:
    started = time.time()
    mock = start_mock()
    node = await capture("node")
    python = await capture("python")
    report: dict[str, Any] = {"generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "scenarios": {}}
    total = identical = 0
    for section in ("http", "socket"):
        for name in node[section]:
            total += 1
            differences = diff(node[section][name], python[section].get(name))
            if not differences:
                identical += 1
            report["scenarios"][f"{section}: {name}"] = {
                "identical": not differences, "differences": differences,
                "node": node[section][name], "python": python[section].get(name),
            }
    mock.terminate()
    report["summary"] = {"total": total, "identical": identical, "different": total - identical, "seconds": int(time.time() - started)}
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report["summary"]))
    for name, entry in report["scenarios"].items():
        if not entry["identical"]:
            print(f"\n## {name}")
            for line in entry["differences"][:12]:
                print("  ", line)
    return 0 if identical == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
