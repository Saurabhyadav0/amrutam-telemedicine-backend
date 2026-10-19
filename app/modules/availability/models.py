import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.modules.users.models import str_enum


class SlotStatus(StrEnum):
    OPEN = "open"
    HELD = "held"  # reserved during checkout, expires at hold_expires_at
    BOOKED = "booked"


class ConsultationMode(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"
    CHAT = "chat"


class AvailabilitySlot(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A bookable time window. Overlapping slots for one doctor are impossible: a GiST
    exclusion constraint on ``(doctor_id, tstzrange(start_at, end_at))`` enforces it in the DB.

    ``version`` increments on every state change (optimistic concurrency for clients).
    """

    __tablename__ = "availability_slots"

    doctor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("doctors.user_id", ondelete="CASCADE"))
    start_at: Mapped[datetime]
    end_at: Mapped[datetime]
    mode: Mapped[ConsultationMode] = mapped_column(
        str_enum(ConsultationMode), default=ConsultationMode.VIDEO
    )
    status: Mapped[SlotStatus] = mapped_column(str_enum(SlotStatus), default=SlotStatus.OPEN)
    held_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    hold_expires_at: Mapped[datetime | None]
    version: Mapped[int] = mapped_column(default=1)
