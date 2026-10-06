"""Generate app/socket/content_filter.py from the Node source so literals are byte-identical."""

import pathlib
import re

SRC = pathlib.Path(r"E:\xampp\htdocs\maidmatch-chat\src\services\awsSocket.service.js")
OUT = pathlib.Path(r"E:\xampp\htdocs\maidmatch-chat-python\app\socket\content_filter.py")

lines = SRC.read_text(encoding="utf-8").splitlines()


def line_with(fragment: str) -> str:
    for line in lines:
        if fragment in line:
            return line.strip()
    raise SystemExit(f"not found: {fragment}")


at_line = line_with("s = s.replace(/\\(at\\)|")
at_body = at_line.split("/", 1)[1].rsplit("/g", 1)[0]  # \(at\)|\[at\]|XXX|at
at_alt = at_body.split("|")[2]

dot_line = line_with("s = s.replace(/\\(dot\\)|")
dot_body = dot_line.split("/", 1)[1].rsplit("/g", 1)[0]
dot_parts = dot_body.split("|")
dot_alt_1, dot_alt_2 = dot_parts[3], dot_parts[4]

src_text = "\n".join(lines)
zh_hk = re.search(r"startsWith\('zh-hk'\).*?return '(.*?)';", src_text, re.S).group(1)
zh_cn = re.search(r"startsWith\('zh-cn'\).*?return '(.*?)';", src_text, re.S).group(1)
terms_block = re.search(r"const BLOCKED_TERMS = \[(.*?)\];", src_text, re.S).group(1)
terms = re.findall(r"'([^']*)'", terms_block)

TEMPLATE = r'''"""Employer<->Worker GROUP-chat content filter (port of awsSocket.service.js).

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

BLOCKED_TERMS = __TERMS__

_AT_ALT = __AT__
_DOT_ALT_1 = __DOT1__
_DOT_ALT_2 = __DOT2__

_FULLWIDTH = re.compile("[\uff01-\uff5e]")
# JS: /\s|\-|\.|_|\(|\)|\[|\]|\{|\}|<|>|\/|\\/g
_STRIP = re.compile(r"\s|-|\.|_|\(|\)|\[|\]|\{|\}|<|>|/|\\")
_AT = re.compile(r"\(at\)|\[at\]|" + re.escape(_AT_ALT) + r"|at")
_DOT = re.compile(r"\(dot\)|\[dot\]|dot|" + re.escape(_DOT_ALT_1) + "|" + re.escape(_DOT_ALT_2))
_ZERO_WIDTH = re.compile("[\u200b-\u200d\ufeff]")
_NON_DIGIT = re.compile(r"\D")

BLOCKED_MESSAGE_ZH_HK = __ZHHK__
BLOCKED_MESSAGE_ZH_CN = __ZHCN__
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
'''

code = (
    TEMPLATE.replace("__TERMS__", repr(terms))
    .replace("__AT__", ascii(at_alt))
    .replace("__DOT1__", ascii(dot_alt_1))
    .replace("__DOT2__", ascii(dot_alt_2))
    .replace("__ZHHK__", ascii(zh_hk))
    .replace("__ZHCN__", ascii(zh_cn))
)
OUT.write_text(code, encoding="utf-8")
print("terms", len(terms), "at", ascii(at_alt), "dot", ascii(dot_alt_1), ascii(dot_alt_2))
