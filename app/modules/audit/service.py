import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import utcnow
from app.core.deps import RequestContext
from app.modules.audit.models import AuditChainHead, AuditLog

GENESIS_HASH = "0" * 64
CHAIN_HEAD_ID = 1


def compute_hash(prev_hash: str, entry: AuditLog) -> str:
    canonical = json.dumps(
        {
            "id": entry.id,
            "occurred_at": entry.occurred_at.isoformat(),
            "actor_id": str(entry.actor_id) if entry.actor_id else None,
            "actor_role": entry.actor_role,
            "action": entry.action,
            "resource_type": entry.resource_type,
            "resource_id": entry.resource_id,
            "ip": entry.ip,
            "request_id": entry.request_id,
            "metadata": entry.metadata_,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(f"{prev_hash}{canonical}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ChainVerification:
    checked: int
    valid: bool
    first_invalid_id: int | None


class AuditService:
    """Writes audit entries inside the caller's transaction, so the business change and its
    audit record commit (or roll back) together."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        action: str,
        *,
        ctx: RequestContext | None,
        actor_id: uuid.UUID | None = None,
        actor_role: str | None = None,
        resource_type: str | None = None,
        resource_id: str | uuid.UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AuditLog:
        # Row lock on the chain head serialises appends; it is held only until commit,
        # so audit calls should be the last step before committing.
        head = await self._session.scalar(
            select(AuditChainHead).where(AuditChainHead.id == CHAIN_HEAD_ID).with_for_update()
        )
        if head is None:
            raise RuntimeError("audit_chain_head is not initialised; run migrations")
        next_id = await self._session.scalar(text("SELECT nextval('audit_logs_id_seq')"))
        entry = AuditLog(
            id=next_id,
            occurred_at=utcnow(),
            actor_id=actor_id,
            actor_role=actor_role,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            ip=ctx.ip if ctx else None,
            user_agent=ctx.user_agent if ctx else None,
            request_id=ctx.request_id if ctx else None,
            metadata_=metadata or {},
            prev_hash=head.last_hash,
        )
        entry.hash = compute_hash(head.last_hash, entry)
        head.last_id = entry.id
        head.last_hash = entry.hash
        self._session.add(entry)
        return entry

    async def list(
        self,
        *,
        actor_id: uuid.UUID | None = None,
        action: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        before_id: int | None = None,
        limit: int = 50,
    ) -> list[AuditLog]:
        stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
        if actor_id:
            stmt = stmt.where(AuditLog.actor_id == actor_id)
        if action:
            stmt = stmt.where(AuditLog.action == action)
        if resource_type:
            stmt = stmt.where(AuditLog.resource_type == resource_type)
        if resource_id:
            stmt = stmt.where(AuditLog.resource_id == resource_id)
        if since:  # time bounds let Postgres prune monthly partitions
            stmt = stmt.where(AuditLog.occurred_at >= since)
        if until:
            stmt = stmt.where(AuditLog.occurred_at < until)
        if before_id:
            stmt = stmt.where(AuditLog.id < before_id)
        return list((await self._session.scalars(stmt)).all())

    async def verify_chain(self, *, max_rows: int = 100_000) -> ChainVerification:
        stmt = (
            select(AuditLog).order_by(AuditLog.id).limit(max_rows).execution_options(yield_per=1000)
        )
        expected_prev = GENESIS_HASH
        checked = 0
        async for entry in await self._session.stream_scalars(stmt):
            checked += 1
            if (
                entry.prev_hash != expected_prev
                or compute_hash(entry.prev_hash, entry) != entry.hash
            ):
                return ChainVerification(checked=checked, valid=False, first_invalid_id=entry.id)
            expected_prev = entry.hash
        return ChainVerification(checked=checked, valid=True, first_invalid_id=None)
