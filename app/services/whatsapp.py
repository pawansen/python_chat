"""360dialog WhatsApp Business API client (port of src/services/whatsAppMessage.service.js).

Same endpoints, payloads and headers (``D360-API-KEY``). Every call has the
OUTBOUND_HTTP_TIMEOUT_MS deadline (media download uses the longer
OUTBOUND_MEDIA_TIMEOUT_MS). Errors are re-raised to the caller exactly where
the Node code re-threw them, and swallowed (returning None) where it did not.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.parse import unquote

from app.core.config import get_settings
from app.core.http import AxiosError, error_status_or_message, request
from app.core.js import js_string, sanitize_filename
from app.redis import cache_keys, cache_service
from app.services import uploader


def _headers(json_content: bool = True) -> dict[str, str]:
    headers = {"D360-API-KEY": get_settings().whatsapp_api_key}
    if json_content:
        headers["Content-Type"] = "application/json"
    return headers


def _url(path: str) -> str:
    return get_settings().whatsapp_api_url + path


async def send_text_message_whatsapp(message: Any, to_number: str):
    try:
        return await request(
            "post",
            _url("/messages"),
            json_body={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to_number,
                "type": "text",
                "text": {"body": message},
            },
            headers=_headers(),
        )
    except Exception as error:
        print(str(error), flush=True)
        print(f"WhatsApp error: {error_status_or_message(error)}", flush=True)
        raise


async def read_text_message_whatsapp(wa_message_id: Any) -> None:
    try:
        await request(
            "post",
            _url("/messages"),
            json_body={"messaging_product": "whatsapp", "status": "read", "message_id": wa_message_id},
            headers=_headers(),
        )
    except Exception as error:
        print(f"WhatsApp message sent successfully: {error_status_or_message(error)}", flush=True)
        raise


async def send_media_message_whatsapp(file_url: str, media_type: str, to_number: str):
    try:
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_number,
            "type": media_type,
        }
        if media_type == "image":
            payload["image"] = {"link": file_url}
        elif media_type == "document":
            ext = os.path.splitext(file_url)[1].replace(".", "", 1) or "pdf"
            filename = f"file.{ext}"
            try:
                last = file_url.split("/")[-1]
                if last and "." in last:
                    filename = unquote(last.split("?")[0], errors="strict")
            except Exception:  # noqa: BLE001
                pass
            payload["document"] = {"link": file_url, "filename": filename}
        elif media_type == "audio":
            payload["audio"] = {"link": file_url}
        elif media_type == "video":
            payload["video"] = {"link": file_url}
        elif media_type == "sticker":
            payload["sticker"] = {"link": file_url}
        return await request("post", _url("/messages"), json_body=payload, headers=_headers())
    except Exception as error:
        print(f"WhatsApp sendMediaMessageWhatsApp error: {error_status_or_message(error)}", flush=True)
        raise


async def send_message_whatsapp(message: Any, to_number: str):
    return await request(
        "post",
        _url("/messages"),
        json_body={
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_number,
            "type": "text",
            "text": {"body": message},
        },
        headers=_headers(),
    )


async def send_otp_message_whatsapp(otp: Any, to_number: str, language: Any):
    return await request(
        "post",
        _url("/messages"),
        json_body={
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "template",
            "template": {
                "name": "authentication",
                "language": {"code": language},
                "components": [
                    {"type": "body", "parameters": [{"type": "text", "text": otp}]},
                    {"type": "button", "sub_type": "url", "index": 0, "parameters": [{"type": "text", "text": otp}]},
                ],
            },
        },
        headers=_headers(),
    )


async def send_welcome_message_whatsapp(to_number: str, language: Any, name: Any):
    try:
        response = await request(
            "post",
            _url("/messages"),
            json_body={
                "messaging_product": "whatsapp",
                "to": to_number,
                "type": "template",
                "template": {
                    "name": "welcome_message_template",
                    "language": {"code": language},
                    "components": [
                        {"type": "body"},
                        {"type": "header", "parameters": [{"type": "text", "text": name if name else "User"}]},
                    ],
                },
            },
            headers=_headers(),
        )
        return response.data
    except Exception as error:
        print(f"Error sending welcome message on WhatsApp: {error_status_or_message(error)}", flush=True)
        raise


async def send_template_message_whatsapp(to_number: str, template_info: dict, components: list, language: Any = ""):
    try:
        language_code = "zh_HK" if language in ("zh_chs", "zh_hant") else language
        response = await request(
            "post",
            _url("/messages"),
            json_body={
                "messaging_product": "whatsapp",
                "to": to_number,
                "type": "template",
                "template": {
                    "name": template_info.get("name"),
                    "language": {"code": language_code if language_code else template_info.get("language")},
                    "components": components,
                },
            },
            headers=_headers(),
        )
        return response.data
    except Exception as error:
        print(f"Error sending welcome message on WhatsApp: {error_status_or_message(error)}", flush=True)
        raise


async def send_assign_pt_message_whatsapp(to_number: str, language: Any, worker_name: Any, nationality: Any, reference_id: Any):
    try:
        await request(
            "post",
            _url("/messages"),
            json_body={
                "messaging_product": "whatsapp",
                "to": to_number,
                "type": "template",
                "template": {
                    "name": "assign_pt",
                    "language": {"code": language},
                    "components": [
                        {
                            "type": "body",
                            "parameters": [
                                {"type": "text", "text": worker_name},
                                {"type": "text", "text": nationality},
                                {"type": "text", "text": reference_id},
                            ],
                        }
                    ],
                },
            },
            headers=_headers(),
        )
    except Exception as error:
        print(f"Error sending assign PT message on WhatsApp: {error_status_or_message(error)}", flush=True)
        raise


async def get_template_list(name: Any, language: Any) -> Any:
    """Approved templates filtered by name list / language (TTL-only cache)."""
    try:
        if language == "zhh":
            language = "zh_HK"
        elif language == "zh_hant":
            language = "zh_CN"
        key = cache_keys.whatsapp_template_list_key(
            cache_keys.cache_key_hash({"name": name or "", "language": language or ""})
        )
        cached = await cache_service.get(key)
        if cached:
            return json.loads(cached)
        response = await request("get", _url("/v1/configs/templates"), headers=_headers(json_content=False))
        data = response.data if isinstance(response.data, dict) else {}
        templates = [t for t in (data.get("waba_templates") or []) if t.get("status") == "approved"]
        if name:
            names = [n.strip() for n in js_string(name).split(",")]
            filtered = [t for t in templates if t.get("name") in names and (not language or t.get("language") == language)]
        else:
            filtered = [t for t in templates if not language or t.get("language") == language]
        result = {"data": filtered}
        await cache_service.set(key, json.dumps(result, ensure_ascii=False, separators=(",", ":")), get_settings().redis.cache_ttl_long)
        return result
    except Exception as error:  # noqa: BLE001
        print(f"getWhatsAppTemplateInfo Error: {error_status_or_message(error)}", flush=True)
        return None


async def _templates() -> list[dict]:
    response = await request("get", _url("/v1/configs/templates"), headers=_headers(json_content=False))
    data = response.data if isinstance(response.data, dict) else {}
    templates = data.get("waba_templates") or data.get("templates")
    if templates is None:
        raise TypeError("Cannot read properties of undefined (reading 'find')")
    return templates


async def get_template_details(template_id: Any) -> Any:
    try:
        templates = await _templates()
        return next((t for t in templates if t.get("id") == template_id or t.get("external_id") == template_id), None)
    except Exception as error:  # noqa: BLE001
        print(f"getWhatsAppTemplateInfo Error sending message: {error_status_or_message(error)}", flush=True)
        return None


async def get_template_details_by_name(template_name: Any, language_code: Any) -> Any:
    try:
        templates = await _templates()
        return next((t for t in templates if t.get("name") == template_name and t.get("language") == language_code), None)
    except Exception as error:  # noqa: BLE001
        print(f"getWhatsAppTemplateInfo Error sending message: {error_status_or_message(error)}", flush=True)
        return None


async def download_media(media_id: Any, output_file_name: str) -> Any:
    """360dialog media -> S3 ``chat/`` (returns the S3 URL); raises on failure."""
    try:
        key = get_settings().whatsapp_api_key
        url_response = await request("get", f"https://waba-v2.360dialog.io/{media_id}", headers={"D360-API-KEY": key})
        data = url_response.data if isinstance(url_response.data, dict) else {}
        if not data.get("url"):
            raise AxiosError("Failed to get media URL.")
        relative = data["url"].replace("https://lookaside.fbsbx.com", "", 1)
        file_response = await request(
            "get",
            f"https://waba-v2.360dialog.io{relative}",
            headers={"D360-API-KEY": key},
            response_type="arraybuffer",
            timeout_ms=get_settings().outbound_media_timeout_ms,
        )
        file_ext = output_file_name.split(".")[-1]
        file_name = sanitize_filename(output_file_name.split(".")[0])
        return await uploader.upload({"originalname": output_file_name, "buffer": file_response.data}, "chat", file_ext, file_name)
    except Exception as error:
        print(f"❌ Error downloading/uploading media: {error}", flush=True)
        raise


async def validate_whatsapp_number(phone_number: str) -> None:
    try:
        await request(
            "post",
            "https://waba.360dialog.io/v1/contacts",
            json_body={"contacts": [phone_number], "force_check": True, "blocking": "wait"},
            headers=_headers(),
        )
    except Exception as error:
        print(f"Error sending welcome message on WhatsApp: {error_status_or_message(error)}", flush=True)
        raise


async def set_whatsapp_webhook() -> Any:
    try:
        response = await request(
            "post",
            _url("/v1/configs/webhook"),
            json_body={"url": "https://chatapi.py_chat.ai/chats/webhook"},
            headers=_headers(),
        )
        return response.data
    except Exception as error:  # noqa: BLE001
        print(f"Error sending message: {error_status_or_message(error)}", flush=True)
        return None


def now_ms() -> int:
    return int(time.time() * 1000)
