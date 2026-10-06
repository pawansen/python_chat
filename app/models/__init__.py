"""SQLAlchemy 2.x models (port of src/models/*.model.js).

Columns, keys, defaults and indexes mirror the Sequelize definitions exactly.
They document the schema this service touches and feed the Alembic baseline;
the live tables are owned by the shared py_chat database, so nothing here is
ever created/dropped automatically at runtime.

``SEQUELIZE_DEFAULTS`` records, per model and in definition order, the
attributes that carry a ``defaultValue`` — :func:`build_values` uses it to
reproduce ``Model.create()``'s returned ``dataValues`` (key order included),
which several endpoints return to clients verbatim.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    DateTime,
    Enum,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import BIGINT, INTEGER, TINYINT

metadata = MetaData()

UBigInt = BIGINT(unsigned=True)
UInt = INTEGER(unsigned=True)
UTinyInt = TINYINT(unsigned=True)

groups = Table(
    "groups",
    metadata,
    Column("id", UBigInt, primary_key=True, autoincrement=True),
    Column("conversation_state", String(255), nullable=True, default=None),
    Column("participant_sid", String(150), nullable=True, default=None),
    Column("friendly_name", String(255), nullable=True, default=None),
    Column("job_id", UInt, nullable=True, default=None),
    Column("job_worker_id", UBigInt, nullable=True, default=None, comment="PK of job_workers"),
    Column("job_description", String(255), nullable=True, default=None),
    Column("unique_name", String(50), nullable=True, default=None),
    Column("account_sid", String(150), nullable=True, default=None),
    Column("chat_service_sid", String(150), nullable=True, default=None),
    Column("conversation_sid", String(150), nullable=True, default=None),
    Column("conversation_bindings", String(255), nullable=True, default=None),
    Column("last_message", Text, nullable=True, default=None),
    Column("last_msgtype", String(255), nullable=True, default=None),
    Column("last_message_ceated_at", DateTime, nullable=True, default=None),
    Column("message_send_by", UBigInt, nullable=True, default=None),
    Column("type", String(255), nullable=False),
    Column("platform", Enum("app", "whatsapp", "wechat", "sms"), nullable=False),
    Column("platform_two", Enum("app", "whatsapp", "wechat", "sms", "action_required"), nullable=True, default=None),
    Column("friend_type", Enum("employer", "user", "worker", "partner", "group_team", "processing"), nullable=False),
    Column("image", String(255), nullable=True, default=None),
    Column("is_created_by_staff", UTinyInt, nullable=False, default=0),
    Column("employer_group_title", String(255), nullable=True, default=None),
    Column("worker_group_title", String(255), nullable=True, default=None),
    Column("is_deleted", UTinyInt, nullable=False, default=0, comment="1=Deleted , 0=Not deleted"),
    Column("created_at", DateTime, nullable=False),
    Column("created_by", UBigInt, nullable=True, default=None, comment="PK of users"),
    Column("updated_at", DateTime, nullable=False),
    Column("updated_by", UBigInt, nullable=True, default=None, comment="PK of users"),
    Column("last_whatsapp_msg_ceated_at", DateTime, nullable=True, default=None),
    UniqueConstraint("unique_name", "platform", "platform_two", name="uk_groups_unique_name_platform_platform_two"),
    Index("idx_conversation_sid", "conversation_sid"),
    Index("idx_participant_sid", "participant_sid"),
    Index("idx_job_id", "job_id"),
    Index("idx_job_worker_id", "job_worker_id"),
    Index("idx_friend_type_deleted", "friend_type", "is_deleted"),
    Index("idx_platform_deleted", "platform", "is_deleted"),
    Index("idx_last_message_created", "last_message_ceated_at"),
    Index("idx_created_by", "created_by"),
    Index("idx_updated_by", "updated_by"),
)

group_participants = Table(
    "group_participants",
    metadata,
    Column("id", UInt, primary_key=True, autoincrement=True),
    Column("role_sid", String(255), nullable=True, default=None),
    Column("messaging_binding", String(255), nullable=True, default=None),
    Column("identity", String(255), nullable=True, default=None),
    Column("group_id", UBigInt, nullable=False, comment="PK of groups"),
    Column("user_id", UBigInt, nullable=False, comment="PK of users"),
    Column("account_sid", String(150), nullable=True, default=""),
    Column("role_id", UInt, nullable=False, default=0),
    Column("last_read_message_index", String(255), nullable=True, default=None),
    Column("last_read_timestamp", String(255), nullable=True, default=None),
    Column("unread_messages_count", UInt, nullable=False, default=0),
    Column("is_host", UTinyInt, nullable=False, default=0),
    Column("last_seen_on", Date, nullable=True, default=None),
    Column("joined_on", Date, nullable=False),
    Column("is_deleted", UTinyInt, nullable=False, default=0, comment="1=Deleted , 0=Not deleted"),
    Column("deleted_by", UBigInt, nullable=True, default=None, comment="PK of users"),
    Column("deleted_at", DateTime, nullable=True, default=None),
    Column("created_at", DateTime, nullable=False),
    Column("created_by", UBigInt, nullable=False, comment="PK of users"),
    Column("updated_at", DateTime, nullable=False),
    Column("updated_by", UBigInt, nullable=True, default=None, comment="PK of users"),
    # NOTE: `last_read_at` is written by raw SQL only (setLastReadWatermark) and
    # is intentionally not declared, exactly like the Sequelize model.
    UniqueConstraint("group_id", "user_id", name="uk_group_user"),
    Index("idx_user_deleted", "user_id", "is_deleted"),
    Index("idx_group_deleted", "group_id", "is_deleted"),
    Index("idx_user_unread", "user_id", "unread_messages_count"),
    Index("idx_identity", "identity"),
    Index("idx_role_sid", "role_sid"),
    Index("idx_created_by", "created_by"),
    Index("idx_updated_by", "updated_by"),
    Index("idx_deleted_by", "deleted_by"),
)

users = Table(
    "users",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("mobile_number", String(255)),
    Column("country_code", String(255)),
    Column("email", String(255)),
    Column("role_id", Integer),
    Column("last_password_updated_by", Integer, nullable=True),
    Column("last_password_updated_at", DateTime, nullable=True),
    Column("whatsapp_verified", Integer, nullable=False, default=0),
    Column("status", Integer, nullable=False, default=1),
    Column("created_by", Integer),
    Column("created_at", DateTime),
    Column("updated_by", Integer),
    Column("updated_at", DateTime),
)

roles = Table(
    "roles",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("role_name", String(50), nullable=False),
    Column("guard_name", String(50), nullable=False),
    Column("description", Text),
    Column("status", Integer, nullable=False, default=1),
    Column("created_by", Integer),
    Column("created_at", DateTime),
    Column("updated_by", Integer),
    Column("updated_at", DateTime),
)

employers = Table(
    "employers",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, nullable=False),
    Column("name", String(255), nullable=False),
    Column("surname", String(255), nullable=False),
    Column("whatsapp_country_code", String(255), nullable=True),
    Column("whatsapp_number", String(255), nullable=True),
    Column("created_at", DateTime, nullable=True),
    Column("updated_at", DateTime, nullable=True),
)

workers = Table(
    "workers",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, nullable=False),
    Column("name", String(255), nullable=False),
    Column("surname", String(255), nullable=False),
    Column("whatsapp_country_code", String(255), nullable=True),
    Column("whatsapp_number", String(255), nullable=True),
)

staffs = Table(
    "staffs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, nullable=False),
    Column("name", String(255), nullable=False),
    Column("surname", String(255), nullable=False),
    Column("created_at", DateTime, nullable=True),
    Column("updated_at", DateTime, nullable=True),
)

messages = Table(
    "messages",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("group_id", Integer),
    Column("user_id", BigInteger),
    Column("replay_message_id", BigInteger, nullable=True),
    Column("entity_id", BigInteger, nullable=True),
    Column("content", String(255), nullable=True),
    Column(
        "message_type",
        Enum("job_detail", "interview", "worker_detail", "text", "image", "file"),
        nullable=False,
        default="text",
    ),
    Column("media_type", Enum("text", "image", "file"), nullable=False, default="text"),
    Column("media_link", String(255), nullable=True),
    Column("media_info", String(255), nullable=True),
    Column("attributes", String(255), nullable=True),
    Column("response_time", Integer, nullable=False, default=0),
    Column("is_deleted", Integer, nullable=False, default=0),
    # 'delivered' requires migration 20260820 (unread -> delivered -> read)
    Column("read_status", Enum("read", "unread", "delivered"), nullable=False, default="unread"),
    Column("status", Integer, default=1),
    Column("deleted_by", Integer),
    Column("deleted_at", DateTime),
    Column("created_at", DateTime, nullable=True),
    Column("created_by", BigInteger, nullable=True),
    Column("updated_at", DateTime, nullable=True),
    Column("updated_by", BigInteger, nullable=True),
)

messages_log = Table(
    "messages_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("message_id", BigInteger),
    Column("user_id", BigInteger),
    Column("read_status", Enum("read", "unread", "delivered"), nullable=False, default="unread"),
    Column("status", Integer, default=1),
    Column("deleted_by", Integer),
    Column("deleted_at", DateTime),
    Column("created_at", DateTime, nullable=True),
    Column("created_by", BigInteger, nullable=True),
    Column("updated_at", DateTime, nullable=True),
    Column("updated_by", BigInteger, nullable=True),
)

action_logs = Table(
    "action_logs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, nullable=False),
    Column("worker_id", Integer, nullable=True),
    Column("employer_id", Integer, nullable=True),
    Column("entity_type", String(100), nullable=False),
    Column("action_type", String(100), nullable=False),
    Column("content", Text, nullable=False),
    Column("status", Integer, nullable=False, default=1),
    Column("created_by", Integer),
    Column("created_at", DateTime),
)

_NOW = "__NOW__"

# Attributes with a Sequelize ``defaultValue`` (definition order). _NOW means DataTypes.NOW.
SEQUELIZE_DEFAULTS: dict[str, list[tuple[str, Any]]] = {
    "groups": [
        ("conversation_state", None),
        ("participant_sid", None),
        ("friendly_name", None),
        ("job_id", None),
        ("job_worker_id", None),
        ("job_description", None),
        ("unique_name", None),
        ("account_sid", None),
        ("chat_service_sid", None),
        ("conversation_sid", None),
        ("conversation_bindings", None),
        ("last_message", None),
        ("last_msgtype", None),
        ("last_message_ceated_at", None),
        ("message_send_by", None),
        ("platform_two", None),
        ("image", None),
        ("is_created_by_staff", 0),
        ("employer_group_title", None),
        ("worker_group_title", None),
        ("is_deleted", 0),
        ("created_at", _NOW),
        ("created_by", None),
        ("updated_at", _NOW),
        ("updated_by", None),
        ("last_whatsapp_msg_ceated_at", None),
    ],
    "group_participants": [
        ("role_sid", None),
        ("messaging_binding", None),
        ("identity", None),
        ("account_sid", ""),
        ("role_id", 0),
        ("last_read_message_index", None),
        ("last_read_timestamp", None),
        ("unread_messages_count", 0),
        ("is_host", 0),
        ("last_seen_on", None),
        ("is_deleted", 0),
        ("deleted_by", None),
        ("deleted_at", None),
        ("created_at", _NOW),
        ("updated_at", _NOW),
        ("updated_by", None),
    ],
}

DATEONLY_COLUMNS = {"group_participants": {"last_seen_on", "joined_on"}}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def build_values(table: Table, data: dict[str, Any], now: Callable[[], datetime] = _now) -> dict[str, Any]:
    """Reproduce ``Model.build(data).dataValues`` (Sequelize v6) for an INSERT.

    Order: defaults for attributes the caller did not provide (definition
    order), then the primary key (``None`` until the INSERT), then the caller's
    values in their own order. Keys that are not model attributes are dropped;
    ``undefined`` values count as not provided.
    """
    from app.core.js import UNDEFINED

    columns = {column.name for column in table.columns}
    provided = {k: v for k, v in data.items() if k in columns and v is not UNDEFINED}
    values: dict[str, Any] = {}
    stamp = None
    for name, default in SEQUELIZE_DEFAULTS.get(table.name, []):
        if name in provided:
            continue
        if default == _NOW:
            stamp = stamp or now()
            values[name] = stamp
        else:
            values[name] = default
    values["id"] = provided.pop("id", None)
    dateonly = DATEONLY_COLUMNS.get(table.name, set())
    for key, value in provided.items():
        if key in dateonly and isinstance(value, datetime):
            value = value.strftime("%Y-%m-%d")
        values[key] = value
    return values


__all__ = [
    "metadata",
    "groups",
    "group_participants",
    "users",
    "roles",
    "employers",
    "workers",
    "staffs",
    "messages",
    "messages_log",
    "action_logs",
    "build_values",
    "SmallInteger",
]
