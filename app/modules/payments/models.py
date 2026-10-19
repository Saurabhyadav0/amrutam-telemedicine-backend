import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin, UUIDPrimaryKeyMixin, utcnow
from app.modules.users.models import str_enum


class PaymentStatus(StrEnum):
    CREATED = "created"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    REFUND_PENDING = "refund_pending"
    REFUNDED = "refunded"


class Payment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "payments"

    consultation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("consultations.id"), unique=True)
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    amount_paise: Mapped[int]
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    status: Mapped[PaymentStatus] = mapped_column(
        str_enum(PaymentStatus), default=PaymentStatus.CREATED
    )
    provider: Mapped[str] = mapped_column(String(32), default="mockpay")
    provider_payment_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    provider_refund_id: Mapped[str | None] = mapped_column(String(64))
    refund_amount_paise: Mapped[int | None]
    failure_reason: Mapped[str | None] = mapped_column(String(64))


class PaymentEvent(Base):
    """Webhook inbox. The (provider, event_id) primary key makes redelivered webhooks no-ops."""

    __tablename__ = "payment_events"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(default=utcnow)
