import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, SmallInteger, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class AuditLog(Base):
    """Append-only, hash-chained audit trail. Range-partitioned by month on ``occurred_at``.

    UPDATE/DELETE are blocked by a trigger (see the initial migration). Each row stores
    ``hash = sha256(prev_hash || canonical(row))`` so any edit or deletion breaks the chain.
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(primary_key=True)
    actor_id: Mapped[uuid.UUID | None]
    actor_role: Mapped[str | None] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(64))
    ip: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    request_id: Mapped[str | None] = mapped_column(String(64))
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class AuditChainHead(Base):
    """Single row holding the chain tip. ``SELECT .. FOR UPDATE`` on it serialises appends."""

    __tablename__ = "audit_chain_head"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    last_id: Mapped[int] = mapped_column(BigInteger)
    last_hash: Mapped[str] = mapped_column(String(64))
