"""Employer<->Worker GROUP-chat content filter (port of awsSocket.service.js).

LO-14: BEST-EFFORT CONTENT-MODERATION BUSINESS RULE, NOT a security control.

GENERATED from the Node source (scripts/gen_content_filter.py) so every literal
is byte-identical. The Node source file had been double-encoded (UTF-8 bytes
read as cp1252 and saved again), so the extra "at"/"dot" alternatives and the
zh-HK / zh-CN refusal messages are MOJIBAKE at runtime — Chinese users
currently receive garbled text. Preserved deliberately for behaviour parity;
see MIGRATION_REPORT.md §11 for the recommended fix.
"""

from __future__ import annotations

import re

BLOCKED_TERMS = ['@', 'email', 'e-mail', 'mail', 'gmail', 'yahoo', 'hotmail', 'outlook', 'icloud', 'protonmail', 'live', 'msn', 'qq', '163', '126', 'http', 'https', 'www', 'link', 'url', '.com', '.net', '.org', '.hk', '.cn', '.tw', '.io', '.me', 'bit.ly', 'tinyurl', 'linktr.ee', 't.co', '+852', '852', 'phone', 'contact', 'call', 'sms', 'text', 'message', 'dm', 'pm', 'exchange', 'address', 'location', 'whatsapp', 'wechat', 'line', 'telegram', 'signal', 'viber', 'kakao', 'skype', 'facetime', 'instagram', 'ig', 'facebook', 'fb', 'messenger', 'tiktok', 'snap', 'snapchat', 'threads', 'twitter', 'x', 'starbucks', 'cafe', 'coffee', 'mtr', 'station', 'exit', 'lobby', 'mall', '7-11', '711', 'circle k', 'mcd', 'mcdonald']

_AT_ALT = '\xc3\xaf\xc2\xbc\xc2\xa0'
_DOT_ALT_1 = '\xc3\xa9\xc2\xbb\xc5\xbe'
_DOT_ALT_2 = '\xc3\xa7\xe2\u20ac\u0161\xc2\xb9'

_FULLWIDTH = re.compile("[\uff01-\uff5e]")
# JS: /\s|\-|\.|_|\(|\)|\[|\]|\{|\}|<|>|\/|\\/g
_STRIP = re.compile(r"\s|-|\.|_|\(|\)|\[|\]|\{|\}|<|>|/|\\")
_AT = re.compile(r"\(at\)|\[at\]|" + re.escape(_AT_ALT) + r"|at")
_DOT = re.compile(r"\(dot\)|\[dot\]|dot|" + re.escape(_DOT_ALT_1) + "|" + re.escape(_DOT_ALT_2))
_ZERO_WIDTH = re.compile("[\u200b-\u200d\ufeff]")
_NON_DIGIT = re.compile(r"\D")

