"""Local stand-in for 360dialog (WhatsApp) and the translate API.

Records every inbound call to ``RECORD_FILE`` (JSON lines) so the regression
runner can compare the OUTBOUND requests the Node and Python services make.
Run: python -m uvicorn tests.harness.mock_providers:app --port 9099
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

RECORD_FILE = Path(os.environ.get("MOCK_RECORD_FILE", Path(__file__).resolve().parent / ".run" / "provider_calls.jsonl"))
app = FastAPI()

TEMPLATES = {
    "waba_templates": [
        {"id": "t1", "external_id": "e1", "name": "welcome_message_template", "language": "en", "status": "approved",
         "components": [{"type": "HEADER", "format": "TEXT", "text": "Hello {{1}}"}, {"type": "BODY", "text": "Welcome to py_chat"}]},
        {"id": "t2", "external_id": "e2", "name": "job_apply", "language": "en", "status": "approved",
         "components": [{"type": "BODY", "text": "Hi {{1}}, a worker applied"},
                        {"type": "BUTTONS", "buttons": [{"type": "URL", "text": "Open", "url": "https://py_chat.ai/app{{1}}"}]}]},
        {"id": "t3", "name": "job_apply", "language": "zh_HK", "status": "approved",
         "components": [{"type": "BODY", "text": "你好 {{1}}"}]},
        {"id": "t4", "name": "draft_template", "language": "en", "status": "pending", "components": []},
    ]
}


def record(request: Request, body: object) -> None:
    RECORD_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "method": request.method,
        "path": request.url.path,
        "query": dict(request.query_params),
        "headers": dict(sorted((k, v) for k, v in request.headers.items() if k in ("d360-api-key", "api-key", "content-type"))),
        "body": body,
    }
    with RECORD_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


@app.post("/messages")
async def messages(request: Request):
    body = await request.json()
    record(request, body)
    return {"messaging_product": "whatsapp", "contacts": [{"wa_id": body.get("to")}], "messages": [{"id": "wamid.mock"}]}


@app.get("/v1/configs/templates")
async def templates(request: Request):
    record(request, None)
    return TEMPLATES


@app.get("/translate")
async def translate(request: Request):
    record(request, None)
    text = request.query_params.get("text", "")
    lang = request.query_params.get("target_language", "")
    return JSONResponse({"translated_text": f"[{lang}] {text}", "source_language": "en"})


@app.get("/reset")
async def reset():
    if RECORD_FILE.exists():
        RECORD_FILE.unlink()
    return {"ok": True}
