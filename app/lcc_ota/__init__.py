"""LCC OTA localization (port of src/core/lcc-ota/*).

Pulls this service's strings (namespace ``chat``, surface ``admin-backend``)
from the LCC's published bucket at startup and every LCC_OTA_POLL_SECONDS:

  GET <base>/i18n/<env>/admin-backend/manifest.json  (If-None-Match -> 304)
  -> changed (version or sha256)? download EVERY mapped language, verify
     sha256, keep ``chat.*`` keys (prefix stripped) -> only then rewrite
     app/assets/locales/<locale>.json atomically and merge into the live
     catalog -> remember {version, etag, sha256, appliedAt}.

Never raises, never blocks boot; any failure keeps the current strings.
Env vars are identical to Node (LCC_OTA_ENABLED, LANGUAGE_PULL_FROM_SERVER,
LCC_OTA_BASE_URL, LCC_OTA_POLL_SECONDS, LCC_OTA_TIMEOUT_MS, LCC_OTA_CACHE_DIR,
LCC_OTA_LOCALES_DIR).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.core import i18n
from app.core.js import iso_now

SURFACE = "admin-backend"
NAMESPACE = "chat"
LANGUAGE_MAP = {"en": "en", "zh-HK": "zh_HK", "zh-CN": "zh_CN"}
DEFAULT_BASE_URL = "https://storage.googleapis.com/py_chat-bffb1.firebasestorage.app"
DEFAULT_POLL_SECONDS = 300
DEFAULT_TIMEOUT_MS = 8000


@dataclass
class OtaConfig:
    enabled: bool
    environment: str
    base_url: str
    poll_seconds: float
    timeout_ms: float
    cache_dir: str
    locales_dir: str


def _bool(value: Any, fallback: bool) -> bool:
    if value is None or value == "":
        return fallback
    return str(value).strip().lower() not in ("false", "0", "no", "off")


def _num(value: Any, fallback: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return fallback
    return n if n >= 0 and n == n and n != float("inf") else fallback


def read_config(env: dict[str, str] | None = None) -> OtaConfig:
    env = dict(os.environ) if env is None else env
    return OtaConfig(
        enabled=_bool(env.get("LCC_OTA_ENABLED"), True),
        environment=(env.get("LANGUAGE_PULL_FROM_SERVER") or "staging").strip(),
        base_url=re.sub(r"/+$", "", env.get("LCC_OTA_BASE_URL") or DEFAULT_BASE_URL),
        poll_seconds=_num(env.get("LCC_OTA_POLL_SECONDS"), DEFAULT_POLL_SECONDS),
        timeout_ms=_num(env.get("LCC_OTA_TIMEOUT_MS"), DEFAULT_TIMEOUT_MS) or DEFAULT_TIMEOUT_MS,
        cache_dir=env.get("LCC_OTA_CACHE_DIR") or str(Path(tempfile.gettempdir()) / "py_chat-chat-lcc-ota"),
        locales_dir=env.get("LCC_OTA_LOCALES_DIR") or str(i18n.LOCALES_DIR),
    )


def build_manifest_url(config: OtaConfig) -> str:
    return f"{config.base_url}/i18n/{quote(config.environment, safe='')}/{SURFACE}/manifest.json"


# ── transport ───────────────────────────────────────────────────────────────
Transport = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


async def httpx_transport(url: str, options: dict[str, Any]) -> dict[str, Any]:
    from app.core.http import request

    response = await request(
        "get", url, headers=options.get("headers") or {}, timeout_ms=options.get("timeout_ms", DEFAULT_TIMEOUT_MS),
        response_type="arraybuffer", validate_status=False, max_content_length=25 * 1024 * 1024,
    )
    return {"status": response.status, "headers": response.headers, "body": response.data or b""}


# ── state ───────────────────────────────────────────────────────────────────
class StateStore:
    def __init__(self, cache_dir: str) -> None:
        self.cache_dir = Path(cache_dir)
        self.memory: dict[str, Any] | None = None
        self.warned = False

    def read(self) -> dict[str, Any] | None:
        try:
            self.memory = json.loads((self.cache_dir / "state.json").read_text(encoding="utf-8"))
            return self.memory
        except (OSError, ValueError):
            return self.memory

    def write(self, state: dict[str, Any]) -> bool:
        self.memory = state
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            target = self.cache_dir / "state.json"
            tmp = target.with_name(f"state.json.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, target)
            return True
        except OSError as error:
            if not self.warned:
                self.warned = True
                print(f"[lcc-ota] could not persist state to {self.cache_dir} ({error}) — falling back to in-memory version state", flush=True)
            return False


# ── manifest / payload ──────────────────────────────────────────────────────
def read_language_entries(manifest: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    sha_map = manifest.get("sha256") if isinstance(manifest, dict) and isinstance(manifest.get("sha256"), dict) else {}
    languages = manifest.get("languages") if isinstance(manifest, dict) else None
    if isinstance(languages, dict):
        for language, value in languages.items():
            url = value if isinstance(value, str) else (value or {}).get("url")
            sha = sha_map.get(language) if isinstance(value, str) else (value or {}).get("sha256")
            if url:
                out[language] = {"url": url, "sha256": sha if sha is not None else sha_map.get(language)}
        return out
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if isinstance(files, dict):
        for language, url in files.items():
            if isinstance(url, str):
                out[language] = {"url": url, "sha256": sha_map.get(language)}
    return out


def assert_valid_manifest(manifest: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(manifest, dict):
        raise ValueError("manifest is not an object")
    if manifest.get("version") is None:
        raise ValueError("manifest has no version")
    entries = read_language_entries(manifest)
    if not entries:
        raise ValueError("manifest lists no language files")
    return entries


def has_changed(manifest: dict[str, Any], entries: dict[str, dict[str, Any]], state: dict[str, Any] | None) -> bool:
    if not state or state.get("version") is None:
        return True
    if str(state["version"]) != str(manifest.get("version")):
        return True
    applied = state.get("sha256") or {}
    return any(entry.get("sha256") and applied.get(language) != entry["sha256"] for language, entry in entries.items())


def extract_namespace(strings: Any, namespace: str = NAMESPACE) -> dict[str, str]:
    if not isinstance(strings, dict):
        raise ValueError("payload has no strings object")
    prefix = f"{namespace}."
    return {k[len(prefix):]: v for k, v in strings.items() if k.startswith(prefix) and isinstance(v, str) and k[len(prefix):]}


async def fetch_language_entries(url: str, sha256: str | None, transport: Transport, timeout_ms: float) -> dict[str, str]:
    result = await transport(url, {"timeout_ms": timeout_ms})
    if result["status"] != 200:
        raise ValueError(f"language request failed with HTTP {result['status']}")
    body = result["body"]
    if sha256:
        actual = hashlib.sha256(body).hexdigest()
        if actual != sha256:
            raise ValueError(f"sha256 mismatch (manifest {sha256[:12]}…, downloaded {actual[:12]}…)")
    try:
        payload = json.loads(body.decode("utf-8"))
    except ValueError as error:
        raise ValueError(f"language payload is not valid JSON: {error}") from error
    entries = extract_namespace(payload.get("strings") if isinstance(payload, dict) else None)
    if not entries:
        raise ValueError(f'payload contains no "{NAMESPACE}." keys')
    return entries


# ── apply ───────────────────────────────────────────────────────────────────
def detect_indent(text: str, fallback: int = 4) -> int | str:
    match = re.search(r"\n([ \t]+)\"", text)
    if not match:
        return fallback
    return "\t" if match.group(1) == "\t" else len(match.group(1))


def merge_catalog(existing: dict[str, Any], entries: dict[str, str]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    updated = unchanged = 0
    for key, value in existing.items():
        if key in entries:
            merged[key] = entries[key]
            if entries[key] == value:
                unchanged += 1
            else:
                updated += 1
        else:
            merged[key] = value
    added = 0
    for key, value in entries.items():
        if key not in merged:
            merged[key] = value
            added += 1
    return {"merged": merged, "updated": updated, "unchanged": unchanged, "added": added, "kept": len(existing) - updated - unchanged}


def apply_language(locales_dir: str, locale: str, entries: dict[str, str]) -> dict[str, Any]:
    file = Path(locales_dir) / f"{locale}.json"
    data: dict[str, Any] = {}
    indent: int | str = 4
    trailing = True
    try:
        text = file.read_text(encoding="utf-8")
        text = text[1:] if text.startswith("﻿") else text
        if text.strip():
            data = json.loads(text)
            indent = detect_indent(text)
            trailing = text.endswith("\n")
    except (OSError, ValueError):
        pass
    result = merge_catalog(data, entries)
    serialized = json.dumps(result["merged"], indent=indent, ensure_ascii=False) + ("\n" if trailing else "")
    tmp = file.with_name(f"{file.name}.{os.getpid()}.tmp")
    tmp.write_text(serialized, encoding="utf-8")
    os.replace(tmp, file)
    catalog_updated = False
    catalog = i18n.get_catalog(locale)
    if isinstance(catalog, dict):
        catalog.update(entries)
        catalog_updated = True
    return {"file": str(file), "locale": locale, "updated": result["updated"], "added": result["added"],
            "unchanged": result["unchanged"], "kept": result["kept"], "catalogUpdated": catalog_updated}


async def run_refresh(config: OtaConfig, transport: Transport, state: StateStore, language_map: dict[str, str] = LANGUAGE_MAP) -> dict[str, Any]:
    url = build_manifest_url(config)
    try:
        previous = state.read()
        headers = {"If-None-Match": previous["etag"]} if previous and previous.get("etag") else {}
        result = await transport(url, {"timeout_ms": config.timeout_ms, "headers": headers})
        if result["status"] == 304:
            return {"ok": True, "applied": False, "reason": "not-modified", "version": (previous or {}).get("version")}
        if result["status"] != 200:
            raise ValueError(f"manifest request failed with HTTP {result['status']}")
        try:
            manifest = json.loads(result["body"].decode("utf-8"))
        except ValueError as error:
            raise ValueError(f"manifest is not valid JSON: {error}") from error
        entries = assert_valid_manifest(manifest)
        etag = (result.get("headers") or {}).get("etag")
        if not has_changed(manifest, entries, previous):
            if etag and etag != (previous or {}).get("etag"):
                state.write({**(previous or {}), "etag": etag})
            return {"ok": True, "applied": False, "reason": "up-to-date", "version": manifest["version"]}
        downloaded = []
        for language, entry in entries.items():
            locale = language_map.get(language)
            if not locale:
                continue
            strings = await fetch_language_entries(entry["url"], entry.get("sha256"), transport, config.timeout_ms)
            downloaded.append((language, locale, strings, entry.get("sha256")))
        if not downloaded:
            print(f"[lcc-ota] manifest v{manifest['version']} has no language this service maps — keeping current strings", flush=True)
            return {"ok": False, "applied": False, "reason": "no-mapped-language"}
        applied = [{"language": lang, **apply_language(config.locales_dir, locale, strings)} for lang, locale, strings, _ in downloaded]
        state.write({"version": manifest["version"], "etag": etag, "sha256": {lang: sha for lang, _, _, sha in downloaded if sha},
                     "appliedAt": iso_now()})
        summary = " | ".join(f"{a['locale']}: {a['updated']} updated, {a['added']} added, {a['unchanged']} identical, {a['kept']} untouched" for a in applied)
        print(f"[lcc-ota] applied {NAMESPACE} strings from {config.environment} manifest v{manifest['version']} — {summary}", flush=True)
        return {"ok": True, "applied": True, "version": manifest["version"], "languages": applied}
    except Exception as error:  # noqa: BLE001
        print(f"[lcc-ota] refresh failed ({error}) — continuing with the current strings. Manifest: {url}", flush=True)
        return {"ok": False, "applied": False, "reason": str(error)}


class LccOta:
    def __init__(self, config: OtaConfig | None = None, transport: Transport | None = None, state: StateStore | None = None) -> None:
        self.config = config or read_config()
        self.transport = transport or httpx_transport
        self.state = state or StateStore(self.config.cache_dir)
        self.running = False
        self.task: asyncio.Task | None = None

    async def refresh(self) -> dict[str, Any]:
        if self.running:
            return {"ok": True, "applied": False, "reason": "already-running"}
        self.running = True
        try:
            return await run_refresh(self.config, self.transport, self.state)
        finally:
            self.running = False

    def start(self) -> None:
        if not self.config.enabled:
            print("[lcc-ota] disabled (LCC_OTA_ENABLED=false) — using the committed locale files only", flush=True)
            return

        async def loop() -> None:
            await self.refresh()
            while self.config.poll_seconds > 0:
                await asyncio.sleep(self.config.poll_seconds)
                await self.refresh()

        self.task = asyncio.get_running_loop().create_task(loop())
        print(f"[lcc-ota] enabled — surface {SURFACE}/{self.config.environment}, poll {int(self.config.poll_seconds)}s, "
              f"locales {self.config.locales_dir}", flush=True)

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self.task = None
