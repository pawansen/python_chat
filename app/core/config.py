"""Application configuration (port of src/config/envs.config.js).

Loaded with Pydantic Settings from the process environment and an optional
``.env`` file. Every env var name is identical to the Node service so the same
deployment configuration works for both.

Defaults reproduce the Node expressions exactly:

* ``Number(process.env.X) || d``      -> :func:`_num` (0, "" and NaN fall back)
* ``process.env.X !== "false"``       -> :func:`_not_false` (default ON)
* ``process.env.X === "true"``        -> :func:`_is_true` (default OFF)
* ``parseInt(process.env.X, 10) || d``-> :func:`_int`
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.js import number, parse_int

BASE_DIR = Path(__file__).resolve().parents[2]
APP_DIR = BASE_DIR / "app"


class RawEnv(BaseSettings):
    """Raw string view of every environment variable the service reads."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="allow", case_sensitive=True)

    NODE_ENV: str | None = None
    PORT: str | None = None
    LOG_LEVEL: str | None = None
    CHAT_CONVERSATION_LIMIT: str | None = None
    CHAT_MESSAGE_LIMIT: str | None = None
    CHAT_GROUP_PARTICIPANTS_LIMIT: str | None = None
    USER_ROLE_EMPLOYER: str | None = None
    USER_ROLE_WORKER: str | None = None
    EXCLUDE_REMOVED_PARTICIPANTS: str | None = None
    SITE_URL: str | None = None
    DEFAULT_PROFILE_IMAGE: str | None = None
    AWS_S3_BASE: str | None = None
    AWS_S3_REGION: str | None = None
    AWS_S3_BUCKET: str | None = None
    AWS_S3_ACCESS_KEY_ID: str | None = None
    AWS_S3_SECRET_ACCESS_KEY: str | None = None
    AWS_S3_SECURE: str | None = None
    AWS_DYNOMODB_ACCESS_KEY: str | None = None
    AWS_DYNOMODB_SECRET_KEY: str | None = None
    AWS_DYNAMODB_ENDPOINT_URL: str | None = None  # python-only: local emulator / VPC endpoint
    AWS_S3_ENDPOINT_URL: str | None = None  # python-only: local emulator
    AWS_REGION: str | None = None
    WHATSAPP_API_KEY: str | None = None
    WHATSAPP_API_URL: str | None = None
    WHATSAPP_WEBHOOK_SECRET: str | None = None
    OUTBOUND_HTTP_TIMEOUT_MS: str | None = None
    OUTBOUND_MEDIA_TIMEOUT_MS: str | None = None
    HTTP_REQUEST_TIMEOUT_MS: str | None = None
    HTTP_KEEP_ALIVE_TIMEOUT_MS: str | None = None
    LINE_CHANNEL_ACCESS_TOKEN: str | None = None
    LINE_CHANNEL_SECRET: str | None = None
    INFOBIP_AUTHORIZATION: str | None = None
    FCM_KEY: str | None = None
    API_KEY: str | None = None
    API_KEYS: str | None = None
    TRANSLATE_API_KEY: str | None = None
    TRANSLATE_API_URL: str | None = None
    TRANSLATE_API_TIMEOUT_MS: str | None = None
    TRANSLATE_TARGETS: str | None = None
    TRANSLATE_RETRIES: str | None = None
    TRANSLATE_BACKOFF_BASE_MS: str | None = None
    TRANSLATE_BACKOFF_MAX_MS: str | None = None
    TRANSLATE_BREAKER_THRESHOLD: str | None = None
    TRANSLATE_BREAKER_COOLDOWN_MS: str | None = None
    TRANSLATE_CACHE_TTL_SECONDS: str | None = None
    TRANSLATE_QUEUE_CONCURRENCY: str | None = None
    TRANSLATE_QUEUE_MAX_DEPTH: str | None = None
    TRANSLATE_JOB_RETRIES: str | None = None
    TRANSLATE_JOB_BACKOFF_MS: str | None = None
    TRANSLATE_JOB_BACKOFF_MAX_MS: str | None = None
    WEBHOOK_QUEUE_CONCURRENCY: str | None = None
    WEBHOOK_QUEUE_MAX_DEPTH: str | None = None
    WEBHOOK_JOB_RETRIES: str | None = None
    WEBHOOK_JOB_BACKOFF_MS: str | None = None
    WEBHOOK_JOB_BACKOFF_MAX_MS: str | None = None
    WEBHOOK_DEDUPE_TTL_MS: str | None = None
    WEBHOOK_RATE_LIMIT_MAX: str | None = None
    WEBHOOK_FANOUT_CONCURRENCY: str | None = None
    GLOBAL_RATE_LIMIT_MAX: str | None = None
    FIREBASE_SERVICE_ACCOUNT_BASE64: str | None = None
    FCM_SERVICE_ACCOUNT_PATH: str | None = None
    INVALIDATION_BUS_ENABLED: str | None = None
    REDIS_HOST: str | None = None
    REDIS_PORT: str | None = None
    REDIS_USERNAME: str | None = None
    REDIS_PASSWORD: str | None = None
    REDIS_DB: str | None = None
    CACHE_TTL_LONG: str | None = None
    CACHE_TTL_LIST: str | None = None
    CACHE_TTL_DUPLICATE: str | None = None
    IS_REDIS_ENABLED: str | None = None
    REDIS_TLS_ENABLED: str | None = None
    REDIS_TLS: str | None = None
    REDIS_CONNECT_TIMEOUT: str | None = None
    REDIS_COMMAND_TIMEOUT: str | None = None
    REDIS_RETRY_DELAY: str | None = None
    REDIS_RETRY_MAX_DELAY: str | None = None
    REDIS_SOCKET_ADAPTER_ENABLED: str | None = None
    REDIS_SOCKET_ADAPTER_SHARDED: str | None = None
    REDIS_SOCKET_ADAPTER_HOST: str | None = None
    REDIS_SOCKET_ADAPTER_PORT: str | None = None
    REDIS_SOCKET_ADAPTER_TLS: str | None = None
    REDIS_SOCKET_ADAPTER_PASSWORD: str | None = None
    DynoMODB_Counter_Table: str | None = None
    DynoMODB_Message_Table: str | None = None
    ACCESS_TOKEN_SECRET: str | None = None
    ACCESS_TOKEN_EXPIRED: str | None = None
    ACCESS_TOKEN_ISSUER: str | None = None
    ACCESS_TOKEN_AUDIENCE: str | None = None
    SOCKET_HANDSHAKE_AUTH_ENFORCE: str | None = None
    SOCKET_RATE_LIMIT_ENABLED: str | None = None
    SOCKET_JWT_BLOCKLIST_PREFIX: str | None = None
    SOCKET_AUTH_CACHE_TTL_MS: str | None = None
    SOCKET_MAX_HTTP_BUFFER_SIZE: str | None = None
    DB_HOST: str | None = None
    DB_PORT: str | None = None
    DB_NAME: str | None = None
    DB_USER: str | None = None
    DB_PASS: str | None = None
    DB_DIALECT: str | None = None
    POOL_MAX: str | None = None
    POOL_MIN: str | None = None
    POOL_ACQUIRE: str | None = None
    POOL_IDLE: str | None = None
    TWILIO_ACCOUNT_SID: str | None = None
    TWILIO_AUTH_TOKEN: str | None = None
    TWILIO_API_KEY: str | None = None
    TWILIO_API_SECRET: str | None = None
    TWILIO_CHAT_SERVICE_SID: str | None = None
    TWILIO_WHATSAPP_FROM: str | None = None
    TWILIO_MEDIA_UPLOAD_LINK: str | None = None
    MESSAGEBIRD_API_KEY: str | None = None
    GOOGLE_PROJECT_NUMBER: str | None = None
    GOOGLE_POOL_ID: str | None = None
    GOOGLE_PROVIDER_ID: str | None = None
    GOOGLE_SERVICE_ACCOUNT: str | None = None
    HTTP_COMPRESSION_ENABLED: str | None = None
    CHAT_LIST_DYNAMO_CONCURRENCY: str | None = None
    DELIVERED_SYNC_PAGE_SIZE: str | None = None
    DELIVERED_SYNC_MAX_MESSAGES: str | None = None
    READ_SYNC_PAGE_SIZE: str | None = None
    READ_SYNC_MAX_MESSAGES: str | None = None
    HISTORY_MAX_GROUPS: str | None = None
    RECEIVER_LOOKUP_PAGE_SIZE: str | None = None
    RECEIVER_LOOKUP_MAX_PAGES: str | None = None
    LCC_OTA_ENABLED: str | None = None
    LANGUAGE_PULL_FROM_SERVER: str | None = None
    LCC_OTA_BASE_URL: str | None = None
    LCC_OTA_POLL_SECONDS: str | None = None
    LCC_OTA_TIMEOUT_MS: str | None = None
    LCC_OTA_CACHE_DIR: str | None = None
    LCC_OTA_LOCALES_DIR: str | None = None
    ALLOWED_ORIGINS: str | None = None  # python-only override; default = Node's hard-coded list


