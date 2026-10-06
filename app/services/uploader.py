"""S3 media upload (port of lib/uploader/s3.js + lib/uploader/upload.js).

* multer limits: single ``file`` field, 1 MB, allowed client MIME types
  image/jpeg|jpg|png|gif + application/pdf (other MIME types are silently
  dropped, exactly like multer's fileFilter returning false);
* ME-21: the stored Content-Type is derived from the file's magic bytes
  (JPEG/PNG/GIF/PDF only) — a spoofed MIME can never be served as HTML;
* S7: every putObject is awaited; a failed put is skipped (no URL returned).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.config import Config

from app.core.config import get_settings
from app.core.logging import logger

ALLOWED_MIME_TYPES = ["image/jpeg", "image/jpg", "image/png", "image/gif", "application/pdf"]
MAX_FILE_SIZE = 1 * 1024 * 1024

_s3 = None


@dataclass
class UploadedFile:
    """multer ``req.file`` shape."""

    fieldname: str
    originalname: str
    encoding: str
    mimetype: str
    buffer: bytes | None
    size: int

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "fieldname": self.fieldname,
            "originalname": self.originalname,
            "encoding": self.encoding,
            "mimetype": self.mimetype,
        }
        if self.buffer is not None:
            out["buffer"] = self.buffer
        out["size"] = self.size
        return out


def _bucket() -> str | None:
    return get_settings().raw.AWS_S3_BUCKET


def _client():
    global _s3
    if _s3 is None:
        settings = get_settings()
        raw = settings.raw
        kwargs: dict[str, Any] = {
            "region_name": raw.AWS_S3_REGION,
            "config": Config(signature_version="s3v4", max_pool_connections=32),
        }
        # aws-sdk v2 global config: S3 keys when set, otherwise the DynamoDB keys
        # that aws.config.js had already merged into AWS.config.
        access = raw.AWS_S3_ACCESS_KEY_ID or settings.aws_dynamodb_access_key
        secret = raw.AWS_S3_SECRET_ACCESS_KEY or settings.aws_dynamodb_secret_key
        if access and secret:
            kwargs["aws_access_key_id"] = access
            kwargs["aws_secret_access_key"] = secret
        if raw.AWS_S3_ENDPOINT_URL:
            kwargs["endpoint_url"] = raw.AWS_S3_ENDPOINT_URL
        kwargs["use_ssl"] = True if raw.AWS_S3_ENDPOINT_URL is None else raw.AWS_S3_SECURE == "true"
        _s3 = boto3.client("s3", **kwargs)
    return _s3


def s3_base() -> str:
    raw = get_settings().raw
    return f"https://{raw.AWS_S3_BUCKET}.s3.{raw.AWS_S3_REGION}.amazonaws.com/"


def detect_allowed_content_type(buffer: Any) -> str | None:
    if not isinstance(buffer, bytes | bytearray) or len(buffer) < 4:
        return None
    b = buffer
    if b[0] == 0xFF and b[1] == 0xD8 and b[2] == 0xFF:
        return "image/jpeg"
    if b[0] == 0x89 and b[1] == 0x50 and b[2] == 0x4E and b[3] == 0x47:
        return "image/png"
    if b[0] == 0x47 and b[1] == 0x49 and b[2] == 0x46 and b[3] == 0x38:
        return "image/gif"
    if b[0] == 0x25 and b[1] == 0x50 and b[2] == 0x44 and b[3] == 0x46:
        return "application/pdf"
    return None


async def upload(files: Any, location: str, extension: str, file_name: str | None = None) -> Any:
    """Returns one URL for a single stored file, else the list of URLs (possibly empty)."""
    items = files if isinstance(files, list) else [files]
    urls: list[str] = []
    for file in items:
        buffer = file.buffer if isinstance(file, UploadedFile) else (file or {}).get("buffer")
        content_type = detect_allowed_content_type(buffer)
        if not content_type:
            print("upload rejected: file content does not match an allowed image/PDF type", flush=True)
            continue
        key = f"{location}/{file_name}.{extension}" if file_name else f"{location}/{int(time.time() * 1000)}.{extension}"
        try:
            await asyncio.to_thread(
                _client().put_object, Bucket=_bucket(), Key=key, Body=bytes(buffer), ContentType=content_type
            )
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "response", {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
            logger.error("upload failed: S3 putObject error", {"code": code or exc.__class__.__name__})
            continue
        urls.append(s3_base() + key)
    return urls[0] if len(urls) == 1 else urls


async def delete_obj(key: str) -> bool:
    try:
        await asyncio.to_thread(_client().delete_object, Bucket=_bucket(), Key=key)
        return True
    except Exception:  # noqa: BLE001
        return False
