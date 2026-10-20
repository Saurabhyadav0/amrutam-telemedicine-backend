from datetime import date, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status

from app.core.deps import ReadSessionDep, RequestContextDep, SessionDep
from app.modules.analytics.schemas import (
    DailyRow,
    DoctorPerformance,
    Overview,
    RefreshAccepted,
    RefreshIn,
    SpecialtyDemand,
)
from app.modules.analytics.service import AnalyticsService, business_today, validate_range
from app.modules.audit.service import AuditService
from app.modules.auth.dependencies import AuthenticatedUser, require_roles
from app.modules.outbox.service import emit
from app.modules.users.models import UserRole

router = APIRouter(prefix="/admin/analytics", tags=["admin analytics"])
AdminUser = Annotated[AuthenticatedUser, Depends(require_roles(UserRole.ADMIN))]


def get_analytics(session: ReadSessionDep) -> AnalyticsService:
    return AnalyticsService(session)  # rollups are read from the replica


AnalyticsDep = Annotated[AnalyticsService, Depends(get_analytics)]


def date_range(
    from_day: Annotated[date | None, Query(alias="from")] = None,
    to_day: Annotated[date | None, Query(alias="to")] = None,
) -> tuple[date, date]:
    """Defaults to the last 30 business days (IST), inclusive of today."""
    to_day = to_day or business_today()
    return from_day or to_day - timedelta(days=29), to_day


RangeDep = Annotated[tuple[date, date], Depends(date_range)]


@router.get("/overview", response_model=Overview)
async def overview(_: AdminUser, service: AnalyticsDep, days: RangeDep) -> Overview:
    return await service.overview(*days)


@router.get("/daily", response_model=list[DailyRow])
async def daily(_: AdminUser, service: AnalyticsDep, days: RangeDep) -> list[DailyRow]:
    return await service.daily(*days)


@router.get("/doctors", response_model=list[DoctorPerformance])
async def doctors(
    _: AdminUser,
    service: AnalyticsDep,
    days: RangeDep,
    sort: Literal["revenue", "completed", "utilization"] = "revenue",
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> list[DoctorPerformance]:
    return await service.doctors(*days, sort=sort, limit=limit)


@router.get("/specialties", response_model=list[SpecialtyDemand])
async def specialties(_: AdminUser, service: AnalyticsDep, days: RangeDep) -> list[SpecialtyDemand]:
    return await service.specialties(*days)


@router.post("/refresh", status_code=status.HTTP_202_ACCEPTED, response_model=RefreshAccepted)
async def request_refresh(
    body: RefreshIn, admin: AdminUser, session: SessionDep, ctx: RequestContextDep
) -> RefreshAccepted:
    """Backfill/recompute a range. Heavy, so it runs on the worker via the outbox."""
    validate_range(body.from_day, body.to_day)
    emit(
        session,
        "analytics.refresh_requested",
        f"{body.from_day}:{body.to_day}",
        {"from_day": body.from_day.isoformat(), "to_day": body.to_day.isoformat()},
    )
    await AuditService(session).record(
        "analytics.refresh_requested",
        ctx=ctx,
        actor_id=admin.user.id,
        actor_role=admin.user.role.value,
        metadata=body.model_dump(mode="json"),
    )
    await session.commit()
    return RefreshAccepted(from_day=body.from_day, to_day=body.to_day)