def _num(value: str | None, default: float) -> float:
    """``Number(value) || default``."""
    if value is None:
        return default
    parsed = number(value)
    if isinstance(parsed, float) and math.isnan(parsed):
        return default
    return parsed if parsed else default


def _int(value: str | None, default: int) -> int:
    """``parseInt(value, 10) || default``."""
    if value is None:
        return default
    parsed = parse_int(value)
    if isinstance(parsed, float) and math.isnan(parsed):
        return default
    return int(parsed) if parsed else default


def _not_false(value: str | None) -> bool:
    return value != "false"


def _is_true(value: str | None) -> bool:
    return value == "true"


def _str(value: str | None, default: str = "") -> str:
    return value if value else default


@dataclass(frozen=True)
class TranslateTarget:
    code: str
    name: str


@dataclass
class TranslateConfig:
    api_key: str
    base_url: str
    timeout_ms: float
    targets: list[TranslateTarget]
    retries: float
    backoff_base_ms: float
    backoff_max_ms: float
    breaker_threshold: float
    breaker_cooldown_ms: float
    cache_ttl_seconds: float
    queue_concurrency: float
    max_queue_depth: float
    job_retries: float
    job_backoff_ms: float
    job_backoff_max_ms: float


@dataclass
class WebhookConfig:
    concurrency: float
    max_depth: float
    retries: float
    backoff_ms: float
    backoff_max_ms: float
    dedupe_ttl_ms: float


