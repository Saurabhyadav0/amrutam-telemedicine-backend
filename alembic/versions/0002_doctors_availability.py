"""doctors, availability slots (no-overlap exclusion constraint), doctor full-text search

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
UUID = postgresql.UUID(as_uuid=True)

# array_to_string is STABLE, so a generated column can't use it: keep the vector via trigger.
SEARCH_DDL = [
    """
    CREATE OR REPLACE FUNCTION doctors_search_vector_refresh() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        NEW.search_vector :=
            setweight(to_tsvector('simple', coalesce(NEW.display_name, '')), 'A') ||
            setweight(to_tsvector('simple',
                replace(array_to_string(NEW.specialties, ' '), '_', ' ')), 'B') ||
            setweight(to_tsvector('simple', coalesce(NEW.qualification, '')), 'C') ||
            setweight(to_tsvector('english', coalesce(NEW.bio, '')), 'D');
        RETURN NEW;
    END;
    $$
    """,
    """
    CREATE TRIGGER doctors_search_vector
        BEFORE INSERT OR UPDATE OF display_name, specialties, qualification, bio ON doctors
        FOR EACH ROW EXECUTE FUNCTION doctors_search_vector_refresh()
    """,
]


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.create_table(
        "doctors",
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("registration_number", sa.String(64), nullable=False),
        sa.Column("registration_council", sa.String(64), nullable=False),
        sa.Column("qualification", sa.String(120), nullable=False),
        sa.Column("years_experience", sa.Integer, nullable=False),
        sa.Column("specialties", postgresql.ARRAY(sa.String(32)), nullable=False),
        sa.Column("languages", postgresql.ARRAY(sa.String(8)), nullable=False),
        sa.Column("fee_paise", sa.Integer, nullable=False),
        sa.Column("bio", sa.Text),
        sa.Column("verification_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("verified_at", TS),
        sa.Column("verified_by", UUID, sa.ForeignKey("users.id")),
        sa.Column("rejection_reason", sa.String(500)),
        sa.Column("is_accepting", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("rating_avg", sa.Numeric(3, 2), nullable=False, server_default="0"),
        sa.Column("rating_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("search_vector", postgresql.TSVECTOR),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "registration_council", "registration_number", name="uq_doctors_registration"
        ),
        sa.CheckConstraint(
            "verification_status IN ('pending','verified','rejected')",
            name="ck_doctors_verification_status",
        ),
        sa.CheckConstraint("fee_paise >= 0", name="ck_doctors_fee_non_negative"),
        sa.CheckConstraint("years_experience BETWEEN 0 AND 70", name="ck_doctors_experience"),
        sa.CheckConstraint("rating_avg BETWEEN 0 AND 5", name="ck_doctors_rating"),
    )
    # Search only ever looks at bookable doctors, so index just those rows.
    bookable = sa.text("verification_status = 'verified' AND is_accepting")
    op.create_index(
        "ix_doctors_search_vector",
        "doctors",
        ["search_vector"],
        postgresql_using="gin",
        postgresql_where=bookable,
    )
    op.create_index(
        "ix_doctors_specialties",
        "doctors",
        ["specialties"],
        postgresql_using="gin",
        postgresql_where=bookable,
    )
    op.create_index(
        "ix_doctors_languages",
        "doctors",
        ["languages"],
        postgresql_using="gin",
        postgresql_where=bookable,
    )
    op.create_index(
        "ix_doctors_rating",
        "doctors",
        [sa.text("rating_avg DESC"), "user_id"],
        postgresql_where=bookable,
    )
    op.create_index(
        "ix_doctors_fee", "doctors", ["fee_paise", "user_id"], postgresql_where=bookable
    )
    op.create_index(
        "ix_doctors_verification_status", "doctors", ["verification_status", "created_at"]
    )
    for statement in SEARCH_DDL:
        op.execute(statement)

    op.create_table(
        "availability_slots",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "doctor_id", UUID, sa.ForeignKey("doctors.user_id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("start_at", TS, nullable=False),
        sa.Column("end_at", TS, nullable=False),
        sa.Column("mode", sa.String(16), nullable=False, server_default="video"),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("held_by", UUID, sa.ForeignKey("users.id")),
        sa.Column("hold_expires_at", TS),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("end_at > start_at", name="ck_availability_slots_window"),
        sa.CheckConstraint(
            "end_at - start_at BETWEEN interval '10 minutes' AND interval '2 hours'",
            name="ck_availability_slots_duration",
        ),
        sa.CheckConstraint("mode IN ('video','audio','chat')", name="ck_availability_slots_mode"),
        sa.CheckConstraint(
            "status IN ('open','held','booked')", name="ck_availability_slots_status"
        ),
        sa.CheckConstraint(
            "(status = 'held') = (held_by IS NOT NULL AND hold_expires_at IS NOT NULL)",
            name="ck_availability_slots_hold_fields",
        ),
    )
    # The database itself guarantees a doctor never has two overlapping slots.
    op.execute(
        """
        ALTER TABLE availability_slots ADD CONSTRAINT ex_availability_slots_no_overlap
        EXCLUDE USING gist (doctor_id WITH =, tstzrange(start_at, end_at, '[)') WITH &&)
        """
    )
    op.create_index(
        "ix_availability_slots_doctor_start", "availability_slots", ["doctor_id", "start_at"]
    )
    # "Next available" lookups and the hold-expiry sweeper.
    op.create_index(
        "ix_availability_slots_open",
        "availability_slots",
        ["doctor_id", "start_at"],
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index(
        "ix_availability_slots_hold_expiry",
        "availability_slots",
        ["hold_expires_at"],
        postgresql_where=sa.text("status = 'held'"),
    )


def downgrade() -> None:
    op.drop_table("availability_slots")
    op.execute("DROP TRIGGER IF EXISTS doctors_search_vector ON doctors")
    op.execute("DROP FUNCTION IF EXISTS doctors_search_vector_refresh()")
    op.drop_table("doctors")
