import json
import uuid

import structlog
from prometheus_client import Counter
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import utcnow
from app.core.deps import RequestContext
from app.core.errors import BadRequest, Unauthorized
from app.core.resilience import TransientError
from app.modules.audit.service import AuditService
from app.modules.consultations.models import Consultation, ConsultationStatus
from app.modules.consultations.saga import (
    cancel_booking,
    confirm_booking,
    hold_is_valid,
    lock_slot,
    request_refund,
)
from app.modules.payments.gateway import (
    InvalidSignature,
    PaymentGateway,
    sign_webhook,
    verify_webhook,
)
from app.modules.payments.models import Payment, PaymentEvent, PaymentStatus
from app.modules.payments.schemas import WebhookEvent

logger = structlog.get_logger(__name__)

_SETTLED = frozenset(
    {PaymentStatus.SUCCEEDED, PaymentStatus.REFUND_PENDING, PaymentStatus.REFUNDED}
)
WEBHOOKS = Counter(
    "payment_webhooks_total", "Payment webhooks by type and outcome", ["type", "outcome"]
)


class PaymentService:
    def __init__(
        self, session: AsyncSession, *, settings: Settings, gateway: PaymentGateway
    ) -> None:
        self._session = session
        self._settings = settings
        self._gateway = gateway
        self._audit = AuditService(session)

    @property
    def provider(self) -> str:
        return self._gateway.provider

    async def ensure_intent(self, payment: Payment) -> Payment:
        """Saga step 2. Safe to call repeatedly: the gateway dedupes on our payment id."""
        if payment.provider_payment_id or payment.status != PaymentStatus.CREATED:
            return payment
        intent = await self._gateway.create_intent(
            idempotency_key=payment.id, amount_paise=payment.amount_paise, currency=payment.currency
        )
        payment.provider_payment_id = intent.provider_payment_id
        await self._session.commit()
        return payment

    # ---- webhooks -------------------------------------------------------------------------

    async def handle_webhook(self, body: bytes, signature: str | None, ctx: RequestContext) -> str:
        try:
            raw = verify_webhook(
                self._settings.payment_webhook_secret,
                body,
                signature,
                tolerance_s=self._settings.payment_webhook_tolerance_seconds,
            )
        except InvalidSignature as exc:
            WEBHOOKS.labels("unknown", "bad_signature").inc()
            logger.warning("webhook_rejected", reason=str(exc))
            raise Unauthorized("Invalid webhook signature", code="invalid_signature") from exc
        try:
            event = WebhookEvent.model_validate(raw)
        except ValueError as exc:
            raise BadRequest("Malformed webhook event", code="malformed_event") from exc
        outcome = await self.apply_event(event, raw, ctx)
        WEBHOOKS.labels(event.type, outcome).inc()
        return outcome

    async def apply_event(self, event: WebhookEvent, raw: dict, ctx: RequestContext | None) -> str:
        inbox = (
            insert(PaymentEvent)
            .values(
                provider=self._gateway.provider,
                event_id=event.id,
                event_type=event.type,
                payload=raw,
            )
            .on_conflict_do_nothing()
            .returning(PaymentEvent.event_id)
        )
        if (await self._session.execute(inbox)).first() is None:
            return "duplicate"  # provider redelivery: already applied

        payment = await self._session.scalar(
            select(Payment)
            .where(Payment.provider_payment_id == event.data.provider_payment_id)
            .with_for_update()
        )
        if payment is None:
            await self._session.commit()  # keep the inbox row for investigation
            logger.warning("webhook_unknown_payment")
            return "unknown_payment"
        consultation = await self._session.scalar(
            select(Consultation).where(Consultation.id == payment.consultation_id).with_for_update()
        )
        if consultation is None:  # pragma: no cover - guaranteed by FK / earlier check
            raise RuntimeError("invariant violated: consultation missing")

        if event.type == "payment.succeeded":
            outcome = await self._on_succeeded(payment, consultation, event)
        else:
            outcome = await self._on_failed(payment, consultation, event)
        await self._audit.record(
            f"payment.{outcome}",
            ctx=ctx,
            resource_type="payment",
            resource_id=payment.id,
            metadata={"event_id": event.id, "consultation_id": str(consultation.id)},
        )
        await self._session.commit()
        return outcome

    async def _on_succeeded(
        self, payment: Payment, consultation: Consultation, event: WebhookEvent
    ) -> str:
        if payment.status in _SETTLED:
            return "ignored"
        # Money captured after we gave up on this payment (expired/cancelled/failed) must go back.
        gave_up = payment.status != PaymentStatus.CREATED
        payment.status = PaymentStatus.SUCCEEDED
        reason = None
        if event.data.amount_paise != payment.amount_paise:
            reason = "amount_mismatch"
        elif gave_up:
            reason = "late_payment"
        else:
            slot = await lock_slot(self._session, consultation.slot_id)
            if consultation.status == ConsultationStatus.PENDING_PAYMENT and hold_is_valid(
                slot, consultation
            ):
                await confirm_booking(self._session, consultation)
                return "confirmed"
            reason = "hold_expired"  # the slot may already belong to someone else
        payment.failure_reason = reason
        request_refund(self._session, payment, event.data.amount_paise)
        if consultation.status == ConsultationStatus.PENDING_PAYMENT:
            await cancel_booking(
                self._session, consultation, reason=reason, cancelled_by=None, now=utcnow()
            )
        return "compensated"

    async def _on_failed(
        self, payment: Payment, consultation: Consultation, event: WebhookEvent
    ) -> str:
        if payment.status != PaymentStatus.CREATED:
            return "ignored"
        payment.status = PaymentStatus.FAILED
        payment.failure_reason = event.data.failure_reason or "declined"
        if consultation.status == ConsultationStatus.PENDING_PAYMENT:
            await cancel_booking(
                self._session,
                consultation,
                reason="payment_failed",
                cancelled_by=None,
                now=utcnow(),
            )
        return "failed"

    # ---- refunds (worker) -----------------------------------------------------------------

    async def process_refund(self, payment_id: uuid.UUID) -> str:
        payment = await self._session.scalar(
            select(Payment).where(Payment.id == payment_id).with_for_update()
        )
        if payment is None or payment.status != PaymentStatus.REFUND_PENDING:
            return "skipped"  # already refunded: at-least-once delivery is harmless
        if not payment.provider_payment_id:
            raise TransientError("payment has no provider id yet")
        payment.provider_refund_id = await self._gateway.refund(
            idempotency_key=payment.id,
            provider_payment_id=payment.provider_payment_id,
            amount_paise=payment.refund_amount_paise or payment.amount_paise,
        )
        payment.status = PaymentStatus.REFUNDED
        await self._audit.record(
            "payment.refunded",
            ctx=None,
            resource_type="payment",
            resource_id=payment.id,
            metadata={"amount_paise": payment.refund_amount_paise},
        )
        await self._session.commit()
        return "refunded"


def mock_gateway_event(
    settings: Settings, provider_payment_id: str, amount_paise: int, outcome: str
) -> tuple[bytes, str]:
    """What MockPay would POST to our webhook. Dev/test only."""
    body = json.dumps(
        {
            "id": f"evt_{uuid.uuid4().hex}",
            "type": f"payment.{outcome}",
            "data": {
                "provider_payment_id": provider_payment_id,
                "amount_paise": amount_paise,
                "failure_reason": None if outcome == "succeeded" else "card_declined",
            },
        }
    ).encode()
    return body, sign_webhook(settings.payment_webhook_secret, body)
