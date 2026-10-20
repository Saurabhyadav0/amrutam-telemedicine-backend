"""denormalised doctors.next_available_at for search sorting/display

Computing min(open slot) per candidate doctor inside every search cost ~70ms at 5k doctors
(a correlated subquery per row, evaluated before the top-N sort). The column is refreshed by
the worker every minute and immediately for a doctor who publishes/removes slots.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("doctors", sa.Column("next_available_at", sa.DateTime(timezone=True)))
    op.create_index(
        "ix_doctors_next_available",
        "doctors",
        ["next_available_at", "user_id"],
        postgresql_where=sa.text("verification_status = 'verified' AND is_accepting"),
    )
    op.execute(
        """
        UPDATE doctors d SET next_available_at = (
            SELECT min(s.start_at) FROM availability_slots s
            WHERE s.doctor_id = d.user_id AND s.status = 'open' AND s.start_at > now()
        )
        """
    )


def downgrade() -> None:
    op.drop_index("ix_doctors_next_available", table_name="doctors")
    op.drop_column("doctors", "next_available_at")
