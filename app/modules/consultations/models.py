import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.modules.availability.models import ConsultationMode
from app.modules.users.models import str_enum


class ConsultationStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"
    CONFIRMED = "confirmed"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


# The only legal moves. Anything else is a 409, never a silent overwrite.
TRANSITIONS: dict[ConsultationStatus, frozenset[ConsultationStatus]] = {
    ConsultationStatus.PENDING_PAYMENT: frozenset(
        {ConsultationStatus.CONFIRMED, ConsultationStatus.CANCELLED}
    ),
    ConsultationStatus.CONFIRMED: frozenset(
        {ConsultationStatus.IN_PROGRESS, ConsultationStatus.CANCELLED, ConsultationStatus.NO_SHOW}
    ),
    ConsultationStatus.IN_PROGRESS: frozenset({ConsultationStatus.COMPLETED}),
    ConsultationStatus.COMPLETED: frozenset(),
    ConsultationStatus.CANCELLED: frozenset(),
    ConsultationStatus.NO_SHOW: frozenset(),
}
ACTIVE_STATUSES = frozenset(
    {
        ConsultationStatus.PENDING_PAYMENT,
        ConsultationStatus.CONFIRMED,
        ConsultationStatus.IN_PROGRESS,
    }
)


class Consultation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One booked teleconsultation.

    DB guarantees: one active consultation per slot (partial unique index) and no overlapping
    active consultations for a patient (exclusion constraint) - see migration 0003.
    """

    __tablename__ = "consultations"

    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    doctor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("doctors.user_id"))
    slot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("availability_slots.id"))
    mode: Mapped[ConsultationMode] = mapped_column(str_enum(ConsultationMode))
    status: Mapped[ConsultationStatus] = mapped_column(
        str_enum(ConsultationStatus), default=ConsultationStatus.PENDING_PAYMENT
    )
    scheduled_start: Mapped[datetime]
    scheduled_end: Mapped[datetime]
    fee_paise: Mapped[int]
    chief_complaint_enc: Mapped[str | None] = mapped_column(Text)
    doctor_notes_enc: Mapped[str | None] = mapped_column(Text)
    cancel_reason: Mapped[str | None] = mapped_column(String(64))
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    cancelled_at: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    rating: Mapped[int | None] = mapped_column(SmallInteger)
    review: Mapped[str | None] = mapped_column(String(1000))
    rated_at: Mapped[datetime | None]
    version: Mapped[int] = mapped_column(default=1)