@dataclass
class RedisConfig:
    host: str
    port: int
    username: str
    password: str
    db: int
    cache_ttl_long: int
    is_redis_enabled: bool
    tls: bool
    connect_timeout_ms: float
    command_timeout_ms: float
    retry_delay_ms: float
    retry_max_delay_ms: float
    socket_adapter_enabled: bool
    adapter_sharded: bool
    adapter_host: str
    adapter_port: int
    adapter_tls: bool
    adapter_password: str | None


@dataclass
class JwtConfig:
    secret: str
    expiry: float
    issuer: str
    audience: str


@dataclass
class SocketConfig:
    handshake_auth_enforce: bool
    rate_limit_enabled: bool
    jwt_blocklist_prefix: str
    auth_cache_ttl_ms: float
    max_http_buffer_size: int


@dataclass
class DbConfig:
    host: str | None
    port: str | None
    database: str | None
    username: str | None
    password: str | None
    dialect: str | None
    pool_max: int
    pool_min: int
    pool_acquire_ms: int
    pool_idle_ms: int


@dataclass
class Settings:
    raw: RawEnv
    env: str
    port: int
    chat_conversation_limit: float
    chat_message_limit: float
    chat_group_participants_limit: float
    user_role_employer: float
    user_role_worker: float
    exclude_removed_participants: bool
    site_url: str | None
    default_profile_image: str | None
    aws_s3_base_url: str | None
    aws_s3_region: str | None
    aws_dynamodb_access_key: str | None
    aws_dynamodb_secret_key: str | None
    whatsapp_api_key: str
    whatsapp_api_url: str
    outbound_http_timeout_ms: float
    outbound_media_timeout_ms: float
    http_request_timeout_ms: float
    http_keep_alive_timeout_ms: float
    whatsapp_webhook_secret: str
    line_channel_access_token: str
    line_channel_secret: str
    api_key: str
    api_keys: list[str]
    translate: TranslateConfig
    webhook: WebhookConfig
    firebase_service_account_base64: str
    firebase_service_account_path: str
    invalidation_bus_enabled: bool
    redis: RedisConfig
    dynamodb_counter_table: str
    dynamodb_message_table: str
    jwt: JwtConfig
    socket: SocketConfig
    db: DbConfig
    twilio: dict[str, str | None]
    cache_ttl_list: float
    cache_ttl_duplicate: float
    chat_list_dynamo_concurrency: int
    webhook_fanout_concurrency: int
    webhook_rate_limit_max: int
    global_rate_limit_max: int
    http_compression_enabled: bool
    allowed_origins: list[str] = field(default_factory=list)

    @property
    def is_production(self) -> bool:
        return self.raw.NODE_ENV == "production"


