"""Add Rules compliance grace-period foundation.

Revision ID: a4f6c8d21e73
Revises: e1a7c4d92b60
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4f6c8d21e73"
down_revision: str | None = "e1a7c4d92b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "rulesets",
        sa.Column("reacceptance_grace_days", sa.SmallInteger(), nullable=True),
    )
    op.create_check_constraint(
        "ck_rulesets_reacceptance_grace_days",
        "rulesets",
        "reacceptance_grace_days IS NULL OR "
        "(requires_reacceptance AND reacceptance_grace_days BETWEEN 1 AND 365)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_rulesets_reacceptance_grace_days",
        "rulesets",
        type_="check",
    )
    op.drop_column("rulesets", "reacceptance_grace_days")
