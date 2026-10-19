import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.cache import Cache
from app.core.deps import (
    FieldCipherDep,
    RedisDep,
    RequestContextDep,
    SessionDep,
    SettingsDep,
    TokenServiceDep,
    get_payment_gateway,
)
from app.core.rate_limit import rate_limit
from app.middleware.idempotency import require_idempotency_key
from app.modules.auth.dependencies import AuthenticatedUser, CurrentUser, require_roles
from app.modules.consultations.models import ConsultationStatus
from app.modules.consultations.schemas import (
    BookingIn,
    BookingOut,
    CancelIn,
    CancelOut,
    CompleteIn,
    ConsultationOut,
    ConsultationPage,
    JoinInfo,
    ReviewIn,
)
from app.modules.consultations.service import ConsultationService
from app.modules.payments.gateway import PaymentGateway
from app.modules.payments.service import PaymentService
from app.modules.users.models import UserRole

router = APIRouter(prefix="/consultations", tags=["consultations"])

Patient = Annotated[AuthenticatedUser, Depends(require_roles(UserRole.PATIENT))]
Doctor = Annotated[AuthenticatedUser, Depends(require_roles(UserRole.DOCTOR))]
Participant = Annotated[
    AuthenticatedUser,
    Depends(require_roles(UserRole.PATIENT, UserRole.DOCTOR, UserRole.ADMIN, UserRole.SUPPORT)),
]


def get_consultation_service(
    session: SessionDep,
    settings: SettingsDep,
    cipher: FieldCipherDep,
    tokens: TokenServiceDep,
    redis: RedisDep,
    gateway: Annotated[PaymentGateway, Depends(get_payment_gateway)],
) -> ConsultationService:
    return ConsultationService(
        session,
        settings=settings,
        cipher=cipher,
        tokens=tokens,
        payments=PaymentService(session, settings=settings, gateway=gateway),
        cache=Cache(redis),
    )


ServiceDep = Annotated[ConsultationService, Depends(get_consultation_service)]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=BookingOut,
    dependencies=[
        Depends(require_idempotency_key),
        Depends(rate_limit("booking", limit=30, window_seconds=60)),
    ],
)
async def book_consultation(
    body: BookingIn, patient: Patient, service: ServiceDep, ctx: RequestContextDep
) -> BookingOut:
    """Hold the slot and start payment. The slot is held for 10 minutes; the consultation is
    confirmed by the payment webhook. Retrying (same key or same slot) resumes the booking."""
    return await service.book(patient.user, body, ctx)


@router.get("", response_model=ConsultationPage)
async def list_my_consultations(
    current: CurrentUser,
    service: ServiceDep,
    consultation_status: Annotated[ConsultationStatus | None, Query(alias="status")] = None,
    before: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ConsultationPage:
    return await service.list(current.user, status=consultation_status, before=before, limit=limit)


@router.get("/{consultation_id}", response_model=ConsultationOut)
async def get_consultation(
    consultation_id: uuid.UUID, actor: Participant, service: ServiceDep, ctx: RequestContextDep
) -> ConsultationOut:
    return await service.get(actor.user, consultation_id, ctx)


@router.post(
    "/{consultation_id}/cancel",
    response_model=CancelOut,
    dependencies=[Depends(require_idempotency_key)],
)
async def cancel_consultation(
    consultation_id: uuid.UUID,
    body: CancelIn,
    actor: Participant,
    service: ServiceDep,
    ctx: RequestContextDep,
) -> CancelOut:
    """Patients get a full refund up to 2h before the start; doctor/admin cancellations are
    always refunded. The slot reopens for other patients."""
    return await service.cancel(actor.user, consultation_id, body.reason, ctx)


@router.post("/{consultation_id}/start", response_model=JoinInfo)
async def start_consultation(
    consultation_id: uuid.UUID, doctor: Doctor, service: ServiceDep, ctx: RequestContextDep
) -> JoinInfo:
    return await service.start(doctor.user, consultation_id, ctx)


@router.get("/{consultation_id}/join", response_model=JoinInfo)
async def join_consultation(
    consultation_id: uuid.UUID, actor: Participant, service: ServiceDep
) -> JoinInfo:
    return await service.join(actor.user, consultation_id)


@router.post("/{consultation_id}/complete", response_model=ConsultationOut)
async def complete_consultation(
    consultation_id: uuid.UUID,
    body: CompleteIn,
    doctor: Doctor,
    service: ServiceDep,
    ctx: RequestContextDep,
) -> ConsultationOut:
    return await service.complete(doctor.user, consultation_id, body.notes, ctx)


@router.post("/{consultation_id}/no-show", response_model=ConsultationOut)
async def mark_no_show(
    consultation_id: uuid.UUID, doctor: Doctor, service: ServiceDep, ctx: RequestContextDep
) -> ConsultationOut:
    return await service.mark_no_show(doctor.user, consultation_id, ctx)


@router.post(
    "/{consultation_id}/review",
    response_model=ConsultationOut,
    dependencies=[Depends(require_idempotency_key)],
)
async def review_consultation(
    consultation_id: uuid.UUID,
    body: ReviewIn,
    patient: Patient,
    service: ServiceDep,
    ctx: RequestContextDep,
) -> ConsultationOut:
    return await service.review(patient.user, consultation_id, body.rating, body.review, ctx)
