import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import Cache
from app.core.db import utcnow
from app.core.deps import RequestContext
from app.core.errors import BadRequest, Conflict, Forbidden, NotFound
from app.modules.audit.service import AuditService
from app.modules.availability.models import AvailabilitySlot, SlotStatus
from app.modules.availability.schemas import (
    BOOKING_HORIZON_DAYS,
    MAX_SLOTS_PER_REQUEST,
    PublicSlotOut,
    RecurrenceRule,
    SlotBatchIn,
    SlotBatchOut,
    SlotOut,
)
from app.modules.doctors.models import Doctor, VerificationStatus
from app.modules.doctors.repository import DoctorRepository, bookable, refresh_next_available
from app.modules.doctors.service import CACHE_NAMESPACE
from app.modules.users.models import User

MIN_SLOT = timedelta(minutes=10)
MAX_SLOT = timedelta(hours=2)
MAX_QUERY_WINDOW = timedelta(days=31)

Window = tuple[datetime, datetime]


def expand_rule(rule: RecurrenceRule) -> Iterator[Window]:
    """Yield UTC (start, end) windows for a weekly rule defined in the doctor's local time.

    Wall-clock arithmetic happens in the local zone, so 10:00 stays 10:00 across DST changes.
    """
    tz = ZoneInfo(rule.timezone)
    step = timedelta(minutes=rule.slot_minutes)
    weekdays = set(rule.weekdays)
    day = rule.start_date
    while day <= rule.end_date:
        if day.weekday() in weekdays:
            cursor = datetime.combine(day, rule.day_start, tzinfo=tz)
            day_end = datetime.combine(day, rule.day_end, tzinfo=tz)
            while cursor + step <= day_end:
                yield cursor.astimezone(UTC), (cursor + step).astimezone(UTC)
                cursor += step
        day += timedelta(days=1)


def validate_windows(windows: list[Window], now: datetime) -> list[Window]:
    horizon = now + timedelta(days=BOOKING_HORIZON_DAYS)
    future = [(s, e) for s, e in windows if s > now]
    if not future:
        raise BadRequest("All requested slots are in the past", code="no_future_slots")
    if len(future) > MAX_SLOTS_PER_REQUEST:
        raise BadRequest(
            f"At most {MAX_SLOTS_PER_REQUEST} slots per request", code="too_many_slots"
        )
    for start, end in future:
        if not MIN_SLOT <= end - start <= MAX_SLOT:
            raise BadRequest("Slots must be 10 minutes to 2 hours long", code="invalid_slot_length")
        if start > horizon:
            raise BadRequest(
                f"Slots can be published at most {BOOKING_HORIZON_DAYS} days ahead",
                code="beyond_booking_horizon",
            )
    return sorted(future)


def validate_query_window(start: datetime, end: datetime) -> None:
    if end <= start:
        raise BadRequest("'to' must be after 'from'", code="invalid_window")
    if end - start > MAX_QUERY_WINDOW:
        raise BadRequest("Query window may span at most 31 days", code="window_too_large")


