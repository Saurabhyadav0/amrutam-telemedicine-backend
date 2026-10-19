import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.modules.availability.models import ConsultationMode
from app.modules.consultations.models import ConsultationStatus
from app.modules.payments.schemas import PaymentOut
from app.modules.users.schemas import StrictModel


class BookingIn(StrictModel):
    slot_id: uuid.UUID
    chief_complaint: str | None = Field(default=None, max_length=1000)
    consent_to_teleconsultation: Literal[True] = Field(
        description="Explicit patient consent required by TPG 2020 before a teleconsultation"
    )


class ConsultationOut(BaseModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    doctor_id: uuid.UUID
    doctor_name: str
    slot_id: uuid.UUID
    mode: ConsultationMode
    status: ConsultationStatus
    scheduled_start: datetime
    scheduled_end: datetime
    fee_paise: int
    chief_complaint: str | None
    cancel_reason: str | None
    rating: int | None
    version: int


class BookingOut(BaseModel):
    consultation: ConsultationOut
    payment: PaymentOut | None
    checkout_url: str | None
    hold_expires_at: datetime | None


class ConsultationPage(BaseModel):
    items: list[ConsultationOut]
    next_cursor: datetime | None


class CancelIn(StrictModel):
    reason: str = Field(min_length=3, max_length=64)


class CancelOut(BaseModel):
    consultation: ConsultationOut
    refund_paise: int


class CompleteIn(StrictModel):
    notes: str | None = Field(default=None, max_length=5000)


class JoinInfo(BaseModel):
    """Video/audio/chat room credentials. The room token is short-lived and per-user."""

    consultation_id: uuid.UUID
    room: str
    token: str
    mode: ConsultationMode
    expires_in: int


class ReviewIn(StrictModel):
    rating: int = Field(ge=1, le=5)
    review: str | None = Field(default=None, max_length=1000)
