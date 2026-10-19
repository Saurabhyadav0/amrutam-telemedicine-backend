import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UUIDPrimaryKeyMixin, utcnow
from app.modules.users.models import str_enum


class MedicineList(StrEnum):
    """Telemedicine Practice Guidelines 2020, Annexure: what may be prescribed remotely."""

    O = "O"  # any mode (OTC / safe)  # noqa: E741
    A = "A"  # first prescription only after a video consult; refills in any mode
    B = "B"  # never via telemedicine (e.g. Schedule E(1) poisonous Ayurvedic drugs)


class Medicine(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "medicines"

    name: Mapped[str] = mapped_column(String(120), unique=True)
    form: Mapped[str] = mapped_column(String(32))  # churna, vati, kwatha, taila, ghrita...
    tpg_list: Mapped[MedicineList] = mapped_column(str_enum(MedicineList))
    amrutam_sku: Mapped[str | None] = mapped_column(String(32))
    is_active: Mapped[bool] = mapped_column(default=True)


class Prescription(UUIDPrimaryKeyMixin, Base):
    """Immutable once issued. ``signature`` is Ed25519 over the canonical document, so
    pharmacies can verify it with the published public key."""

    __tablename__ = "prescriptions"

    consultation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("consultations.id"), unique=True)
    doctor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("doctors.user_id"), index=True)
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    # The exact signed JSON, encrypted. Stored rather than rebuilt so later edits (doctor
    # name, patient age) can never invalidate the signature of a legal record.
    document_enc: Mapped[str] = mapped_column(Text)
    issued_at: Mapped[datetime] = mapped_column(default=utcnow)
    content_sha256: Mapped[str] = mapped_column(String(64))
    signature: Mapped[str] = mapped_column(String(128))
    signing_key_id: Mapped[str] = mapped_column(String(16))


class PrescriptionItem(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "prescription_items"

    prescription_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("prescriptions.id", ondelete="CASCADE"), index=True
    )
    medicine_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("medicines.id"))
    position: Mapped[int]  # order as written; part of the signed document
    dose: Mapped[str] = mapped_column(String(64))
    frequency: Mapped[str] = mapped_column(String(64))
    duration_days: Mapped[int]
    instructions: Mapped[str | None] = mapped_column(String(500))
