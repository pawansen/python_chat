"""REST routes (ports of src/routes/aws.route.js, whatsapp.route.js, global.route.js
and the thin src/controllers/aws.controller.js).

Every route keeps its URL, method, middleware order (verifyToken ->
celebrate validation -> controller) and its ``res.ok({...result})`` envelope
(``success: true`` appended when the service did not set it).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import Response

from app.api.auth import verify_request_token
from app.api.context import RequestContext
from app.api.http import send
from app.core.errors import UploadError, error_response_body
from app.core.i18n import Translator, guess_locale, resolve_override
from app.core.logging import logger
from app.schemas import chat as S
from app.schemas.joi import JoiModel, validate_segment
from app.services import uploader
from app.services.chat import conversations, duplicates, lists, messages, templates, whatsapp_flows

Service = Callable[[RequestContext], Awaitable[Any]]


@dataclass(frozen=True)
class RouteSpec:
    method: str
    path: str
    service: Service
    auth: bool = True
    body: type[JoiModel] | None = None
    query: type[JoiModel] | None = None
    upload: bool = False
    name: str = ""


async def create_conversation_controller(req: RequestContext) -> Any:
    platform = (req.body or {}).get("platform") if isinstance(req.body, dict) else None
    if platform == "app":
        return await conversations.create_conversation(req)
    if platform in ("whatsapp", "action_required"):
        return await conversations.create_conversation_whatsapp_action_required(req)
    if platform == "line":
        return await conversations.create_conversation_line(req)
    return _NO_RESPONSE


_NO_RESPONSE = object()

ROUTES: list[RouteSpec] = [
    # /chats (aws.route.js)
    RouteSpec("POST", "/chats/create-conversation", create_conversation_controller, body=S.CreateConversation),
    RouteSpec("POST", "/chats/create-conversation-group", conversations.create_conversation_group, body=S.CreateConversationGroup),
    RouteSpec("POST", "/chats/validate-change-user-group-cs-interpreter", conversations.validate_change_user_group_cs_interpreter,
              body=S.ValidateChangeUserGroupCsInterpreter),
    RouteSpec("POST", "/chats/conversation-list", lists.conversation_list, body=S.ConversationList),
    RouteSpec("POST", "/chats/conversation-list-all", lists.conversation_list_all, body=S.ConversationList),
    RouteSpec("POST", "/chats/unread-message-count", lists.unread_message_count),
    RouteSpec("POST", "/chats/conversation-list-app", lists.conversation_list_app, body=S.ConversationList),
    RouteSpec("POST", "/chats/conversation-list-no-replay", lists.conversation_list_no_replay, body=S.ConversationList),
    RouteSpec("POST", "/chats/conversactions", lists.conversactions, body=S.Conversactions),
    RouteSpec("POST", "/chats/get-conversactions-history", lists.get_conversactions_history, body=S.Conversactions),
    RouteSpec("POST", "/chats/message-list", messages.message_list, body=S.MessageListAws),
    RouteSpec("POST", "/chats/message-list-app", messages.message_list_app, body=S.MessageListAws),
    RouteSpec("POST", "/chats/message-list-group", messages.get_message_list_grouped_by_date, body=S.MessageListAws),
    RouteSpec("POST", "/chats/chat-history-list", messages.chat_history_list, body=S.ChatHistoryList),
    RouteSpec("POST", "/chats/read-all-message", messages.read_all_message, body=S.ReadAllMessageAws),
    RouteSpec("POST", "/chats/upload-media", messages.send_file, upload=True),
    RouteSpec("POST", "/chats/send-template-message", templates.send_template_message, body=S.SendTemplateMessage),
    RouteSpec("POST", "/chats/send-whatsapp-template-message", templates.send_whatsapp_template_message, body=S.SendWhatsAppTemplateMessage),
    RouteSpec("POST", "/chats/whatsapp-template-notification", templates.send_whatsapp_template_notification, auth=False,
              body=S.SendWhatsAppTemplateNotification),
    RouteSpec("POST", "/chats/send-template-message-whatsapp", templates.send_whatsapp_template_message, auth=False,
              body=S.SendWhatsAppTemplateMessage),
    RouteSpec("POST", "/chats/send-template-message-assign-partner", templates.send_template_message_assign_partner,
              body=S.SendTemplateMessageAssignPartner),
    RouteSpec("GET", "/chats/get-template-list-whatsapp", templates.get_template_list, auth=False),
    RouteSpec("POST", "/chats/send-template-message-action-required-passport", templates.send_template_message_action_required,
              auth=False, body=S.SendTemplateMessageActionRequired),
    RouteSpec("POST", "/chats/send-chat-message", templates.send_chat_message, body=S.SendChatMessage),
    RouteSpec("POST", "/chats/add-action-required", templates.add_action_required, body=S.AddActionRequired),
    RouteSpec("GET", "/chats/get-action-list", lists.get_action_list, query=S.GetActionList),
    RouteSpec("POST", "/chats/send-message-whatsapp", whatsapp_flows.send_message_whatsapp),
    RouteSpec("POST", "/chats/send-otp-whatsapp", whatsapp_flows.send_otp_whatsapp, body=S.SendOTPWhatsapp),
    RouteSpec("POST", "/chats/send-otp-whatsapp-direct", whatsapp_flows.send_otp_whatsapp_direct, body=S.SendOTPWhatsappDirect),
    RouteSpec("POST", "/chats/send-welcome-message-whatsapp", whatsapp_flows.send_welcome_message_whatsapp, auth=False,
              body=S.SendWelcomeMessageWhatsapp),
    RouteSpec("POST", "/chats/send-auto-message-whatsapp", whatsapp_flows.send_auto_message_whatsapp),
    RouteSpec("GET", "/chats/getAccessToken", messages.get_access_token),
    RouteSpec("POST", "/chats/addRemoveGroupParticipant", messages.add_remove_group_participant, body=S.AddRemoveGroupParticipant),
    RouteSpec("POST", "/chats/fetchConversation", messages.fetch_conversation, body=S.FetchConversation),
    RouteSpec("POST", "/chats/getMessageById", messages.get_message_by_id, body=S.GetMessageById),
    RouteSpec("POST", "/chats/allMessage", messages.all_message, body=S.AllMessage),
    RouteSpec("PUT", "/chats/updateReadMessageCount", messages.update_read_message_count, body=S.UpdateReadMessageCount),
    RouteSpec("POST", "/chats/filterListConversation", lists.filter_list_conversation),
    RouteSpec("POST", "/chats/fetchAllParticipant", messages.fetch_all_participant, body=S.FetchAllParticipant),
    RouteSpec("POST", "/chats/getParticipantById", messages.get_participant_by_id, body=S.GetParticipantById),
    RouteSpec("POST", "/chats/createWhatsappGroup", messages.create_whatsapp_group, body=S.CreateWhatsappGroup),
    RouteSpec("POST", "/chats/sendMessageText", messages.send_message_text, body=S.SendMessageText),
    # /whatsapp (whatsapp.route.js)
    RouteSpec("POST", "/whatsapp/send-message", whatsapp_flows.send_whatsapp_message, auth=False, body=S.SendWhatsAppMessage),
    RouteSpec("POST", "/whatsapp/send-otp", whatsapp_flows.send_otp_whatsapp, body=S.SendOTPWhatsapp),
    RouteSpec("POST", "/whatsapp/send-otp-direct", whatsapp_flows.send_otp_whatsapp_direct, body=S.SendOTPWhatsappDirect),
    RouteSpec("POST", "/whatsapp/send-welcome-message", whatsapp_flows.send_whatsapp_welcome_message, body=S.SendWhatsAppWelcomeMessage),
    RouteSpec("POST", "/whatsapp/validate-whatsapp", whatsapp_flows.validate_mobile, body=S.ValidateMobile),
    # /global (global.route.js)
    RouteSpec("GET", "/global/get-duplicate-employers-count", duplicates.get_duplicate_employers_count, query=S.GetDuplicateCount),
    RouteSpec("GET", "/global/get-duplicate-workers-count", duplicates.get_duplicate_workers_count, query=S.GetDuplicateCount),
    RouteSpec("GET", "/global/get-duplicate-employers", duplicates.get_duplicate_employers, query=S.GetDuplicateEmployers),
    RouteSpec("GET", "/global/get-duplicate-workers", duplicates.get_duplicate_workers, query=S.GetDuplicateWorkers),
    RouteSpec("GET", "/global/get-duplicate-jobs-count", duplicates.get_duplicate_jobs_count, query=S.GetDuplicateJobsCount),
    RouteSpec("GET", "/global/get-duplicate-jobs", duplicates.get_duplicate_jobs, query=S.GetDuplicateJobs),
    RouteSpec("GET", "/global/get-worker-previous-images", duplicates.get_worker_previous_images, query=S.GetWorkerPreviousImages),
    RouteSpec("POST", "/global/save-language-translate", duplicates.save_language_translate, body=S.SaveLanguageTranslate),
    RouteSpec("GET", "/global/get-language-translate", duplicates.get_language_translate),
]


def _translator(request: Request, query: dict[str, Any], body: Any) -> Translator:
    lang_query = query.get("lang")
    locale = guess_locale(lang_query if isinstance(lang_query, str) else None, request.headers.get("accept-language"))
    requested = request.headers.get("language") or query.get("language")
    if not requested and isinstance(body, dict):
        requested = body.get("language")
    override = resolve_override(requested)
    return Translator(override or locale)


async def _parse_upload(request: Request, body: dict[str, Any]) -> uploader.UploadedFile | None:
    """multer.memoryStorage + single("file") with the 1 MB / MIME rules."""
    if not (request.headers.get("content-type") or "").lower().startswith("multipart/form-data"):
        return None
    form = await request.form(max_files=1000, max_fields=1000)
    found: uploader.UploadedFile | None = None
    for key, value in form.multi_items():
        if hasattr(value, "filename") and hasattr(value, "read"):
            if key != "file" or found is not None:
                raise UploadError("LIMIT_UNEXPECTED_FILE")
            content = await value.read()
            if len(content) > uploader.MAX_FILE_SIZE:
                raise UploadError("LIMIT_FILE_SIZE")
            mimetype = value.content_type or "application/octet-stream"
            if mimetype not in uploader.ALLOWED_MIME_TYPES:
                continue  # fileFilter -> cb(null, false): silently skipped
            found = uploader.UploadedFile(fieldname=key, originalname=value.filename or "", encoding="7bit",
                                          mimetype=mimetype, buffer=content, size=len(content))
        else:
            body[key] = value
    return found


def make_handler(spec: RouteSpec):
    async def handler(request: Request) -> Response:
        state = request.scope.get("state", {})
        query: dict[str, Any] = dict(state.get("query") or {})
        body: Any = state.get("parsed_body")
        translator = _translator(request, query, body)
        method = request.method
        ctx = RequestContext(body=body, query=query, translator=translator)
        try:
            if spec.auth:
                ctx.user_details = await verify_request_token(request.headers.get("authorization"), translator)
            if spec.upload:
                if ctx.body is None:
                    ctx.body = {}
                ctx.file = await _parse_upload(request, ctx.body)
            if spec.query is not None:
                ctx.query = validate_segment(spec.query, "query", ctx.query)
            if spec.body is not None:
                ctx.body = validate_segment(spec.body, "body", ctx.body)
            result = await spec.service(ctx)
            if result is _NO_RESPONSE:
                # Node never answered (controller had no branch); hold until the client gives up.
                for _ in range(300):
                    if await request.is_disconnected():
                        break
                    await asyncio.sleep(1)
                return Response(status_code=504)
            payload = dict(result) if isinstance(result, dict) else {}
            if "success" not in payload:
                payload["success"] = True
            return send(200, payload, method=method, request_headers=request.headers)
        except Exception as error:  # noqa: BLE001 - central error mapping (handleError)
            status, payload = error_response_body(error, translator)
            if status >= 500:
                logger.error("request failed", {"path": spec.path, "error": f"{error.__class__.__name__}: {error}"})
            return send(status, payload, method=method, request_headers=request.headers)

    handler.__name__ = spec.name or spec.path.strip("/").replace("/", "_").replace("-", "_")
    return handler


def build_router() -> APIRouter:
    router = APIRouter()
    for spec in ROUTES:
        router.add_api_route(spec.path, make_handler(spec), methods=[spec.method], include_in_schema=True,
                             summary=f"{spec.method} {spec.path}", response_model=None)
    return router
