import uuid
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import AwareDatetime

from app.core.cache import Cache
from app.core.db import utcnow
from app.core.deps import ReadSessionDep, RedisDep, RequestContextDep, SessionDep
from app.core.errors import BadRequest
from app.core.rate_limit import rate_limit
from app.middleware.idempotency import require_idempotency_key
from app.modules.auth.dependencies import AuthenticatedUser, require_roles
from app.modules.availability.models import SlotStatus
from app.modules.availability.schemas import PublicSlotOut, SlotBatchIn, SlotBatchOut, SlotOut
from app.modules.availability.service import AvailabilityService
from app.modules.doctors.models import AYURVEDA_SPECIALTIES, LANGUAGES, VerificationStatus
from app.modules.doctors.schemas import (
    AcceptingUpdate,
    DoctorAdminPage,
    DoctorOwnOut,
    DoctorProfileIn,
    DoctorPublic,
    DoctorSearchPage,
    DoctorSearchParams,
    DoctorSort,
    VerificationDecision,
)
from app.modules.doctors.service import DoctorService
from app.modules.users.models import UserRole

DoctorUser = Annotated[AuthenticatedUser, Depends(require_roles(UserRole.DOCTOR))]
AdminUser = Annotated[AuthenticatedUser, Depends(require_roles(UserRole.ADMIN))]
DEFAULT_WINDOW = timedelta(days=7)
MAX_SEARCH_OFFSET = 1000  # deep pagination belongs to filters, not page 500


def get_doctor_service(session: SessionDep, redis: RedisDep) -> DoctorService:
    return DoctorService(session, Cache(redis))


def get_public_doctor_service(session: ReadSessionDep, redis: RedisDep) -> DoctorService:
    return DoctorService(session, Cache(redis))


def get_availability_service(session: SessionDep, redis: RedisDep) -> AvailabilityService:
    return AvailabilityService(session, Cache(redis))


def get_public_availability_service(
    session: ReadSessionDep, redis: RedisDep
) -> AvailabilityService:
    return AvailabilityService(session, Cache(redis))


DoctorServiceDep = Annotated[DoctorService, Depends(get_doctor_service)]
PublicDoctorServiceDep = Annotated[DoctorService, Depends(get_public_doctor_service)]
AvailabilityServiceDep = Annotated[AvailabilityService, Depends(get_availability_service)]
PublicAvailabilityDep = Annotated[AvailabilityService, Depends(get_public_availability_service)]


