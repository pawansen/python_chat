"""Background workers (ports of src/workers/translation.worker.js and webhook.worker.js)."""

from __future__ import annotations

from typing import Any

from app.core.js import js_string, nullish
from app.core.logging import component_log
from app.queues.translation_queue import set_translation_processor
from app.queues.webhook_queue import set_webhook_processor
from app.repositories import aws_message as aws_message_repo
from app.services.translate import translate_text
from app.socket.emitter import Emitter

tlog = component_log("translation-worker")
wlog = component_log("webhook-worker")

_io: Emitter | None = None


def to_content_columns(translations: dict[str, Any]) -> dict[str, Any]:
    columns: dict[str, Any] = {}
    for key, value in (translations or {}).items():
        if key == "source_language":
            columns["source_language"] = value
        else:
            columns[f"content_{key}"] = None if nullish(value) else value
    return columns


async def process_translation_job(job: dict[str, Any]) -> None:
    translations = await translate_text(job["content"])
    succeeded = sum(1 for k, v in translations.items() if k != "source_language" and v)
    if succeeded == 0:
        raise RuntimeError("all languages failed to translate")
    columns = to_content_columns(translations)
    result = await aws_message_repo.update_message_translations(job["messageId"], job["createdAt"], columns, job.get("groupId"))
    if result and result.get("error"):
        raise RuntimeError(f"DynamoDB update failed: {result['error']}")
    await emit_translation_update(job, columns)
    tlog("info", "translation_persisted_and_emitted", messageId=job["messageId"], groupId=job.get("groupId"), succeeded=succeeded)


async def emit_translation_update(job: dict[str, Any], columns: dict[str, Any]) -> None:
    if _io is None:
        tlog("error", "emit_skipped_no_io", messageId=job.get("messageId"))
        return
    payload = {
        "status": True,
        "message": "Success",
        "data": {"id": job["messageId"], "message_id": job["messageId"], "group_id": job.get("groupId"),
                 "conversation_sid": job.get("groupId"), **columns},
    }
    await _io.emit_to(js_string(job.get("groupId")), "message_translation_updated", payload)
    rooms: list[str] = []
    for value in (job.get("receiverId"), job.get("senderId"), job.get("userId")):
        if not nullish(value) and js_string(value) not in rooms:
            rooms.append(js_string(value))
    for room in rooms:
        await _io.emit_to(room, "message_translation_updated", payload)


def init_translation_worker(io: Emitter) -> None:
    global _io
    _io = io
    set_translation_processor(process_translation_job)
    tlog("info", "worker_initialized")


def init_webhook_worker(io: Emitter) -> None:
    from app.services import line as line_service
    from app.services.chat import whatsapp_flows

    async def process(job: dict[str, Any]) -> None:
        source = job.get("source")
        if source == "whatsapp":
            await whatsapp_flows.process_whatsapp_webhook_job(job.get("body"), io)
        elif source == "line":
            await line_service.handle_webhook_line_message(job.get("body"), io)
        else:
            wlog("error", "unknown_webhook_source", source=source)

    set_webhook_processor(process)
    wlog("info", "worker_initialized")
