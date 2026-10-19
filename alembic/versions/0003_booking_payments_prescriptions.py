"""consultations, payments + webhook inbox, prescriptions, medicine catalogue, outbox

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-21
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
UUID = postgresql.UUID(as_uuid=True)

# Starter catalogue. Lists follow TPG 2020: Schedule E(1) poisonous drugs (Vatsanabha/aconite,
# Kupilu/nux-vomica, Bhallataka) are List B; mineral/metal rasa preparations that need
# supervision are List A; common classical formulations are List O.
MEDICINES = [
    ("Triphala Churna", "churna", "O", "AMR-TRIPHALA"),
    ("Ashwagandha Churna", "churna", "O", "AMR-ASHWAGANDHA"),
    ("Avipattikar Churna", "churna", "O", None),
    ("Hingvastak Churna", "churna", "O", None),
    ("Sitopaladi Churna", "churna", "O", None),
    ("Chyawanprash", "avaleha", "O", "AMR-CHYAWANPRASH"),
    ("Dashmool Kwatha", "kwatha", "O", None),
    ("Mahanarayan Taila", "taila", "O", None),
    ("Brahmi Ghrita", "ghrita", "O", None),
    ("Yogaraja Guggulu", "vati", "A", None),
    ("Kaishore Guggulu", "vati", "A", None),
    ("Arogyavardhini Vati", "vati", "A", None),
    ("Sutashekhar Rasa", "vati", "A", None),
    ("Tribhuvan Kirti Rasa", "vati", "B", None),
    ("Agnitundi Vati", "vati", "B", None),
    ("Bhallatakadi Modaka", "modaka", "B", None),
]


def upgrade() -> None:
    op.create_table(
        "consultations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("patient_id", UUID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("doctor_id", UUID, sa.ForeignKey("doctors.user_id"), nullable=False),
        sa.Column("slot_id", UUID, sa.ForeignKey("availability_slots.id"), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending_payment"),
        sa.Column("scheduled_start", TS, nullable=False),
        sa.Column("scheduled_end", TS, nullable=False),
        sa.Column("fee_paise", sa.Integer, nullable=False),
        sa.Column("chief_complaint_enc", sa.Text),
        sa.Column("doctor_notes_enc", sa.Text),
        sa.Column("cancel_reason", sa.String(64)),
        sa.Column("cancelled_by", UUID, sa.ForeignKey("users.id")),
        sa.Column("cancelled_at", TS),
        sa.Column("started_at", TS),
        sa.Column("completed_at", TS),
        sa.Column("rating", sa.SmallInteger),
        sa.Column("review", sa.String(1000)),
        sa.Column("rated_at", TS),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('pending_payment','confirmed','in_progress','completed','cancelled','no_show')",
            name="ck_consultations_status",
        ),
        sa.CheckConstraint(
            "rating IS NULL OR rating BETWEEN 1 AND 5", name="ck_consultations_rating"
        ),
        sa.CheckConstraint("scheduled_end > scheduled_start", name="ck_consultations_window"),
    )
    # Second line of defence behind the slot hold: one live consultation per slot...
    op.create_index(
        "uq_consultations_active_slot",
        "consultations",
        ["slot_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'cancelled'"),
    )
    # ...and a patient can't be in two consultations at once.
    op.execute(
        """
        ALTER TABLE consultations ADD CONSTRAINT ex_consultations_patient_overlap
        EXCLUDE USING gist (patient_id WITH =, tstzrange(scheduled_start, scheduled_end, '[)') WITH &&)
        WHERE (status IN ('pending_payment', 'confirmed', 'in_progress'))
        """
    )
    op.create_index(
        "ix_consultations_patient", "consultations", ["patient_id", sa.text("scheduled_start DESC")]
    )
    op.create_index(
        "ix_consultations_doctor", "consultations", ["doctor_id", sa.text("scheduled_start DESC")]
    )
    op.create_index("ix_consultations_status_start", "consultations", ["status", "scheduled_start"])

    op.create_table(
        "payments",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("consultation_id", UUID, sa.ForeignKey("consultations.id"), nullable=False),
        sa.Column("patient_id", UUID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("amount_paise", sa.Integer, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="INR"),
        sa.Column("status", sa.String(16), nullable=False, server_default="created"),
        sa.Column("provider", sa.String(32), nullable=False, server_default="mockpay"),
        sa.Column("provider_payment_id", sa.String(64)),
        sa.Column("provider_refund_id", sa.String(64)),
        sa.Column("refund_amount_paise", sa.Integer),
        sa.Column("failure_reason", sa.String(64)),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("consultation_id", name="uq_payments_consultation_id"),
        sa.UniqueConstraint("provider_payment_id", name="uq_payments_provider_payment_id"),
        sa.CheckConstraint("amount_paise >= 0", name="ck_payments_amount"),
        sa.CheckConstraint(
            "status IN ('created','succeeded','failed','expired','cancelled','refund_pending','refunded')",
            name="ck_payments_status",
        ),
    )
    op.create_index("ix_payments_patient_id", "payments", ["patient_id"])
    op.create_index("ix_payments_status", "payments", ["status", "created_at"])

    op.create_table(
        "payment_events",
        sa.Column("provider", sa.String(32), primary_key=True),
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("received_at", TS, nullable=False, server_default=sa.func.now()),
    )

    medicines = op.create_table(
        "medicines",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("form", sa.String(32), nullable=False),
        sa.Column("tpg_list", sa.String(16), nullable=False),
        sa.Column("amrutam_sku", sa.String(32)),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.UniqueConstraint("name", name="uq_medicines_name"),
        sa.CheckConstraint("tpg_list IN ('O','A','B')", name="ck_medicines_tpg_list"),
    )
    op.bulk_insert(
        medicines,
        [
            {
                "id": uuid.uuid5(uuid.NAMESPACE_URL, f"amrutam:medicine:{name}"),
                "name": name,
                "form": form,
                "tpg_list": tpg,
                "amrutam_sku": sku,
                "is_active": True,
            }
            for name, form, tpg, sku in MEDICINES
        ],
    )

    op.create_table(
        "prescriptions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("consultation_id", UUID, sa.ForeignKey("consultations.id"), nullable=False),
        sa.Column("doctor_id", UUID, sa.ForeignKey("doctors.user_id"), nullable=False),
        sa.Column("patient_id", UUID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("document_enc", sa.Text, nullable=False),
        sa.Column("issued_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("signature", sa.String(128), nullable=False),
        sa.Column("signing_key_id", sa.String(16), nullable=False),
        sa.UniqueConstraint("consultation_id", name="uq_prescriptions_consultation_id"),
    )
    op.create_index("ix_prescriptions_patient_id", "prescriptions", ["patient_id"])
    op.create_index("ix_prescriptions_doctor_id", "prescriptions", ["doctor_id"])
    op.create_table(
        "prescription_items",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "prescription_id",
            UUID,
            sa.ForeignKey("prescriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("medicine_id", UUID, sa.ForeignKey("medicines.id"), nullable=False),
        sa.Column("position", sa.SmallInteger, nullable=False),
        sa.Column("dose", sa.String(64), nullable=False),
        sa.Column("frequency", sa.String(64), nullable=False),
        sa.Column("duration_days", sa.Integer, nullable=False),
        sa.Column("instructions", sa.String(500)),
        sa.CheckConstraint(
            "duration_days BETWEEN 1 AND 180", name="ck_prescription_items_duration"
        ),
    )
    op.create_index(
        "ix_prescription_items_prescription_id", "prescription_items", ["prescription_id"]
    )
    # Prescriptions are legal records: block edits at the database level too.
    op.execute(
        """
        CREATE TRIGGER prescriptions_immutable BEFORE UPDATE OR DELETE ON prescriptions
        FOR EACH ROW EXECUTE FUNCTION forbid_audit_mutation()
        """
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("topic", sa.String(64), nullable=False),
        sa.Column("aggregate_id", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_attempt_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("last_error", sa.Text),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("processed_at", TS),
        sa.CheckConstraint("status IN ('pending','done','dead')", name="ck_outbox_events_status"),
    )
    op.create_index(
        "ix_outbox_events_due",
        "outbox_events",
        ["next_attempt_at", "id"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_outbox_events_dead",
        "outbox_events",
        ["created_at"],
        postgresql_where=sa.text("status = 'dead'"),
    )


def downgrade() -> None:
    for table in (
        "outbox_events",
        "prescription_items",
        "prescriptions",
        "medicines",
        "payment_events",
        "payments",
        "consultations",
    ):
        op.drop_table(table)
