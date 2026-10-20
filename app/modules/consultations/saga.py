"""Booking saga steps and compensations, shared by every entry point.

    book ──► HOLD slot + consultation(PENDING_PAYMENT) + payment(CREATED)        [1 local tx]
         ──► gateway.create_intent (retry + breaker, idempotent)                [external]
    webhook payment.succeeded ──► CONFIRM: consultation CONFIRMED, slot BOOKED  [1 local tx]
                        └─ hold already gone ──► compensate: cancel + REFUND    [1 local tx]
    webhook payment.failed / hold expiry ──► cancel consultation, reopen slot  [1 local tx]
    refund requested ──► worker: gateway.refund (retry/backoff/DLQ)            [async]

Each step is a single Postgres transaction that also writes its outbox event, so no step is
half-applied and no notification is lost. Callers hold row locks (FOR UPDATE) on the
consultation, slot and payment they pass in.
"""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict
from app.modules.availability.models import AvailabilitySlot, SlotStatus
from app.modules.consultations.models import TRANSITIONS, Consultation, ConsultationStatus
from app.modules.outbox.service import emit
from app.modules.payments.models import Payment, PaymentStatus


def transition(consultation: Consultation, target: ConsultationStatus) -> None:
    if target not in TRANSITIONS[consultation.status]:
        raise Conflict(
            f"Cannot move consultation from {consultation.status.value} to {target.value}",
            code="invalid_transition",
        )
    consultation.status = target
    consultation.version += 1


async def lock_slot(session: AsyncSession, slot_id: uuid.UUID) -> AvailabilitySlot:
    slot = await session.scalar(
        select(AvailabilitySlot).where(AvailabilitySlot.id == slot_id).with_for_update()
    )
    if slot is None:  # pragma: no cover - FK guarantees existence
        raise Conflict("Slot disappeared", code="slot_missing")
    return slot


def _clear_hold(slot: AvailabilitySlot, status: SlotStatus) -> None:
    slot.status = status
    slot.held_by = None
    slot.hold_expires_at = None
    slot.version += 1


async def confirm_booking(session: AsyncSession, consultation: Consultation) -> None:
    slot = await lock_slot(session, consultation.slot_id)
    transition(consultation, ConsultationStatus.CONFIRMED)
    _clear_hold(slot, SlotStatus.BOOKED)
    emit(
        session,
        "consultation.confirmed",
        consultation.id,
        {
            "consultation_id": str(consultation.id),
            "patient_id": str(consultation.patient_id),
            "doctor_id": str(consultation.doctor_id),
        },
    )


def hold_is_valid(slot: AvailabilitySlot, consultation: Consultation) -> bool:
    return slot.status == SlotStatus.HELD and slot.held_by == consultation.patient_id


async def cancel_booking(
    session: AsyncSession,
    consultation: Consultation,
    *,
    reason: str,
    cancelled_by: uuid.UUID | None,
    now: datetime,
) -> None:
    transition(consultation, ConsultationStatus.CANCELLED)
    consultation.cancel_reason = reason
    consultation.cancelled_by = cancelled_by
    consultation.cancelled_at = now
    slot = await lock_slot(session, consultation.slot_id)
    # Give the time back to other patients, but only if the slot is still "ours" (a hold that
    # already expired may have been re-booked by someone else). Past open slots are inert.
    if slot.status == SlotStatus.BOOKED or hold_is_valid(slot, consultation):
        _clear_hold(slot, SlotStatus.OPEN)
    emit(
        session,
        "consultation.cancelled",
        consultation.id,
        {
            "consultation_id": str(consultation.id),
            "patient_id": str(consultation.patient_id),
            "doctor_id": str(consultation.doctor_id),
            "reason": reason,
        },
    )


def request_refund(session: AsyncSession, payment: Payment, amount_paise: int) -> None:
    payment.status = PaymentStatus.REFUND_PENDING
    payment.refund_amount_paise = amount_paise
    emit(session, "payment.refund_requested", payment.id, {"payment_id": str(payment.id)})


def close_unpaid(payment: Payment | None, status: PaymentStatus, reason: str) -> None:
    if payment is not None and payment.status == PaymentStatus.CREATED:
        payment.status = status
        payment.failure_reason = reason
