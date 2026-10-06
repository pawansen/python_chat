"""Build tests/fixtures/validation_corpus.json: inputs run through BOTH the
original celebrate validators (generate_node_fixtures.mjs) and the Pydantic
port (tests/unit/test_validation_parity.py)."""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

# JS export name -> (segment, Python model name, valid baseline)
VALIDATORS = {
    "createConversation": ("body", "CreateConversation", {"platform": "app", "friend_id": 5, "friend_type": "worker"}),
    "createConversationGroup": ("body", "CreateConversationGroup", {"platform": "app", "id": 5, "friend_type": "group_team", "group_type": "job"}),
    "validateChangeUserGroupCsInterpreter": ("body", "ValidateChangeUserGroupCsInterpreter", {"user_id": 1, "user_type": "cs", "group_id": 2}),
    "conversationList": ("body", "ConversationList", {"limit": 10, "page": 1, "platform": "app", "actionType": "unread", "search": ""}),
    "conversactions": ("body", "Conversactions", {"limit": 10, "page": 1, "user_id": 3}),
    "addActionRequired": ("body", "AddActionRequired", {"user_id": 1, "entity_id": 2, "entity_type": "worker", "action_type": "x", "content": "y"}),
    "getActionList": ("query", "GetActionList", {"limit": "10", "page": "1", "user_id": "2"}),
    "messageListAws": ("body", "MessageListAws", {"sid": 1, "limit": 10, "ExclusiveStartKey": {"id": 1}, "platform": ""}),
    "chatHistoryList": ("body", "ChatHistoryList", {"user_id": 1, "limit": 5, "group_id": "1,2", "startDate": ""}),
    "readAllMessageAws": ("body", "ReadAllMessageAws", {"sid": "12"}),
    "addRemoveGroupParticipant": ("body", "AddRemoveGroupParticipant", {"sid": "CH1", "groupId": 1, "userId": 2, "action": "ADD"}),
    "fetchConversation": ("body", "FetchConversation", {"sid": "CH1"}),
    "getMessageById": ("body", "GetMessageById", {"sid": "a", "msid": "b"}),
    "allMessage": ("body", "AllMessage", {"sid": "a", "limit": 1}),
    "updateReadMessageCount": ("body", "UpdateReadMessageCount", {"sid": "a"}),
    "fetchAllParticipant": ("body", "FetchAllParticipant", {"sid": "a", "limit": 1}),
    "getParticipantById": ("body", "GetParticipantById", {"sid": "a", "psid": "b"}),
    "createWhatsappGroup": ("body", "CreateWhatsappGroup", {"friendly_name": "f", "type": "GROUP", "platform": 2, "image": None}),
    "sendMessageText": ("body", "SendMessageText", {"message": "m", "phone_number": "+1"}),
    "sendTemplateMessage": ("body", "SendTemplateMessage", {"platform": "app", "friend_id": 1, "template_content": "t", "entity_id": "", "sender_id": None}),
    "sendWhatsAppTemplateMessage": ("body", "SendWhatsAppTemplateMessage", {"platform": "whatsapp", "friend_id": 1, "template_id": "e1"}),
    "sendTemplateMessageAssignPartner": ("body", "SendTemplateMessageAssignPartner",
                                         {"platform": "whatsapp", "friend_id": 1, "template_content": "t", "worker_name": "w", "nationality": "n", "reference_id": "r"}),
    "sendTemplateMessageActionRequired": ("body", "SendTemplateMessageActionRequired", {"user_id": 1, "reason": 2, "language": "zh_hant", "action_module": "passport"}),
    "sendChatMessage": ("body", "SendChatMessage", {"platform": "app", "user_id": 1, "user_type": "worker", "message": "m"}),
    "sendOTPWhatsapp": ("body", "SendOTPWhatsapp", {"language": "en", "user_id": 1, "otp": 1234}),
    "sendOTPWhatsappDirect": ("body", "SendOTPWhatsappDirect", {"language": "id", "whatsapp_country_code": "+62", "whatsapp_number": "812", "otp": "99"}),
    "sendWelcomeMessageWhatsapp": ("body", "SendWelcomeMessageWhatsapp", {"language": "zh_cn", "whatsapp_country_code": "+86", "whatsapp_number": 1}),
    "sendWhatsAppMessage": ("body", "SendWhatsAppMessage", {"user_id": 1, "message": "m"}),
    "getDuplicateEmployers": ("query", "GetDuplicateEmployers", {"limit": "10", "page": "1", "user_id": "5"}),
    "getDuplicateWorkers": ("query", "GetDuplicateWorkers", {"limit": "10", "page": "1", "user_id": "5"}),
    "getDuplicateCount": ("query", "GetDuplicateCount", {"user_id": "5"}),
    "getDuplicateJobsCount": ("query", "GetDuplicateJobsCount", {"employer_id": "1,2"}),
    "getDuplicateJobs": ("query", "GetDuplicateJobs", {"limit": "10", "page": "1", "employer_id": "1"}),
    "getWorkerPreviousImages": ("query", "GetWorkerPreviousImages", {"user_id": "5"}),
    "sendWhatsAppWelcomeMessage": ("body", "SendWhatsAppWelcomeMessage", {"user_id": 1}),
    "validateMobile": ("body", "ValidateMobile", {"whatsapp_country_code": "+852", "whatsapp_number": 9}),
    "sendWhatsAppTemplateNotification": ("body", "SendWhatsAppTemplateNotification",
                                          {"language": "en", "user_id": 1, "user_type": "worker", "message_type": "job_apply", "sender_id": ""}),
    "saveLanguageTranslate": ("body", "SaveLanguageTranslate", {"chat_translate_view": "both", "chat_translate_language": "zh_tw"}),
}

ODD_VALUES = ["", None, "abc", " 12 ", "1e3", "0x10", True, 1.5, [1], {"a": 1}, 2**60, "-0", "  "]


def build() -> list[dict]:
    cases = []
    for name, (segment, model, base) in VALIDATORS.items():
        cases.append({"validator": name, "model": model, "segment": segment, "value": base})
        cases.append({"validator": name, "model": model, "segment": segment, "value": {}})
        cases.append({"validator": name, "model": model, "segment": segment, "value": {**base, "zzz_unknown": 1}})
        cases.append({"validator": name, "model": model, "segment": segment, "value": {"zzz_unknown": 1, **base}})
        for key in base:
            cases.append({"validator": name, "model": model, "segment": segment, "value": {k: v for k, v in base.items() if k != key}})
            for odd in ODD_VALUES:
                cases.append({"validator": name, "model": model, "segment": segment, "value": {**base, key: odd}})
    # conditional pid rule
    for extra in ({"action": "REMOVE"}, {"action": "REMOVE", "pid": ""}, {"action": "REMOVE", "pid": "PX"}, {"action": "ADD", "pid": ""}):
        value = {"sid": "s", "groupId": 1, "userId": 2, **extra}
        cases.append({"validator": "addRemoveGroupParticipant", "model": "AddRemoveGroupParticipant", "segment": "body", "value": value})
    cases.append({"validator": "createConversation", "model": "CreateConversation", "segment": "body", "value": [1, 2]})
    cases.append({"validator": "createConversation", "model": "CreateConversation", "segment": "body", "value": "__undefined__"})
    cases.append({"validator": "createConversation", "model": "CreateConversation", "segment": "body", "value": {"a<b": 1}})
    return cases


if __name__ == "__main__":
    corpus = build()
    (HERE / "validation_corpus.json").write_text(json.dumps(corpus, ensure_ascii=False), encoding="utf-8")
    print(len(corpus), "cases")
