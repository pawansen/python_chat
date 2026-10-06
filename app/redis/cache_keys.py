"""Central cache-key registry (port of src/config/cache.keys.config.js).

The ONLY place a Redis key string is assembled. Key formats are identical to
the Node service so both implementations can share one Redis during a
cut-over (a key and the SCAN pattern that invalidates it are generated from
the same prefix and can never drift apart). py_chat-chat is single-tenant:
the service prefix is ``chat:`` and nothing else.

``cache_key_hash`` reproduces ``sha1(JSON.stringify(value))`` byte-for-byte
(see :mod:`app.core.jsonx`), so hashed key segments match too.
"""

from __future__ import annotations

import hashlib
from typing import Any

from app.core.js import UNDEFINED
from app.core.jsonx import stringify


def cache_key_hash(value: Any) -> str:
    """Deterministic sha1 for key segments (attrs / param variants)."""
    if isinstance(value, str):
        material = value
    else:
        material = stringify(None if value is None or value is UNDEFINED else value)
    return hashlib.sha1(material.encode("utf-8")).hexdigest()


CONVERSATION_LIST_USER_PREFIX = "chat:conversation:list:user"
CONVERSATION_LIST_ALL_PREFIX = "chat:conversation:list:all"
CONVERSATION_LIST_APP_PREFIX = "chat:conversation:list:app"
MESSAGE_LIST_PREFIX = "chat:message:list"
USER_DETAIL_PREFIX = "chat:user:detail"
GROUP_DETAIL_PREFIX = "chat:group:detail"
PARTICIPANT_DETAIL_PREFIX = "chat:participant:detail"
PARTICIPANT_LIST_PREFIX = "chat:participant:list"


def _s(value: Any) -> str:
    from app.core.js import js_string

    return js_string(value)


# ── Version counters (VERSION tier: INCR + EXPIRE) ──────────────────────────
def version_message_key(group_id: Any) -> str:
    return f"chat:version:message:{_s(group_id)}"


def version_conversation_key(user_id: Any) -> str:
    return f"chat:version:conversation:{_s(user_id)}"


# ── Conversation lists (per user, version-bump invalidated) ─────────────────
def conversation_list_user_key(user_id: Any, version: Any, digest: str) -> str:
    return f"{CONVERSATION_LIST_USER_PREFIX}:{_s(user_id)}:v{_s(version)}:{digest}"


def conversation_list_all_key(user_id: Any, version: Any, digest: str) -> str:
    return f"{CONVERSATION_LIST_ALL_PREFIX}:{_s(user_id)}:v{_s(version)}:{digest}"


def conversation_list_app_key(user_id: Any, version: Any, digest: str) -> str:
    return f"{CONVERSATION_LIST_APP_PREFIX}:{_s(user_id)}:v{_s(version)}:{digest}"


# ── Message lists (per group, version-bump invalidated) ─────────────────────
def message_list_key(group_id: Any, version: Any, digest: str) -> str:
    return f"{MESSAGE_LIST_PREFIX}:{_s(group_id)}:v{_s(version)}:{digest}"


def message_list_pattern(group_id: Any) -> str:
    return f"{MESSAGE_LIST_PREFIX}:{_s(group_id)}:*"


# ── User detail (user_data_view projection) ─────────────────────────────────
def user_detail_key(user_id: Any, attrs_hash: str) -> str:
    return f"{USER_DETAIL_PREFIX}:{_s(user_id)}:{attrs_hash}"


def user_detail_pattern(user_id: Any) -> str:
    return f"{USER_DETAIL_PREFIX}:{_s(user_id)}:*"


# ── Group detail ────────────────────────────────────────────────────────────
def group_detail_key(group_id: Any, attrs_hash: str) -> str:
    return f"{GROUP_DETAIL_PREFIX}:{_s(group_id)}:{attrs_hash}"


def group_detail_pattern(group_id: Any) -> str:
    return f"{GROUP_DETAIL_PREFIX}:{_s(group_id)}:*"


def group_detail_filter_key(filter_hash: str) -> str:
    return f"{GROUP_DETAIL_PREFIX}:filter:{filter_hash}"


# ── Participants (group-scoped) ─────────────────────────────────────────────
def participant_detail_key(group_id: Any, user_id: Any, digest: str) -> str:
    return f"{PARTICIPANT_DETAIL_PREFIX}:{_s(group_id)}:{_s(user_id)}:{digest}"


def participant_detail_pattern(group_id: Any) -> str:
    return f"{PARTICIPANT_DETAIL_PREFIX}:{_s(group_id)}:*"


def participant_list_key(group_id: Any, digest: str) -> str:
    return f"{PARTICIPANT_LIST_PREFIX}:{_s(group_id)}:{digest}"


def participant_list_pattern(group_id: Any) -> str:
    return f"{PARTICIPANT_LIST_PREFIX}:{_s(group_id)}:*"


# ── Translations (content-addressed, immutable) ─────────────────────────────
def translation_key(text_hash: str, lang_code: str) -> str:
    return f"chat:translation:{text_hash}:{lang_code}"


# ── /global duplicate-detection reads (TTL-only) ────────────────────────────
def duplicate_key(entity: str, digest: str) -> str:
    return f"chat:duplicate:{entity}:{digest}"


# ── WhatsApp template list (provider-owned, TTL-only) ───────────────────────
def whatsapp_template_list_key(digest: str) -> str:
    return f"chat:whatsapp:template:list:{digest}"


# ── Auth: fleet-wide revocation list, deliberately NOT service-prefixed ─────
def jwt_blocklist_key(prefix: str, jti: Any) -> str:
    return f"{prefix}{_s(jti)}"