def _parse_targets(value: str | None) -> list[TranslateTarget]:
    targets = []
    for pair in (value or "en:en,id:id,zh_cn:zh-CN,zh_tw:zh-TW").split(","):
        pair = pair.strip()
        if not pair:
            continue
        code, _, name = pair.partition(":")
        targets.append(TranslateTarget(code=code.strip(), name=(name or code).strip()))
    return targets


def build_settings(raw: RawEnv | None = None) -> Settings:
    r = raw or RawEnv()
    api_keys = [k.strip() for k in (r.API_KEYS or r.API_KEY or "").split(",") if k.strip()]
    adapter_tls_raw = r.REDIS_SOCKET_ADAPTER_TLS if r.REDIS_SOCKET_ADAPTER_TLS is not None else r.REDIS_TLS_ENABLED
    production = r.NODE_ENV == "production"
    default_origins = ([] if production else ["http://localhost:3000"]) + [
        "https://newui.py_chat.ai",
        "https://py_chat.ai",
        "https://staging.py_chat.ai",
    ]
    origins = [o.strip() for o in r.ALLOWED_ORIGINS.split(",") if o.strip()] if r.ALLOWED_ORIGINS else default_origins
    return Settings(
        raw=r,
        env=r.NODE_ENV or "dev",
        port=int(_num(r.PORT, 7091)),
        chat_conversation_limit=_num(r.CHAT_CONVERSATION_LIMIT, 15),
        chat_message_limit=_num(r.CHAT_MESSAGE_LIMIT, 20),
        chat_group_participants_limit=_num(r.CHAT_GROUP_PARTICIPANTS_LIMIT, 4),
        user_role_employer=_num(r.USER_ROLE_EMPLOYER, 3),
        user_role_worker=_num(r.USER_ROLE_WORKER, 4),
        exclude_removed_participants=_not_false(r.EXCLUDE_REMOVED_PARTICIPANTS),
        site_url=r.SITE_URL,
        default_profile_image=r.DEFAULT_PROFILE_IMAGE,
        aws_s3_base_url=r.AWS_S3_BASE,
        aws_s3_region=r.AWS_S3_REGION,
        aws_dynamodb_access_key=r.AWS_DYNOMODB_ACCESS_KEY,
        aws_dynamodb_secret_key=r.AWS_DYNOMODB_SECRET_KEY,
        whatsapp_api_key=_str(r.WHATSAPP_API_KEY),
        whatsapp_api_url=_str(r.WHATSAPP_API_URL),
        outbound_http_timeout_ms=_num(r.OUTBOUND_HTTP_TIMEOUT_MS, 10000),
        outbound_media_timeout_ms=_num(r.OUTBOUND_MEDIA_TIMEOUT_MS, 30000),
        http_request_timeout_ms=_num(r.HTTP_REQUEST_TIMEOUT_MS, 30000),
        http_keep_alive_timeout_ms=_num(r.HTTP_KEEP_ALIVE_TIMEOUT_MS, 65000),
        whatsapp_webhook_secret=_str(r.WHATSAPP_WEBHOOK_SECRET),
        line_channel_access_token=_str(r.LINE_CHANNEL_ACCESS_TOKEN),
        line_channel_secret=_str(r.LINE_CHANNEL_SECRET),
        api_key=_str(r.API_KEY),
        api_keys=api_keys,
        translate=TranslateConfig(
            api_key=_str(r.TRANSLATE_API_KEY),
            base_url=_str(r.TRANSLATE_API_URL, "https://api.py_chat.ai/translate"),
            timeout_ms=_num(r.TRANSLATE_API_TIMEOUT_MS, 8000),
            targets=_parse_targets(r.TRANSLATE_TARGETS),
            retries=_num(r.TRANSLATE_RETRIES, 2),
            backoff_base_ms=_num(r.TRANSLATE_BACKOFF_BASE_MS, 200),
            backoff_max_ms=_num(r.TRANSLATE_BACKOFF_MAX_MS, 3000),
            breaker_threshold=_num(r.TRANSLATE_BREAKER_THRESHOLD, 10),
            breaker_cooldown_ms=_num(r.TRANSLATE_BREAKER_COOLDOWN_MS, 30000),
            cache_ttl_seconds=_num(r.TRANSLATE_CACHE_TTL_SECONDS, 604800),
            queue_concurrency=_num(r.TRANSLATE_QUEUE_CONCURRENCY, 5),
            max_queue_depth=_num(r.TRANSLATE_QUEUE_MAX_DEPTH, 5000),
            job_retries=_num(r.TRANSLATE_JOB_RETRIES, 3),
            job_backoff_ms=_num(r.TRANSLATE_JOB_BACKOFF_MS, 1000),
            job_backoff_max_ms=_num(r.TRANSLATE_JOB_BACKOFF_MAX_MS, 15000),
        ),
        webhook=WebhookConfig(
            concurrency=_num(r.WEBHOOK_QUEUE_CONCURRENCY, 5),
            max_depth=_num(r.WEBHOOK_QUEUE_MAX_DEPTH, 5000),
            retries=_num(r.WEBHOOK_JOB_RETRIES, 2),
            backoff_ms=_num(r.WEBHOOK_JOB_BACKOFF_MS, 1000),
            backoff_max_ms=_num(r.WEBHOOK_JOB_BACKOFF_MAX_MS, 15000),
            dedupe_ttl_ms=_num(r.WEBHOOK_DEDUPE_TTL_MS, 600000),
        ),
        firebase_service_account_base64=_str(r.FIREBASE_SERVICE_ACCOUNT_BASE64),
        firebase_service_account_path=_str(r.FCM_SERVICE_ACCOUNT_PATH),
        invalidation_bus_enabled=_not_false(r.INVALIDATION_BUS_ENABLED),
        redis=RedisConfig(
            host=_str(r.REDIS_HOST, "localhost"),
            port=int(_num(r.REDIS_PORT, 6379)),
            username=_str(r.REDIS_USERNAME),
            password=_str(r.REDIS_PASSWORD),
            db=int(_num(r.REDIS_DB, 0)),
            cache_ttl_long=int(_num(r.CACHE_TTL_LONG, 36000)),
            is_redis_enabled=_not_false(r.IS_REDIS_ENABLED),
            tls=_is_true(r.REDIS_TLS_ENABLED) or _is_true(r.REDIS_TLS),
            connect_timeout_ms=_num(r.REDIS_CONNECT_TIMEOUT, 5000),
            command_timeout_ms=_num(r.REDIS_COMMAND_TIMEOUT, 0),
            retry_delay_ms=_num(r.REDIS_RETRY_DELAY, 200),
            retry_max_delay_ms=_num(r.REDIS_RETRY_MAX_DELAY, 5000),
            socket_adapter_enabled=_not_false(r.REDIS_SOCKET_ADAPTER_ENABLED),
            adapter_sharded=_not_false(r.REDIS_SOCKET_ADAPTER_SHARDED),
            adapter_host=r.REDIS_SOCKET_ADAPTER_HOST or r.REDIS_HOST or "localhost",
            adapter_port=int(_num(r.REDIS_SOCKET_ADAPTER_PORT, 0) or _num(r.REDIS_PORT, 6379)),
            adapter_tls=adapter_tls_raw == "true",
            adapter_password=r.REDIS_SOCKET_ADAPTER_PASSWORD or r.REDIS_PASSWORD or None,
        ),
        dynamodb_counter_table=_str(r.DynoMODB_Counter_Table, "Counters"),
        dynamodb_message_table=_str(r.DynoMODB_Message_Table, "chat_messages"),
        jwt=JwtConfig(
            secret=_str(r.ACCESS_TOKEN_SECRET),
            expiry=_num(r.ACCESS_TOKEN_EXPIRED, 3600),
            issuer=_str(r.ACCESS_TOKEN_ISSUER),
            audience=_str(r.ACCESS_TOKEN_AUDIENCE),
        ),
        socket=SocketConfig(
            handshake_auth_enforce=_not_false(r.SOCKET_HANDSHAKE_AUTH_ENFORCE),
            rate_limit_enabled=_not_false(r.SOCKET_RATE_LIMIT_ENABLED),
            jwt_blocklist_prefix=_str(r.SOCKET_JWT_BLOCKLIST_PREFIX, "jwt:blocklist:"),
            auth_cache_ttl_ms=_num(r.SOCKET_AUTH_CACHE_TTL_MS, 60000),
            max_http_buffer_size=int(_num(r.SOCKET_MAX_HTTP_BUFFER_SIZE, 1e6)),
        ),
        db=DbConfig(
            host=r.DB_HOST,
            port=r.DB_PORT,
            database=r.DB_NAME,
            username=r.DB_USER,
            password=r.DB_PASS,
            dialect=r.DB_DIALECT,
            pool_max=_int(r.POOL_MAX, 10),
            pool_min=_int(r.POOL_MIN, 0),
            pool_acquire_ms=_int(r.POOL_ACQUIRE, 30000),
            pool_idle_ms=_int(r.POOL_IDLE, 10000),
        ),
        twilio={
            "account_sid": r.TWILIO_ACCOUNT_SID,
            "auth_token": r.TWILIO_AUTH_TOKEN,
            "api_key": r.TWILIO_API_KEY,
            "api_secret": r.TWILIO_API_SECRET,
            "chat_service_sid": r.TWILIO_CHAT_SERVICE_SID,
            "whatsapp_from": r.TWILIO_WHATSAPP_FROM,
            "media_upload_link": r.TWILIO_MEDIA_UPLOAD_LINK,
        },
        cache_ttl_list=_num(r.CACHE_TTL_LIST, 1800),
        cache_ttl_duplicate=_num(r.CACHE_TTL_DUPLICATE, 600),
        chat_list_dynamo_concurrency=int(_num(r.CHAT_LIST_DYNAMO_CONCURRENCY, 8)),
        webhook_fanout_concurrency=int(_num(r.WEBHOOK_FANOUT_CONCURRENCY, 4)),
        webhook_rate_limit_max=int(_num(r.WEBHOOK_RATE_LIMIT_MAX, 300)),
        global_rate_limit_max=int(_num(r.GLOBAL_RATE_LIMIT_MAX, 1000)),
        http_compression_enabled=_is_true(r.HTTP_COMPRESSION_ENABLED),
        allowed_origins=origins,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return build_settings()


def env_num(raw_value: str | None, default: float) -> float:
    """Public helper for call sites that read ``Number(process.env.X) || d`` lazily."""
    return _num(raw_value, default)
