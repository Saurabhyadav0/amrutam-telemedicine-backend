import uuid
from datetime import datetime, timedelta

from prometheus_client import Counter
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import Cache
from app.core.config import Settings
from app.core.crypto import FieldCipher
from app.core.db import utcnow
from app.core.deps import RequestContext
from app.core.errors import Conflict, Forbidden, NotFound
from app.core.security import TokenService
from app.modules.audit.service import AuditService
from app.modules.availability.models import AvailabilitySlot, SlotStatus
from app.modules.consultations.models import Consultation, ConsultationStatus
from app.modules.consultations.saga import (
    cancel_booking,
    close_unpaid,
    confirm_booking,
    request_refund,
    transition,
)
from app.modules.consultations.schemas import (
    BookingIn,
    BookingOut,
    CancelOut,
    ConsultationOut,
    ConsultationPage,
    JoinInfo,
)
from app.modules.doctors.models import Doctor
from app.modules.doctors.repository import bookable
from app.modules.doctors.service import CACHE_NAMESPACE as DOCTORS_CACHE
from app.modules.payments.models import Payment, PaymentStatus
from app.modules.payments.schemas import PaymentOut
from app.modules.payments.service import PaymentService
from app.modules.users.models import Consent, User, UserRole

BOOKINGS = Counter("bookings_total", "Booking requests by outcome", ["outcome"])
CANCELLATIONS = Counter("cancellations_total", "Consultation cancellations by actor", ["actor"])

TELECONSULT_CONSENT = "teleconsultation"
TELECONSULT_CONSENT_VERSION = "tpg-2020-v1"
ROOM_TOKEN_TTL = 2 * 60 * 60
STAFF_ROLES = frozenset({UserRole.ADMIN, UserRole.SUPPORT})


def complaint_aad(consultation_id: uuid.UUID) -> str:
    return f"consultations.chief_complaint:{consultation_id}"


def notes_aad(consultation_id: uuid.UUID) -> str:
    return f"consultations.doctor_notes:{consultation_id}"


