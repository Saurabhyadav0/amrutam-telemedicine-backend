import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core.db import utcnow
from app.core.deps import ReadSessionDep, RequestContextDep, SessionDep
from app.core.errors import NotFound
from app.modules.audit.service import AuditService
from app.modules.auth.dependencies import AuthenticatedUser, require_roles
from app.modules.outbox.models import OutboxEvent, OutboxStatus
from app.modules.users.models import UserRole
from app.modules.users.router import UserServiceDep
from app.modules.users.schemas import RoleUpdate, UserOut, UserPage

router = APIRouter(prefix="/admin", tags=["admin"])

AdminUser = Annotated[AuthenticatedUser, Depends(require_roles(UserRole.ADMIN))]
AuditReader = Annotated[AuthenticatedUser, Depends(require_roles(UserRole.ADMIN, UserRole.SUPPORT))]


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: int
    occurred_at: datetime
    actor_id: uuid.UUID | None
    actor_role: str | None
    action: str
    resource_type: str | None
    resource_id: str | None
    ip: str | None
    request_id: str | None
    metadata: dict[str, Any] = Field(validation_alias="metadata_")
    hash: str


class AuditChainOut(BaseModel):
    checked: int
    valid: bool
    first_invalid_id: int | None


@router.get("/users", response_model=UserPage)
async def list_users(
    _: AdminUser,
    service: UserServiceDep,
    role: UserRole | None = None,
    cursor: datetime | None = Query(default=None, description="next_cursor from previous page"),
    limit: int = Query(default=50, ge=1, le=200),
) -> UserPage:
    return await service.list_users(role=role, cursor=cursor, limit=limit)


@router.patch("/users/{user_id}/role", response_model=UserOut)
async def change_role(
    user_id: uuid.UUID,
    body: RoleUpdate,
    admin: AdminUser,
    service: UserServiceDep,
    ctx: RequestContextDep,
) -> UserOut:
    return await service.change_role(admin.user, user_id, body.role, ctx)


@router.get("/audit-logs", response_model=list[AuditLogOut])
async def list_audit_logs(
    _: AuditReader,
    session: ReadSessionDep,
    actor_id: uuid.UUID | None = None,
    action: str | None = Query(default=None, max_length=64),
    resource_type: str | None = Query(default=None, max_length=64),
    resource_id: str | None = Query(default=None, max_length=64),
    since: datetime | None = None,
    until: datetime | None = None,
    before_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[AuditLogOut]:
    rows = await AuditService(session).list(
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        since=since,
        until=until,
        before_id=before_id,
        limit=limit,
    )
    return [AuditLogOut.model_validate(row) for row in rows]


@router.get("/audit-logs/verify", response_model=AuditChainOut)
async def verify_audit_chain(_: AdminUser, session: SessionDep) -> AuditChainOut:
    result = await AuditService(session).verify_chain()
    return AuditChainOut(
        checked=result.checked, valid=result.valid, first_invalid_id=result.first_invalid_id
    )


class OutboxEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    topic: str
    aggregate_id: str
    status: OutboxStatus
    attempts: int
    last_error: str | None
    created_at: datetime
    next_attempt_at: datetime


@router.get("/outbox", response_model=list[OutboxEventOut])
async def list_outbox(
    _: AdminUser,
    session: SessionDep,
    outbox_status: OutboxStatus = Query(default=OutboxStatus.DEAD, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[OutboxEventOut]:
    """The dead-letter queue (status=dead): events whose handlers kept failing."""
    rows = await session.scalars(
        select(OutboxEvent)
        .where(OutboxEvent.status == outbox_status)
        .order_by(OutboxEvent.id.desc())
        .limit(limit)
    )
    return [OutboxEventOut.model_validate(r) for r in rows]


@router.post("/outbox/{event_id}/retry", response_model=OutboxEventOut)
async def retry_outbox_event(
    event_id: int, admin: AdminUser, session: SessionDep, ctx: RequestContextDep
) -> OutboxEventOut:
    event = await session.scalar(
        select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update()
    )
    if event is None or event.status != OutboxStatus.DEAD:
        raise NotFound("No dead-lettered event with this id")
    event.status = OutboxStatus.PENDING
    event.attempts = 0
    event.next_attempt_at = utcnow()
    await AuditService(session).record(
        "outbox.retried",
        ctx=ctx,
        actor_id=admin.user.id,
        actor_role=admin.user.role.value,
        resource_type="outbox_event",
        resource_id=str(event_id),
        metadata={"topic": event.topic},
    )
    await session.commit()
    return OutboxEventOut.model_validate(event)
