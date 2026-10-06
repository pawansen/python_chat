"""Add 'delivered' to messages / messages_log read_status (port of
migrations/20260820-add-delivered-read-status.sql).

Additive and idempotent: the value is appended (existing members and stored
values unchanged, default stays 'unread'); skipped when already present.
Downgrade is intentionally a no-op (removing an ENUM member could fail on or
rewrite existing rows — destructive changes are never automated).

Revision ID: 0002_delivered_read_status
Revises: 0001_baseline
Create Date: 2026-10-05
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_delivered_read_status"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def _column_type(table: str) -> str | None:
    row = op.get_bind().execute(
        sa.text(
            "SELECT COLUMN_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = :t AND COLUMN_NAME = 'read_status'"
        ),
        {"t": table},
    ).first()
    return row[0] if row else None


def upgrade() -> None:
    for table in ("messages", "messages_log"):
        current = _column_type(table)
        if current is None or "'delivered'" in current:
            continue
        op.execute(
            f"ALTER TABLE `{table}` MODIFY COLUMN `read_status` ENUM('read', 'unread', 'delivered') NOT NULL DEFAULT 'unread'"
        )


def downgrade() -> None:
    pass
