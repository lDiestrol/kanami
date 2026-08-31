"""add member return idempotency

Revision ID: 7c2d9a4e6f10
Revises: 2f6a8c4d1e90
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c2d9a4e6f10"
down_revision: str | None = "2f6a8c4d1e90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_audit_events_member_returned",
        "audit_events",
        ["guild_id", "subject_id", "occurred_at"],
        unique=True,
        postgresql_where=sa.text("event_type = 'member.returned'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_audit_events_member_returned",
        table_name="audit_events",
    )