def _window(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    start = start or utcnow()
    return start, end or start + DEFAULT_WINDOW


# ---- doctor self-service: /doctors/me ----------------------------------------------------

me_router = APIRouter(prefix="/doctors/me", tags=["doctor self-service"])


@me_router.get("", response_model=DoctorOwnOut)
async def get_my_practice(current: DoctorUser, service: DoctorServiceDep) -> DoctorOwnOut:
    return await service.get_own(current.user)


@me_router.put("", response_model=DoctorOwnOut)
async def submit_my_practice(
    body: DoctorProfileIn, current: DoctorUser, service: DoctorServiceDep, ctx: RequestContextDep
) -> DoctorOwnOut:
    """Create or replace the practice profile. Changing registration details on a verified
    profile sends it back to ``pending`` for re-verification."""
    return await service.upsert_own(current.user, body, ctx)


@me_router.patch("/accepting", response_model=DoctorOwnOut)
async def toggle_accepting(
    body: AcceptingUpdate, current: DoctorUser, service: DoctorServiceDep, ctx: RequestContextDep
) -> DoctorOwnOut:
    return await service.set_accepting(current.user, body.is_accepting, ctx)


@me_router.post(
    "/slots",
    status_code=status.HTTP_201_CREATED,
    response_model=SlotBatchOut,
    dependencies=[Depends(require_idempotency_key)],
)
async def publish_slots(
    body: SlotBatchIn, current: DoctorUser, service: AvailabilityServiceDep, ctx: RequestContextDep
) -> SlotBatchOut:
    return await service.publish(current.user, body, ctx)


@me_router.get("/slots", response_model=list[SlotOut])
async def list_my_slots(
    current: DoctorUser,
    service: AvailabilityServiceDep,
    start: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    end: Annotated[AwareDatetime | None, Query(alias="to")] = None,
    slot_status: Annotated[SlotStatus | None, Query(alias="status")] = None,
) -> list[SlotOut]:
    return await service.list_own(current.user, *_window(start, end), slot_status)


@me_router.delete("/slots/{slot_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_slot(
    slot_id: uuid.UUID, current: DoctorUser, service: AvailabilityServiceDep, ctx: RequestContextDep
) -> Response:
    await service.remove(current.user, slot_id, ctx)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- public discovery: /doctors ----------------------------------------------------------

public_router = APIRouter(
    prefix="/doctors",
    tags=["doctor discovery"],
    dependencies=[Depends(rate_limit("discovery", limit=120, window_seconds=60))],
)


def search_params(
    q: Annotated[
        str | None, Query(max_length=100, description="Name, specialty or keyword")
    ] = None,
    specialty: str | None = None,
    language: str | None = None,
    min_fee: Annotated[int | None, Query(ge=0)] = None,
    max_fee: Annotated[int | None, Query(ge=0)] = None,
    min_rating: Annotated[float | None, Query(ge=0, le=5)] = None,
    available_from: AwareDatetime | None = None,
    available_to: AwareDatetime | None = None,
    sort: DoctorSort = DoctorSort.RELEVANCE,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0, le=MAX_SEARCH_OFFSET)] = 0,
) -> DoctorSearchParams:
    if specialty is not None and specialty not in AYURVEDA_SPECIALTIES:
        raise BadRequest(f"Unknown specialty {specialty!r}", code="unknown_specialty")
    if language is not None and language not in LANGUAGES:
        raise BadRequest(f"Unknown language {language!r}", code="unknown_language")
    if min_fee is not None and max_fee is not None and min_fee > max_fee:
        raise BadRequest("min_fee must not exceed max_fee", code="invalid_fee_range")
    return DoctorSearchParams(
        q=q.strip() if q and q.strip() else None,
        specialty=specialty,
        language=language,
        min_fee=min_fee,
        max_fee=max_fee,
        min_rating=min_rating,
        available_from=available_from,
        available_to=available_to,
        sort=sort,
        limit=limit,
        offset=offset,
    )


@public_router.get("", response_model=DoctorSearchPage)
async def search_doctors(
    params: Annotated[DoctorSearchParams, Depends(search_params)], service: PublicDoctorServiceDep
) -> DoctorSearchPage:
    """Search verified doctors who are accepting consultations. Cached for up to 30s."""
    return await service.search(params)


@public_router.get("/specialties", response_model=list[str])
async def list_specialties() -> list[str]:
    return sorted(AYURVEDA_SPECIALTIES)


@public_router.get("/{doctor_id}", response_model=DoctorPublic)
async def get_doctor(doctor_id: uuid.UUID, service: PublicDoctorServiceDep) -> DoctorPublic:
    return await service.get_public(doctor_id)


@public_router.get("/{doctor_id}/slots", response_model=list[PublicSlotOut])
async def list_doctor_slots(
    doctor_id: uuid.UUID,
    service: PublicAvailabilityDep,
    start: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    end: Annotated[AwareDatetime | None, Query(alias="to")] = None,
) -> list[PublicSlotOut]:
    """Open slots only. Not cached: this is what the patient picks from right before booking."""
    return await service.list_public(doctor_id, *_window(start, end))


# ---- admin verification: /admin/doctors ---------------------------------------------------

admin_router = APIRouter(prefix="/admin/doctors", tags=["admin"])


@admin_router.get("", response_model=DoctorAdminPage)
async def list_doctors_for_review(
    _: AdminUser,
    service: DoctorServiceDep,
    verification_status: Annotated[VerificationStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DoctorAdminPage:
    return await service.list_for_review(verification_status, limit=limit, offset=offset)


@admin_router.post("/{doctor_id}/verification", response_model=DoctorOwnOut)
async def decide_verification(
    doctor_id: uuid.UUID,
    body: VerificationDecision,
    admin: AdminUser,
    service: DoctorServiceDep,
    ctx: RequestContextDep,
) -> DoctorOwnOut:
    """Approve after checking the registration against the NCISM / State Board register,
    or reject (also used to suspend) with a reason. Fully audited."""
    return await service.decide_verification(admin.user, doctor_id, body, ctx)
