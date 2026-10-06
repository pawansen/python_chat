"""Request schemas (port of src/validations/chat.validation.js).

One Pydantic v2 model per celebrate validator that is mounted on a route.
Field order matches the Joi object (it decides which error is reported first).
Optional fields default to ``ABSENT`` and are never added to the request.
"""

from __future__ import annotations

from typing import Any

from app.schemas.joi import ABSENT, JoiModel, joi_any, joi_number, joi_string

PLATFORMS = ("app", "whatsapp", "wechat", "action_required", "line")
FRIEND_TYPES = ("employer", "user", "worker", "processing", "partner", "group_team")

Num = joi_number()
Str = joi_string()
AnyV = joi_any()


class CreateConversation(JoiModel):
    platform: joi_string(valid=PLATFORMS)
    friend_id: Num
    friend_type: Str


class CreateConversationGroup(JoiModel):
    platform: joi_string(valid=PLATFORMS)
    id: Num
    group_type: joi_string(valid=("job", "team")) = ABSENT
    friend_type: Str
    job_worker_id: Num = ABSENT
    job_interview_id: Num = ABSENT


class ValidateChangeUserGroupCsInterpreter(JoiModel):
    user_id: Num
    group_id: Num = ABSENT
    job_id: Num = ABSENT
    job_stage_id: Num = ABSENT
    employer_id: Num = ABSENT
    user_type: joi_string(valid=("cs", "interpreter"))


class ConversationList(JoiModel):
    limit: Num
    page: Num
    user_id: AnyV = ABSENT
    search: AnyV = ABSENT
    friend_type: AnyV = ABSENT
    actionType: joi_string(valid=("all", "not_replied", "unread"), allow=("", None)) = ABSENT
    platform: joi_string(valid=PLATFORMS)


class Conversactions(JoiModel):
    limit: Num
    page: Num
    user_id: Num
    search: AnyV = ABSENT


class AddActionRequired(JoiModel):
    user_id: Num
    entity_id: Num
    entity_type: joi_string(valid=("worker", "employer"))
    action_type: Str
    content: Str


class GetActionList(JoiModel):
    limit: Num
    page: Num
    user_id: Num
    action_type: AnyV = ABSENT


class MessageListAws(JoiModel):
    sid: Num
    limit: Num
    ExclusiveStartKey: AnyV = ABSENT
    platform: AnyV = ABSENT
    action_type: AnyV = ABSENT
    only_action_required: AnyV = ABSENT


class ChatHistoryList(JoiModel):
    user_id: Num
    to_user_id: AnyV = ABSENT
    group_id: AnyV = ABSENT
    limit: Num
    ExclusiveStartKey: AnyV = ABSENT
    platform: AnyV = ABSENT
    startDate: AnyV = ABSENT
    endDate: AnyV = ABSENT
    search: AnyV = ABSENT
    page: AnyV = ABSENT


class ReadAllMessageAws(JoiModel):
    sid: Num


def _pid_when(raw: dict[str, Any]) -> str | None:
    """``pid: Joi.string().when("action", {is: "REMOVE", then: required, otherwise: optional().allow("")})``."""
    if raw.get("action") != "REMOVE":
        return None
    if "pid" not in raw:
        return "any.required"
    if raw.get("pid") == "":
        return "string.empty"
    return None


class AddRemoveGroupParticipant(JoiModel):
    sid: Str
    groupId: Num
    userId: Num
    pid: joi_string(allow=("",)) = ABSENT
    action: joi_string(valid=("ADD", "REMOVE"))

    __joi_conditions__ = (("pid", _pid_when),)


class FetchConversation(JoiModel):
    sid: Str


class GetMessageById(JoiModel):
    sid: Str
    msid: Str


class AllMessage(JoiModel):
    sid: Str
    limit: Num


class UpdateReadMessageCount(JoiModel):
    sid: Str


class FetchAllParticipant(JoiModel):
    sid: Str
    limit: Num


class GetParticipantById(JoiModel):
    sid: Str
    psid: Str


class CreateWhatsappGroup(JoiModel):
    friendly_name: Str
    type: Str
    platform: Num
    image: joi_string(allow=(None, "")) = ABSENT


class SendMessageText(JoiModel):
    message: Str
    phone_number: Str


class SendTemplateMessage(JoiModel):
    platform: joi_string(valid=PLATFORMS)
    friend_id: Num
    friend_type: joi_string(valid=FRIEND_TYPES) = ABSENT
    template_content: Str
    entity_id: joi_number(allow=(None, "")) = ABSENT
    entity_type: joi_string(allow=(None, "")) = ABSENT
    template_file_path: joi_string(allow=(None, "")) = ABSENT
    sender_id: joi_number(allow=(None, "")) = ABSENT


