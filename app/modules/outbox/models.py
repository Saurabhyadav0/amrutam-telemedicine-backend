from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, Identity, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, utcnow
from app.modules.users.models import str_enum


class OutboxStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    DEAD = "dead"  # exhausted retries: the dead-letter queue, surfaced to admins


class OutboxEvent(Base):
    """Transactional outbox: written in the same transaction as the state change it
    describes, delivered at-least-once by the worker. Handlers must be idempotent."""

    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    topic: Mapped[str] = mapped_column(String(64))
    aggregate_id: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[OutboxStatus] = mapped_column(
        str_enum(OutboxStatus), default=OutboxStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    processed_at: Mapped[datetime | None]