BLOCKED_MESSAGE_ZH_HK = '\xc3\xa5\xc5\xb8\xc2\xba\xc3\xa6\xe2\u20ac\u201c\xc2\xbc\xc3\xa5\xc2\xae\xe2\u20ac\xb0\xc3\xa5\xe2\u20ac\xa6\xc2\xa8\xc3\xa5\xc2\x8f\xc5\xa0\xc3\xa6\xe2\u20ac\x9d\xc2\xbf\xc3\xa7\xc2\xad\xe2\u20ac\u201c\xc3\xa5\xc5\xbd\xc5\xb8\xc3\xa5\xe2\u20ac\xba\xc2\xa0\xc3\xaf\xc2\xbc\xc5\u2019\xc3\xa4\xc2\xb8\xc2\x8d\xc3\xa5\xe2\u20ac\xa6\xc2\x81\xc3\xa8\xc2\xa8\xc2\xb1\xc3\xa5\xcb\u2020\xe2\u20ac\xa0\xc3\xa4\xc2\xba\xc2\xab\xc3\xa8\xc2\x81\xc2\xaf\xc3\xa7\xc2\xb5\xc2\xa1\xc3\xa8\xc2\xb3\xe2\u20ac\xa1\xc3\xa6\xe2\u20ac\u201c\xe2\u201e\xa2\xc3\xaf\xc2\xbc\xcb\u2020\xc3\xa9\xe2\u20ac\xba\xc2\xbb\xc3\xa8\xc2\xa9\xc2\xb1\xc3\xaf\xc2\xbc\xc2\x8f\xc3\xa9\xe2\u20ac\xba\xc2\xbb\xc3\xa9\xc6\u2019\xc2\xb5\xc3\xaf\xc2\xbc\xc2\x8f\xc3\xa7\xc2\xa4\xc2\xbe\xc3\xa4\xc2\xba\xc2\xa4\xc3\xa5\xc2\xb9\xc2\xb3\xc3\xa5\xc2\x8f\xc2\xb0\xc3\xaf\xc2\xbc\xe2\u20ac\xb0\xc3\xa6\xcb\u2020\xe2\u20ac\u201c\xc3\xa5\xc2\xae\xe2\u20ac\xb0\xc3\xa6\xc5\xbd\xe2\u20ac\u2122\xc3\xa5\xc2\xb9\xc2\xb3\xc3\xa5\xc2\x8f\xc2\xb0\xc3\xa5\xc2\xa4\xe2\u20ac\u201c\xc3\xa8\xc2\xa6\xe2\u20ac\xb9\xc3\xa9\xc2\x9d\xc2\xa2\xc3\xa3\xe2\u201a\xac\xe2\u20ac\u0161'
BLOCKED_MESSAGE_ZH_CN = '\xc3\xa5\xe2\u20ac\xa1\xc2\xba\xc3\xa4\xc2\xba\xc5\xbd\xc3\xa5\xc2\xae\xe2\u20ac\xb0\xc3\xa5\xe2\u20ac\xa6\xc2\xa8\xc3\xa5\xc2\x8f\xc5\xa0\xc3\xa6\xe2\u20ac\x9d\xc2\xbf\xc3\xa7\xc2\xad\xe2\u20ac\u201c\xc3\xa5\xc5\xbd\xc5\xb8\xc3\xa5\xe2\u20ac\xba\xc2\xa0\xc3\xaf\xc2\xbc\xc5\u2019\xc3\xa4\xc2\xb8\xc2\x8d\xc3\xa5\xe2\u20ac\xa6\xc2\x81\xc3\xa8\xc2\xae\xc2\xb8\xc3\xa5\xcb\u2020\xe2\u20ac\xa0\xc3\xa4\xc2\xba\xc2\xab\xc3\xa8\xc2\x81\xe2\u20ac\x9d\xc3\xa7\xc2\xb3\xc2\xbb\xc3\xa6\xe2\u20ac\u201c\xc2\xb9\xc3\xa5\xc2\xbc\xc2\x8f\xc3\xaf\xc2\xbc\xcb\u2020\xc3\xa7\xe2\u20ac\x9d\xc2\xb5\xc3\xa8\xc2\xaf\xc2\x9d/\xc3\xa9\xe2\u20ac\u0161\xc2\xae\xc3\xa7\xc2\xae\xc2\xb1/\xc3\xa7\xc2\xa4\xc2\xbe\xc3\xa4\xc2\xba\xc2\xa4\xc3\xa5\xc2\xb9\xc2\xb3\xc3\xa5\xc2\x8f\xc2\xb0\xc3\xaf\xc2\xbc\xe2\u20ac\xb0\xc3\xa6\xcb\u2020\xe2\u20ac\u201c\xc3\xa5\xc2\xae\xe2\u20ac\xb0\xc3\xa6\xc5\xbd\xe2\u20ac\u2122\xc3\xa5\xc2\xb9\xc2\xb3\xc3\xa5\xc2\x8f\xc2\xb0\xc3\xa5\xc2\xa4\xe2\u20ac\u201c\xc3\xa8\xc2\xa7\xc2\x81\xc3\xa9\xc2\x9d\xc2\xa2\xc3\xa3\xe2\u201a\xac\xe2\u20ac\u0161'
BLOCKED_MESSAGE_ID = (
    "Demi keamanan dan kebijakan, berbagi detail kontak (telepon/email/media sosial) "
    "atau mengatur pertemuan di luar platform tidak diperbolehkan."
)
BLOCKED_MESSAGE_EN = (
    "For safety and policy reasons, sharing contact details (phone/email/social) "
    "or arranging off-platform meetings isn't allowed."
)


def to_ascii(text: str) -> str:
    return _FULLWIDTH.sub(lambda m: chr(ord(m.group(0)) - 0xFEE0), text).replace("\u3000", " ")


def normalize_tight(text: str) -> str:
    s = text.lower()
    s = to_ascii(s)
    s = _STRIP.sub("", s)
    s = _AT.sub("@", s)
    s = _DOT.sub(".", s)
    s = _ZERO_WIDTH.sub("", s)
    return s


def has_blocked_term(raw: str, norm: str) -> bool:
    raw_lower = raw.lower()
    for term in BLOCKED_TERMS:
        if term in raw_lower or term in norm:
            return True
    digits = _NON_DIGIT.sub("", norm)
    return len(digits) >= 7


def get_blocked_message(locale: object) -> str:
    lang = (locale if isinstance(locale, str) and locale else "en").lower()
    if lang.startswith("zh-hk") or lang.startswith("zh-hant"):
        return BLOCKED_MESSAGE_ZH_HK
    if lang.startswith("zh-cn") or lang.startswith("zh-hans"):
        return BLOCKED_MESSAGE_ZH_CN
    if lang.startswith("id"):
        return BLOCKED_MESSAGE_ID
    return BLOCKED_MESSAGE_EN