class SendWhatsAppTemplateMessage(JoiModel):
    platform: joi_string(valid=("whatsapp",))
    friend_id: Num
    friend_type: joi_string(valid=FRIEND_TYPES) = ABSENT
    template_content: joi_string(allow=(None, "")) = ABSENT
    template_id: Str
    sender_id: joi_number(allow=(None, "")) = ABSENT
    custom_content_params: joi_string(allow=(None, "")) = ABSENT


class SendTemplateMessageAssignPartner(JoiModel):
    platform: joi_string(valid=PLATFORMS)
    friend_id: Num
    friend_type: joi_string(valid=FRIEND_TYPES) = ABSENT
    template_content: Str
    entity_id: joi_string(allow=(None, "")) = ABSENT
    entity_type: joi_string(allow=(None, "")) = ABSENT
    template_file_path: joi_string(allow=(None, "")) = ABSENT
    sender_id: joi_number(allow=(None, "")) = ABSENT
    worker_name: Str
    nationality: Str
    reference_id: Str


class SendTemplateMessageActionRequired(JoiModel):
    user_id: Num
    reason: Num
    # zh_hant/zh_chs are the pre-rename names, still accepted so existing clients keep working.
    language: joi_string(valid=("zh_HK", "zh_CN", "zh_hant", "zh_chs", "id", "en"))
    action_module: joi_string(valid=("passport", "birth_certificate"))


class SendChatMessage(JoiModel):
    platform: joi_string(valid=PLATFORMS)
    user_id: Num
    user_type: joi_string(valid=("employer", "user", "worker")) = ABSENT
    message: Str


class SendOTPWhatsapp(JoiModel):
    language: joi_string(valid=("zh_hk", "zh_cn", "id", "en"))
    user_id: Num
    otp: Num


class SendOTPWhatsappDirect(JoiModel):
    language: joi_string(valid=("zh_hk", "zh_cn", "id", "en"))
    whatsapp_country_code: Str
    whatsapp_number: Num
    otp: Num


class SendWelcomeMessageWhatsapp(JoiModel):
    language: joi_string(valid=("zh_hk", "zh_cn", "id", "en"))
    whatsapp_country_code: Str
    whatsapp_number: Num


class SendWhatsAppMessage(JoiModel):
    user_id: Num
    message: Str


class GetDuplicateEmployers(JoiModel):
    limit: Num
    page: Num
    user_id: Num


class GetDuplicateWorkers(JoiModel):
    limit: Num
    page: Num
    user_id: Num


class GetDuplicateCount(JoiModel):
    user_id: Num


class GetDuplicateJobsCount(JoiModel):
    employer_id: Str


class GetDuplicateJobs(JoiModel):
    limit: Num
    page: Num
    employer_id: Str


class GetWorkerPreviousImages(JoiModel):
    user_id: Num


class SendWhatsAppWelcomeMessage(JoiModel):
    user_id: Num


class ValidateMobile(JoiModel):
    whatsapp_country_code: Str
    whatsapp_number: Num


WHATSAPP_NOTIFICATION_TYPES = (
    "job_apply",
    "welcome_message_template",
    "job_matched",
    "apply_job_after_suggestion",
    "upcoming_interview_instant",
    "reschedule_request_send_by_worker",
    "interview_cancelled_by_worker",
    "job_opened_after_4_days_notification_to_employer",
    "notification_to_worker_for_not_completing_registration",
    "notification_to_worker_for_not_uploading_photo",
    "visa_expiry_date_expired",
    "contract_end_date_expired",
    "upcoming_interview_24_hour_prior",
    "job_offer_expire_soon",
)


class SendWhatsAppTemplateNotification(JoiModel):
    # zh_chs/zh_hant are the pre-rename names, still accepted so existing clients keep working.
    language: joi_string(valid=("zh_CN", "zh_HK", "zh_chs", "zh_hant", "id", "en"))
    user_id: Num
    user_type: joi_string(valid=FRIEND_TYPES) = ABSENT
    message_type: joi_string(valid=WHATSAPP_NOTIFICATION_TYPES) = ABSENT
    sender_id: joi_number(allow=(None, "")) = ABSENT
    custom_content_params: joi_string(allow=(None, "")) = ABSENT


class SaveLanguageTranslate(JoiModel):
    chat_translate_view: joi_string(valid=("original", "translated", "both"))
    chat_translate_language: joi_string(valid=("ch", "id", "en", "zh_cn", "zh_tw"))
