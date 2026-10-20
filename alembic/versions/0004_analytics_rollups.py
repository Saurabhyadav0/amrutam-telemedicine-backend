"""analytics rollup tables (daily platform + per-doctor), refreshed by the worker

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def _count(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer, nullable=False, server_default="0")


def _money(name: str) -> sa.Column:
    return sa.Column(name, sa.BigInteger, nullable=False, server_default="0")


def upgrade() -> None:
    # Dashboards read these small tables instead of scanning consultations/payments.
    op.create_table(
        "analytics_daily",
        sa.Column("day", sa.Date, primary_key=True),
        _count("consultations_booked"),
        _count("consultations_confirmed"),
        _count("consultations_completed"),
        _count("consultations_cancelled"),
        _count("consultations_no_show"),
        _money("gross_revenue_paise"),
        _money("refunds_paise"),
        _count("new_patients"),
        _count("new_doctors"),
        _count("ratings_count"),
        sa.Column("ratings_sum", sa.Integer, nullable=False, server_default="0"),
        sa.Column("refreshed_at", TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "analytics_doctor_daily",
        sa.Column("day", sa.Date, primary_key=True),
        sa.Column(
            "doctor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("doctors.user_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        _count("slots_published"),
        _count("slots_booked"),
        _count("consultations_completed"),
        _count("consultations_cancelled"),
        _money("revenue_paise"),
        sa.Column("refreshed_at", TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_analytics_doctor_daily_doctor", "analytics_doctor_daily", ["doctor_id", "day"]
    )


def downgrade() -> None:
    op.drop_table("analytics_doctor_daily")
    op.drop_table("analytics_daily")
