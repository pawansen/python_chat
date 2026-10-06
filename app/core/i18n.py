"""Localization (port of the ``i18n`` npm package configuration in server.js).

Behaviour reproduced from i18n@0.15 as configured by the Node service
(locales en/id/zh_CN/zh_HK, defaultLocale en, queryParameter "lang",
objectNotation, updateFiles false) — verified against the real package:

* a missing key returns the key itself (no fallback to the default locale);
* ``?lang=`` is lower-cased before matching, so only ``en``/``id`` can match it;
* ``Accept-Language`` is matched exactly, then by its primary subtag
  (``id-ID`` -> ``id``; ``zh-HK`` does NOT select ``zh_HK``);
* server.js then lets the ``language`` header/query/body override the locale,
  mapping the legacy codes (zh_hant, zh-hk, ...) onto the renamed files.

Socket handlers and service code that used the global ``i18n.__`` always ran
in the default locale (English); :func:`t_default` mirrors that.
The catalogs are mutable so the LCC OTA updater can merge fresh strings in.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from app.core.config import APP_DIR

LOCALES = ["en", "id", "zh_CN", "zh_HK"]
DEFAULT_LOCALE = "en"
LOCALES_DIR = APP_DIR / "assets" / "locales"

LOCALE_ALIASES = {
    "zh_hant": "zh_HK",
    "zh_chs": "zh_CN",
    "zh-hk": "zh_HK",
    "zh-cn": "zh_CN",
    "zh-hant": "zh_HK",
    "zh-hans": "zh_CN",
}

_lock = threading.Lock()
_catalogs: dict[str, dict[str, Any]] = {}


def _read_locale(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if text.startswith("﻿"):
        text = text[1:]
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def load_catalogs(directory: Path | None = None) -> None:
    base = directory or LOCALES_DIR
    with _lock:
        for locale in LOCALES:
            _catalogs[locale] = _read_locale(base / f"{locale}.json")


def get_catalog(locale: str) -> dict[str, Any] | None:
    if not _catalogs:
        load_catalogs()
    return _catalogs.get(locale)


def _lookup(catalog: dict[str, Any], key: str) -> Any:
    if key in catalog:
        return catalog[key]
    # objectNotation: "a.b" traverses nested objects (only when such nesting exists)
    if "." in key and not key.endswith(".") and not key.startswith("."):
        node: Any = catalog
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node if isinstance(node, str) else None
    return None


def translate(key: Any, locale: str | None = None) -> Any:
    """``req.__(key)`` / ``i18n.__(key)``."""
    if not isinstance(key, str):
        return key
    catalog = get_catalog(locale or DEFAULT_LOCALE) or {}
    value = _lookup(catalog, key)
    return value if isinstance(value, str) else key


def t_default(key: str) -> str:
    """Global ``i18n.__`` (socket handlers / FCM titles): always the default locale."""
    return translate(key, DEFAULT_LOCALE)


def _accepted_languages(header: str) -> list[str]:
    parsed: list[tuple[str, float, int]] = []
    for index, part in enumerate(header.split(",")):
        piece = part.strip()
        if not piece:
            continue
        lang, _, params = piece.partition(";")
        quality = 1.0
        params = params.strip()
        if params.startswith("q="):
            try:
                quality = float(params[2:])
            except ValueError:
                quality = 0.0
        parsed.append((lang.strip(), quality, index))
    parsed.sort(key=lambda item: (-item[1], item[2]))
    return [lang for lang, quality, _ in parsed if quality > 0]


def guess_locale(query_lang: str | None, accept_language: str | None) -> str:
    """i18n.init's guessLanguage for this configuration."""
    if query_lang:
        candidate = str(query_lang).lower()
        if candidate in LOCALES:
            return candidate
    if accept_language:
        match = None
        fallback = None
        for lang in _accepted_languages(accept_language):
            parent = lang.split("-", 1)[0]
            if lang in LOCALES:
                match = lang
                break
            if fallback is None and parent in LOCALES:
                fallback = parent
        chosen = match or fallback
        if chosen:
            return chosen
    return DEFAULT_LOCALE


def resolve_override(requested: Any) -> str | None:
    """server.js locale middleware: ``language`` header/query/body with alias mapping."""
    if requested is None or requested == "" or requested is False:
        return None
    text = requested if isinstance(requested, str) else str(requested)
    lang = LOCALE_ALIASES.get(text.lower(), text)
    return lang if lang in LOCALES else None


class Translator:
    """Bound ``req.__`` for one request."""

    __slots__ = ("locale",)

    def __init__(self, locale: str = DEFAULT_LOCALE) -> None:
        self.locale = locale

    def __call__(self, key: Any) -> Any:
        return translate(key, self.locale)

    def get_locale(self) -> str:
        return self.locale