class ConsultationService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        cipher: FieldCipher,
        tokens: TokenService,
        payments: PaymentService,
        cache: Cache,
    ) -> None:
        self._session = session
        self._settings = settings
        self._cipher = cipher
        self._tokens = tokens
        self._payments = payments
        self._cache = cache
        self._audit = AuditService(session)

    # ---- booking (saga step 1 + 2) --------------------------------------------------------

    async def book(self, patient: User, req: BookingIn, ctx: RequestContext) -> BookingOut:
        resumed = await self._resume(patient.id, req.slot_id)
        if resumed:  # client retry after a gateway hiccup: continue, don't double-book
            consultation, payment = resumed
            BOOKINGS.labels("resumed").inc()
        else:
            try:
                consultation, payment = await self._create_booking(patient, req, ctx)
            except Conflict as exc:
                BOOKINGS.labels(exc.code).inc()
                raise
            BOOKINGS.labels("held" if payment else "confirmed_free").inc()
        if payment is not None:
            payment = await self._payments.ensure_intent(payment)
        return await self._booking_out(consultation, payment)

    async def _create_booking(
        self, patient: User, req: BookingIn, ctx: RequestContext
    ) -> tuple[Consultation, Payment | None]:
        now = utcnow()
        hold_until = now + timedelta(seconds=self._settings.slot_hold_seconds)
        # Atomic compare-and-set: succeeds for exactly one concurrent request per slot.
        held = (
            await self._session.execute(
                update(AvailabilitySlot)
                .where(
                    AvailabilitySlot.id == req.slot_id,
                    AvailabilitySlot.status == SlotStatus.OPEN,
                    AvailabilitySlot.start_at
                    > now + timedelta(minutes=self._settings.booking_min_lead_minutes),
                    AvailabilitySlot.doctor_id.in_(select(Doctor.user_id).where(bookable())),
                )
                .values(
                    status=SlotStatus.HELD,
                    held_by=patient.id,
                    hold_expires_at=hold_until,
                    version=AvailabilitySlot.version + 1,
                    updated_at=now,
                )
                .returning(
                    AvailabilitySlot.doctor_id,
                    AvailabilitySlot.start_at,
                    AvailabilitySlot.end_at,
                    AvailabilitySlot.mode,
                )
                .execution_options(synchronize_session=False)
            )
        ).first()
        if held is None:
            raise Conflict("This slot is no longer available", code="slot_unavailable")
        doctor = await self._session.get(Doctor, held.doctor_id)
        if doctor is None:  # pragma: no cover - guaranteed by FK / earlier check
            raise RuntimeError("invariant violated: doctor missing")
        if doctor.user_id == patient.id:
            raise Conflict("Doctors cannot book themselves", code="self_booking")

        await self._ensure_consent(patient, ctx)
        consultation = Consultation(
            id=uuid.uuid4(),
            patient_id=patient.id,
            doctor_id=doctor.user_id,
            slot_id=req.slot_id,
            mode=held.mode,
            scheduled_start=held.start_at,
            scheduled_end=held.end_at,
            fee_paise=doctor.fee_paise,
        )
        if req.chief_complaint:
            consultation.chief_complaint_enc = self._cipher.encrypt(
                req.chief_complaint, aad=complaint_aad(consultation.id)
            )
        self._session.add(consultation)
        await self._flush_booking()

        payment: Payment | None = None
        if doctor.fee_paise == 0:
            await confirm_booking(self._session, consultation)
        else:
            payment = Payment(
                id=uuid.uuid4(),
                consultation_id=consultation.id,
                patient_id=patient.id,
                amount_paise=doctor.fee_paise,
                provider=self._payments.provider,
            )
            self._session.add(payment)
        await self._audit.record(
            "consultation.booked",
            ctx=ctx,
            actor_id=patient.id,
            actor_role=patient.role.value,
            resource_type="consultation",
            resource_id=consultation.id,
            metadata={"slot_id": str(req.slot_id), "doctor_id": str(doctor.user_id)},
        )
        await self._session.commit()
        return consultation, payment

    async def _flush_booking(self) -> None:
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()  # also releases the hold taken above
            if "ex_consultations_patient_overlap" in str(exc.orig):
                raise Conflict(
                    "You already have a consultation at this time", code="patient_time_conflict"
                ) from exc
            raise Conflict("This slot is no longer available", code="slot_unavailable") from exc

    async def _resume(
        self, patient_id: uuid.UUID, slot_id: uuid.UUID
    ) -> tuple[Consultation, Payment | None] | None:
        row = (
            await self._session.execute(
                select(Consultation, Payment)
                .outerjoin(Payment, Payment.consultation_id == Consultation.id)
                .where(
                    Consultation.patient_id == patient_id,
                    Consultation.slot_id == slot_id,
                    Consultation.status.in_(
                        [ConsultationStatus.PENDING_PAYMENT, ConsultationStatus.CONFIRMED]
                    ),
                )
            )
        ).first()
        return (row[0], row[1]) if row else None

    async def _ensure_consent(self, patient: User, ctx: RequestContext) -> None:
        existing = await self._session.scalar(
            select(Consent.id).where(
                Consent.user_id == patient.id,
                Consent.purpose == TELECONSULT_CONSENT,
                Consent.version == TELECONSULT_CONSENT_VERSION,
                Consent.granted.is_(True),
            )
        )
        if existing is None:
            self._session.add(
                Consent(
                    user_id=patient.id,
                    purpose=TELECONSULT_CONSENT,
                    version=TELECONSULT_CONSENT_VERSION,
                    granted=True,
                    ip=ctx.ip,
                    user_agent=ctx.user_agent,
                )
            )

    # ---- reads ------------------------------------------------------------------------------

    async def get(
        self, actor: User, consultation_id: uuid.UUID, ctx: RequestContext
    ) -> ConsultationOut:
        consultation = await self._load(actor, consultation_id)
        if actor.role in STAFF_ROLES:
            # Staff reading someone else's health data is always traceable.
            await self._audit.record(
                "consultation.viewed_by_staff",
                ctx=ctx,
                actor_id=actor.id,
                actor_role=actor.role.value,
                resource_type="consultation",
                resource_id=consultation.id,
            )
            await self._session.commit()
        return await self._out(consultation)

    async def list(
        self,
        actor: User,
        *,
        status: ConsultationStatus | None,
        before: datetime | None,
        limit: int,
    ) -> ConsultationPage:
        stmt = (
            select(Consultation)
            .order_by(Consultation.scheduled_start.desc(), Consultation.id)
            .limit(limit + 1)
        )
        if actor.role == UserRole.DOCTOR:
            stmt = stmt.where(Consultation.doctor_id == actor.id)
        else:
            stmt = stmt.where(Consultation.patient_id == actor.id)
        if status:
            stmt = stmt.where(Consultation.status == status)
        if before:
            stmt = stmt.where(Consultation.scheduled_start < before)
        rows = list((await self._session.scalars(stmt)).all())
        has_more = len(rows) > limit
        rows = rows[:limit]
        names = await self._doctor_names({c.doctor_id for c in rows})
        return ConsultationPage(
            items=[self._to_out(c, names.get(c.doctor_id, "")) for c in rows],
            next_cursor=rows[-1].scheduled_start if has_more else None,
        )

    # ---- lifecycle --------------------------------------------------------------------------

    async def cancel(
        self, actor: User, consultation_id: uuid.UUID, reason: str, ctx: RequestContext
    ) -> CancelOut:
        consultation = await self._load(actor, consultation_id, for_update=True)
        if actor.role == UserRole.SUPPORT:
            raise Forbidden("Support staff cannot cancel consultations", code="insufficient_role")
        now = utcnow()
        payment = await self._session.scalar(
            select(Payment).where(Payment.consultation_id == consultation.id).with_for_update()
        )
        refund = self._refund_amount(actor, consultation, payment, now)
        await cancel_booking(
            self._session, consultation, reason=reason, cancelled_by=actor.id, now=now
        )
        if payment is not None and refund:
            request_refund(self._session, payment, refund)
        close_unpaid(payment, PaymentStatus.CANCELLED, "consultation_cancelled")
        await self._audit.record(
            "consultation.cancelled",
            ctx=ctx,
            actor_id=actor.id,
            actor_role=actor.role.value,
            resource_type="consultation",
            resource_id=consultation.id,
            metadata={"reason": reason, "refund_paise": refund},
        )
        await self._session.commit()
        CANCELLATIONS.labels(actor.role.value).inc()
        return CancelOut(consultation=await self._out(consultation), refund_paise=refund)

    def _refund_amount(
        self, actor: User, consultation: Consultation, payment: Payment | None, now: datetime
    ) -> int:
        if payment is None or payment.status != PaymentStatus.SUCCEEDED:
            return 0
        if actor.role != UserRole.PATIENT:
            return payment.amount_paise  # doctor/admin cancellations are always refunded
        cutoff = timedelta(hours=self._settings.patient_refund_cutoff_hours)
        return payment.amount_paise if consultation.scheduled_start - now >= cutoff else 0

    async def start(self, actor: User, consultation_id: uuid.UUID, ctx: RequestContext) -> JoinInfo:
        consultation = await self._load(actor, consultation_id, for_update=True)
        self._require_doctor(actor, consultation)
        now = utcnow()
        window = timedelta(minutes=self._settings.join_window_minutes)
        if not (consultation.scheduled_start - window <= now <= consultation.scheduled_end):
            raise Conflict("Consultation can only start in its time window", code="outside_window")
        transition(consultation, ConsultationStatus.IN_PROGRESS)
        consultation.started_at = now
        await self._audit.record(
            "consultation.started",
            ctx=ctx,
            actor_id=actor.id,
            actor_role=actor.role.value,
            resource_type="consultation",
            resource_id=consultation.id,
        )
        await self._session.commit()
        return self._join_info(consultation, actor)

    async def join(self, actor: User, consultation_id: uuid.UUID) -> JoinInfo:
        consultation = await self._load(actor, consultation_id)
        if actor.id not in (consultation.patient_id, consultation.doctor_id):
            raise Forbidden("Only participants can join", code="not_participant")
        if consultation.status != ConsultationStatus.IN_PROGRESS:
            raise Conflict("The doctor has not started this consultation yet", code="not_started")
        return self._join_info(consultation, actor)

    async def complete(
        self, actor: User, consultation_id: uuid.UUID, notes: str | None, ctx: RequestContext
    ) -> ConsultationOut:
        consultation = await self._load(actor, consultation_id, for_update=True)
        self._require_doctor(actor, consultation)
        transition(consultation, ConsultationStatus.COMPLETED)
        consultation.completed_at = utcnow()
        if notes:
            consultation.doctor_notes_enc = self._cipher.encrypt(
                notes, aad=notes_aad(consultation.id)
            )
        await self._audit.record(
            "consultation.completed",
            ctx=ctx,
            actor_id=actor.id,
            actor_role=actor.role.value,
            resource_type="consultation",
            resource_id=consultation.id,
        )
        await self._session.commit()
        return await self._out(consultation)

    async def mark_no_show(
        self, actor: User, consultation_id: uuid.UUID, ctx: RequestContext
    ) -> ConsultationOut:
        consultation = await self._load(actor, consultation_id, for_update=True)
        self._require_doctor(actor, consultation)
        grace = timedelta(minutes=self._settings.join_window_minutes)
        if utcnow() < consultation.scheduled_start + grace:
            raise Conflict(
                "Wait 15 minutes past the start before marking a no-show", code="too_early"
            )
        transition(consultation, ConsultationStatus.NO_SHOW)
        await self._audit.record(
            "consultation.no_show",
            ctx=ctx,
            actor_id=actor.id,
            actor_role=actor.role.value,
            resource_type="consultation",
            resource_id=consultation.id,
        )
        await self._session.commit()
        return await self._out(consultation)

    async def review(
        self,
        actor: User,
        consultation_id: uuid.UUID,
        rating: int,
        text: str | None,
        ctx: RequestContext,
    ) -> ConsultationOut:
        consultation = await self._load(actor, consultation_id, for_update=True)
        if actor.id != consultation.patient_id:
            raise Forbidden("Only the patient can review", code="not_participant")
        if consultation.status != ConsultationStatus.COMPLETED:
            raise Conflict("Only completed consultations can be reviewed", code="not_completed")
        if consultation.rating is not None:
            raise Conflict("Already reviewed", code="already_reviewed")
        consultation.rating = rating
        consultation.review = text
        consultation.rated_at = utcnow()
        # Incremental average in one atomic UPDATE: concurrent reviews can't lose a vote.
        await self._session.execute(
            update(Doctor)
            .where(Doctor.user_id == consultation.doctor_id)
            .values(
                rating_avg=(Doctor.rating_avg * Doctor.rating_count + rating)
                / (Doctor.rating_count + 1),
                rating_count=Doctor.rating_count + 1,
            )
            .execution_options(synchronize_session=False)
        )
        await self._audit.record(
            "consultation.reviewed",
            ctx=ctx,
            actor_id=actor.id,
            actor_role=actor.role.value,
            resource_type="consultation",
            resource_id=consultation.id,
            metadata={"rating": rating},
        )
        await self._session.commit()
        await self._cache.invalidate(DOCTORS_CACHE)
        return await self._out(consultation)

    # ---- helpers ----------------------------------------------------------------------------

    async def _load(
        self, actor: User, consultation_id: uuid.UUID, *, for_update: bool = False
    ) -> Consultation:
        stmt = select(Consultation).where(Consultation.id == consultation_id)
        if for_update:
            stmt = stmt.with_for_update()
        consultation = await self._session.scalar(stmt)
        allowed = consultation is not None and (
            actor.role in STAFF_ROLES
            or actor.id in (consultation.patient_id, consultation.doctor_id)
        )
        if not allowed:
            # 404 rather than 403: don't confirm that someone else's consultation exists (BOLA).
            raise NotFound("Consultation not found")
        if consultation is None:  # pragma: no cover - guaranteed by FK / earlier check
            raise RuntimeError("invariant violated: consultation missing")
        return consultation

    @staticmethod
    def _require_doctor(actor: User, consultation: Consultation) -> None:
        if actor.id != consultation.doctor_id:
            raise Forbidden("Only the consulting doctor can do this", code="not_consulting_doctor")

    def _join_info(self, consultation: Consultation, actor: User) -> JoinInfo:
        return JoinInfo(
            consultation_id=consultation.id,
            room=f"consult-{consultation.id}",
            token=self._tokens.create_room_token(
                consultation.id, actor.id, actor.role.value, ROOM_TOKEN_TTL
            ),
            mode=consultation.mode,
            expires_in=ROOM_TOKEN_TTL,
        )

    async def _doctor_names(self, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not ids:
            return {}
        rows = await self._session.execute(
            select(Doctor.user_id, Doctor.display_name).where(Doctor.user_id.in_(ids))
        )
        return {r.user_id: r.display_name for r in rows}

    async def _out(self, consultation: Consultation) -> ConsultationOut:
        names = await self._doctor_names({consultation.doctor_id})
        return self._to_out(consultation, names.get(consultation.doctor_id, ""))

    def _to_out(self, c: Consultation, doctor_name: str) -> ConsultationOut:
        complaint = (
            self._cipher.decrypt(c.chief_complaint_enc, aad=complaint_aad(c.id))
            if c.chief_complaint_enc
            else None
        )
        return ConsultationOut(
            id=c.id,
            patient_id=c.patient_id,
            doctor_id=c.doctor_id,
            doctor_name=doctor_name,
            slot_id=c.slot_id,
            mode=c.mode,
            status=c.status,
            scheduled_start=c.scheduled_start,
            scheduled_end=c.scheduled_end,
            fee_paise=c.fee_paise,
            chief_complaint=complaint,
            cancel_reason=c.cancel_reason,
            rating=c.rating,
            version=c.version,
        )

    async def _booking_out(self, consultation: Consultation, payment: Payment | None) -> BookingOut:
        hold = await self._session.scalar(
            select(AvailabilitySlot.hold_expires_at).where(
                AvailabilitySlot.id == consultation.slot_id
            )
        )
        return BookingOut(
            consultation=await self._out(consultation),
            payment=PaymentOut.model_validate(payment) if payment else None,
            checkout_url=(
                f"https://mockpay.test/checkout/{payment.provider_payment_id}"
                if payment and payment.provider_payment_id
                else None
            ),
            hold_expires_at=hold,
        )
