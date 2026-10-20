import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.outbox.models import OutboxEvent


def emit(
    session: AsyncSession, topic: str, aggregate_id: uuid.UUID | str, payload: dict[str, Any]
) -> None:
    """Queue an event inside the caller's transaction (commits or rolls back with it).
    Payloads carry ids, never PHI: handlers re-read what they need."""
    session.add(OutboxEvent(topic=topic, aggregate_id=str(aggregate_id), payload=payload))
