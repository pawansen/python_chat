"""Baseline: the existing py_chat schema (no-op).

The chat service does not own its tables (users, groups, group_participants,
... are shared with the admin / mobile / contract services). This revision
only marks the starting point; ``app/models`` documents the columns, keys and
indexes the service relies on.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-05
"""

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
