import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import select

from app.core.deps import RequestContextDep, SessionDep, SettingsDep, get_payment_gateway
from app.core.errors import NotFound
from app.modules.auth.dependencies import AuthenticatedUser, require_roles
from app.modules.payments.gateway import PaymentGateway
from app.modules.payments.models import Payment
from app.modules.payments.schemas import MockCompleteIn
from app.modules.payments.service import PaymentService, mock_gateway_event
from app.modules.users.models import UserRole

router = APIRouter(prefix="/payments", tags=["payments"])
Patient = Annotated[AuthenticatedUser, Depends(require_roles(UserRole.PATIENT))]


def get_payment_service(
    session: SessionDep,
    settings: SettingsDep,
    gateway: Annotated[PaymentGateway, Depends(get_payment_gateway)],
) -> PaymentService:
    return PaymentService(session, settings=settings, gateway=gateway)


ServiceDep = Annotated[PaymentService, Depends(get_payment_service)]


@router.post("/webhooks/mockpay", include_in_schema=True)
async def mockpay_webhook(
    request: Request,
    service: ServiceDep,
    ctx: RequestContextDep,
    signature: Annotated[str | None, Header(alias="X-MockPay-Signature")] = None,
) -> dict[str, str]:
    """Gateway → us. HMAC-signed and timestamped; duplicate deliveries are acknowledged and
    ignored. Always 200 after a valid signature so the gateway stops retrying."""
    outcome = await service.handle_webhook(await request.body(), signature, ctx)
    return {"status": outcome}


mock_router = APIRouter(prefix="/payments/mock", tags=["payments (dev only)"])


@mock_router.post("/{payment_id}/complete")
async def mock_complete_payment(
    payment_id: uuid.UUID,
    body: MockCompleteIn,
    patient: Patient,
    session: SessionDep,
    settings: SettingsDep,
    service: ServiceDep,
    ctx: RequestContextDep,
) -> dict[str, str]:
    """Simulates the patient paying on MockPay's checkout page, which makes MockPay send us a
    signed webhook. Not mounted in production."""
    payment = await session.scalar(
        select(Payment).where(Payment.id == payment_id, Payment.patient_id == patient.user.id)
    )
    if payment is None or payment.provider_payment_id is None:
        raise NotFound("Payment not found")
    raw, signature = mock_gateway_event(
        settings, payment.provider_payment_id, payment.amount_paise, body.outcome
    )
    return {"status": await service.handle_webhook(raw, signature, ctx)}