class AvailabilityService:
    def __init__(self, session: AsyncSession, cache: Cache) -> None:
        self._session = session
        self._cache = cache
        self._doctors = DoctorRepository(session)
        self._audit = AuditService(session)

    async def publish(self, user: User, batch: SlotBatchIn, ctx: RequestContext) -> SlotBatchOut:
        doctor = await self._require_verified(user)
        windows = (
            list(expand_rule(batch.rule))
            if batch.rule
            else [(s.start_at.astimezone(UTC), s.end_at.astimezone(UTC)) for s in batch.slots or []]
        )
        windows = validate_windows(windows, utcnow())

        now = utcnow()
        rows = [
            {
                "id": uuid.uuid4(),
                "doctor_id": doctor.user_id,
                "start_at": start,
                "end_at": end,
                "mode": batch.mode.value,
                "status": SlotStatus.OPEN.value,
                "version": 1,
                "created_at": now,
                "updated_at": now,
            }
            for start, end in windows
        ]
        # No conflict target: overlaps rejected by the exclusion constraint are skipped,
        # including overlaps between rows of this same batch. Re-publishing a week is a no-op.
        stmt = (
            insert(AvailabilitySlot)
            .values(rows)
            .on_conflict_do_nothing()
            .returning(AvailabilitySlot)
        )
        created = list((await self._session.scalars(stmt)).all())

        await refresh_next_available(self._session, now, doctor.user_id)
        await self._audit.record(
            "availability.published",
            ctx=ctx,
            actor_id=user.id,
            actor_role=user.role.value,
            resource_type="doctor",
            resource_id=user.id,
            metadata={"requested": len(rows), "created": len(created), "mode": batch.mode.value},
        )
        await self._session.commit()
        await self._cache.invalidate(CACHE_NAMESPACE)
        created.sort(key=lambda s: s.start_at)
        return SlotBatchOut(
            created=len(created),
            skipped_overlapping=len(rows) - len(created),
            slots=[SlotOut.model_validate(s) for s in created],
        )

    async def list_own(
        self, user: User, start: datetime, end: datetime, status: SlotStatus | None
    ) -> list[SlotOut]:
        validate_query_window(start, end)
        stmt = (
            select(AvailabilitySlot)
            .where(
                AvailabilitySlot.doctor_id == user.id,
                AvailabilitySlot.start_at >= start,
                AvailabilitySlot.start_at < end,
            )
            .order_by(AvailabilitySlot.start_at)
        )
        if status:
            stmt = stmt.where(AvailabilitySlot.status == status)
        return [SlotOut.model_validate(s) for s in (await self._session.scalars(stmt)).all()]

    async def remove(self, user: User, slot_id: uuid.UUID, ctx: RequestContext) -> None:
        # Conditional delete: only an OPEN slot owned by this doctor. Held/booked slots belong
        # to a patient's booking and must go through cancellation instead.
        result = await self._session.execute(
            delete(AvailabilitySlot)
            .where(
                AvailabilitySlot.id == slot_id,
                AvailabilitySlot.doctor_id == user.id,
                AvailabilitySlot.status == SlotStatus.OPEN,
            )
            .returning(AvailabilitySlot.start_at)
        )
        if result.first() is None:
            exists = await self._session.scalar(
                select(AvailabilitySlot.status).where(
                    AvailabilitySlot.id == slot_id, AvailabilitySlot.doctor_id == user.id
                )
            )
            if exists is None:
                raise NotFound("Slot not found")
            raise Conflict(
                "Slot is held or booked; cancel the consultation instead", code="slot_not_open"
            )
        await refresh_next_available(self._session, utcnow(), user.id)
        await self._audit.record(
            "availability.slot_removed",
            ctx=ctx,
            actor_id=user.id,
            actor_role=user.role.value,
            resource_type="slot",
            resource_id=slot_id,
        )
        await self._session.commit()
        await self._cache.invalidate(CACHE_NAMESPACE)

    async def list_public(
        self, doctor_id: uuid.UUID, start: datetime, end: datetime
    ) -> list[PublicSlotOut]:
        validate_query_window(start, end)
        is_bookable = await self._session.scalar(
            select(Doctor.user_id).where(Doctor.user_id == doctor_id, bookable())
        )
        if is_bookable is None:
            raise NotFound("Doctor not found")
        stmt = (
            select(AvailabilitySlot)
            .where(
                AvailabilitySlot.doctor_id == doctor_id,
                AvailabilitySlot.status == SlotStatus.OPEN,
                AvailabilitySlot.start_at >= max(start, utcnow()),
                AvailabilitySlot.start_at < end,
            )
            .order_by(AvailabilitySlot.start_at)
        )
        return [PublicSlotOut.model_validate(s) for s in (await self._session.scalars(stmt)).all()]

    async def _require_verified(self, user: User) -> Doctor:
        doctor = await self._doctors.get(user.id)
        if doctor is None:
            raise NotFound("Submit your practice profile first", code="doctor_profile_missing")
        if doctor.verification_status != VerificationStatus.VERIFIED:
            raise Forbidden(
                "Your registration must be verified before publishing availability",
                code="doctor_not_verified",
            )
        return doctor
