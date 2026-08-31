"""add member anniversary idempotency

Revision ID: 2f6a8c4d1e90
Revises: 4b9c1e7a2d63
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2f6a8c4d1e90"
down_revision: str | None = "4b9c1e7a2d63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_audit_events_member_anniversary",
        "audit_events",
        ["guild_id", "subject_id", "occurred_at"],
        unique=True,
        postgresql_where=sa.text("event_type = 'member.anniversary'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_audit_events_member_anniversary",
        table_name="audit_events",
    )
