"""Add groups.job_worker_id + idx_job_worker_id (port of
migrations/20260821-add-groups-job-worker-id.sql).

Additive (nullable, default NULL, no rows modified) and idempotent. Downgrade
is a deliberate no-op: dropping a populated column is destructive and must
be done by hand after review.

Revision ID: 0003_groups_job_worker_id
Revises: 0002_delivered_read_status
Create Date: 2026-10-05
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_groups_job_worker_id"
down_revision = "0002_delivered_read_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    exists = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = 'groups' AND COLUMN_NAME = 'job_worker_id'"
        )
    ).first()
    if not exists:
        op.execute(
            "ALTER TABLE `groups` ADD COLUMN `job_worker_id` BIGINT UNSIGNED NULL DEFAULT NULL "
            "COMMENT 'PK of job_workers' AFTER `job_id`, ADD INDEX `idx_job_worker_id` (`job_worker_id`)"
        )


def downgrade() -> None:
    pass
