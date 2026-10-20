"""Background jobs. Plain async functions over explicit dependencies, so they're testable
without a running arq worker; ``app.worker.main`` schedules them."""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, cast

import structlog
from prometheus_client import Counter
from sqlalchemy import CursorResult, delete, select, text, tuple_, update

from app.core.config import Settings
from app.core.db import Database, utcnow
from app.core.resilience import backoff_delay
from app.middleware.idempotency import IdempotencyKey
from app.modules.analytics.service import business_today, refresh_rollups
from app.modules.availability.models import AvailabilitySlot, SlotStatus
from app.modules.consultations.models import Consultation, ConsultationStatus
from app.modules.consultations.saga import cancel_booking, close_unpaid
from app.modules.doctors.repository import refresh_next_available
from app.modules.outbox.models import OutboxEvent, OutboxStatus
from app.modules.payments.gateway import PaymentGateway
from app.modules.payments.models import Payment, PaymentStatus
from app.modules.payments.service import PaymentService
from app.worker.notifications import Notifier

logger = structlog.get_logger(__name__)

OUTBOX = Counter(
    "outbox_events_total", "Outbox deliveries by topic and result", ["topic", "result"]
)
HOLDS_RELEASED = Counter("slot_holds_released_total", "Unpaid holds released by the sweeper")

MAX_ATTEMPTS = 8  # with 1s base / 15min cap, roughly 30 minutes of retries before dead-letter
LEASE = timedelta(seconds=60)


@dataclass
class WorkerDeps:
    db: Database
    settings: Settings
    gateway: PaymentGateway
    notifier: Notifier
    handlers: dict[str, Callable[[OutboxEvent, "WorkerDeps"], Awaitable[None]]] = field(
        default_factory=dict
    )


# ---- outbox ------------------------------------------------------------------------------


async def _notify_participants(event: OutboxEvent, deps: WorkerDeps) -> None:
    template = event.topic.replace(".", "_")
    data = {"consultation_id": event.payload["consultation_id"]}
    await deps.notifier.send(uuid.UUID(event.payload["patient_id"]), template, data)
    await deps.notifier.send(uuid.UUID(event.payload["doctor_id"]), template, data)


async def _notify_prescription(event: OutboxEvent, deps: WorkerDeps) -> None:
    await deps.notifier.send(
        uuid.UUID(event.payload["patient_id"]),
        "prescription_issued",
        {"prescription_id": event.payload["prescription_id"]},
    )


async def _refund(event: OutboxEvent, deps: WorkerDeps) -> None:
    async with deps.db.sessionmaker() as session:
        service = PaymentService(session, settings=deps.settings, gateway=deps.gateway)
        await service.process_refund(uuid.UUID(event.payload["payment_id"]))


async def _refresh_analytics(event: OutboxEvent, deps: WorkerDeps) -> None:
    await refresh_analytics(
        deps,
        from_day=date.fromisoformat(event.payload["from_day"]),
        to_day=date.fromisoformat(event.payload["to_day"]),
    )


DEFAULT_HANDLERS: dict[str, Callable[[OutboxEvent, WorkerDeps], Awaitable[None]]] = {
    "consultation.confirmed": _notify_participants,
    "consultation.cancelled": _notify_participants,
    "prescription.issued": _notify_prescription,
    "payment.refund_requested": _refund,
    "analytics.refresh_requested": _refresh_analytics,
}


async def dispatch_outbox(deps: WorkerDeps, *, batch: int = 50, now: datetime | None = None) -> int:
    """Claim due events with a lease (SKIP LOCKED: many workers never take the same row),
    run their handlers outside any long transaction, then record the result."""
    now = now or utcnow()
    async with deps.db.sessionmaker() as session, session.begin():
        ids = list(
            (
                await session.scalars(
                    select(OutboxEvent.id)
                    .where(
                        OutboxEvent.status == OutboxStatus.PENDING,
                        OutboxEvent.next_attempt_at <= now,
                    )
                    .order_by(OutboxEvent.next_attempt_at, OutboxEvent.id)
                    .limit(batch)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        if ids:
            await session.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id.in_(ids))
                .values(next_attempt_at=now + LEASE)
            )

    handlers = deps.handlers or DEFAULT_HANDLERS
    for event_id in ids:
        async with deps.db.sessionmaker() as session:
            event = await session.get(OutboxEvent, event_id)
            if event is None:  # pragma: no cover - guaranteed by FK / earlier check
                raise RuntimeError("invariant violated: event missing")
            handler = handlers.get(event.topic)
            try:
                if handler is None:
                    raise LookupError(f"no handler for topic {event.topic!r}")
                await handler(event, deps)
            except Exception as exc:
                event.attempts += 1
                event.last_error = f"{type(exc).__name__}: {exc}"[:2000]
                if event.attempts >= MAX_ATTEMPTS or handler is None:
                    event.status = OutboxStatus.DEAD
                    OUTBOX.labels(event.topic, "dead").inc()
                    logger.error(
                        "outbox_dead_letter",
                        topic=event.topic,
                        event_id=event.id,
                        error=event.last_error,
                    )
                else:
                    event.next_attempt_at = utcnow() + timedelta(
                        seconds=1 + backoff_delay(event.attempts, base=1.0, cap=900.0)
                    )
                    OUTBOX.labels(event.topic, "retry").inc()
            else:
                event.status = OutboxStatus.DONE
                event.processed_at = utcnow()
                OUTBOX.labels(event.topic, "done").inc()
            await session.commit()
    return len(ids)


# ---- booking saga timeouts ---------------------------------------------------------------


async def release_expired_holds(
    deps: WorkerDeps, *, batch: int = 200, now: datetime | None = None
) -> int:
    """Compensating step for abandoned checkouts: cancel the pending consultation, expire the
    unpaid payment and reopen the slot. A payment that lands later is refunded automatically."""
    now = now or utcnow()
    async with deps.db.sessionmaker() as session, session.begin():
        slots = list(
            (
                await session.scalars(
                    select(AvailabilitySlot)
                    .where(
                        AvailabilitySlot.status == SlotStatus.HELD,
                        AvailabilitySlot.hold_expires_at < now,
                    )
                    .order_by(AvailabilitySlot.hold_expires_at)
                    .limit(batch)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for slot in slots:
            consultation = await session.scalar(
                select(Consultation)
                .where(
                    Consultation.slot_id == slot.id,
                    Consultation.status == ConsultationStatus.PENDING_PAYMENT,
                )
                .with_for_update()
            )
            if consultation is None:  # orphan hold (should not happen): just free the slot
                slot.status, slot.held_by, slot.hold_expires_at = SlotStatus.OPEN, None, None
                slot.version += 1
                continue
            payment = await session.scalar(
                select(Payment).where(Payment.consultation_id == consultation.id).with_for_update()
            )
            close_unpaid(payment, PaymentStatus.EXPIRED, "hold_expired")
            await cancel_booking(
                session, consultation, reason="payment_timeout", cancelled_by=None, now=now
            )
    HOLDS_RELEASED.inc(len(slots))
    if slots:
        logger.info("holds_released", count=len(slots))
    return len(slots)


# ---- housekeeping ------------------------------------------------------------------------


async def purge_idempotency_keys(deps: WorkerDeps, *, batch: int = 5000) -> int:
    async with deps.db.sessionmaker() as session, session.begin():
        expired = (
            select(IdempotencyKey.owner, IdempotencyKey.key)
            .where(IdempotencyKey.expires_at < utcnow())
            .limit(batch)
        )
        result = await session.execute(
            delete(IdempotencyKey).where(
                tuple_(IdempotencyKey.owner, IdempotencyKey.key).in_(expired)
            )
        )
    return cast(CursorResult[Any], result).rowcount or 0


async def ensure_partitions(deps: WorkerDeps, *, months_ahead: int = 3) -> None:
    async with deps.db.sessionmaker() as session, session.begin():
        await session.execute(
            text("SELECT create_monthly_partitions('audit_logs', :m)"), {"m": months_ahead}
        )


async def refresh_analytics(
    deps: WorkerDeps, *, from_day: date | None = None, to_day: date | None = None
) -> None:
    """Default: today and yesterday (late cancellations/refunds still change yesterday)."""
    to_day = to_day or business_today()
    from_day = from_day or to_day - timedelta(days=1)
    async with deps.db.sessionmaker() as session, session.begin():
        await refresh_rollups(session, from_day, to_day)


async def refresh_doctor_availability(deps: WorkerDeps) -> int:
    """Keeps the search listing's next_available_at within ~1 minute of bookings/holds."""
    async with deps.db.sessionmaker() as session, session.begin():
        return await refresh_next_available(session, utcnow())
