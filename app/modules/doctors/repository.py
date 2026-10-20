import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Select, and_, case, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.availability.models import AvailabilitySlot, SlotStatus
from app.modules.doctors.models import Doctor, VerificationStatus
from app.modules.doctors.schemas import DoctorSearchParams, DoctorSort

_TOKEN = re.compile(r"[\w]+", re.UNICODE)
MAX_QUERY_TOKENS = 6


def prefix_tsquery(text: str) -> str | None:
    """'pri sha' -> 'pri:* & sha:*'. Tokens are word characters only, so the result is a safe
    tsquery literal (no operators from user input reach to_tsquery)."""
    tokens = _TOKEN.findall(text.lower())[:MAX_QUERY_TOKENS]
    return " & ".join(f"{t}:*" for t in tokens) or None


def bookable() -> Any:
    return and_(Doctor.verification_status == VerificationStatus.VERIFIED, Doctor.is_accepting)


class DoctorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: uuid.UUID, *, for_update: bool = False) -> Doctor | None:
        stmt = select(Doctor).where(Doctor.user_id == user_id)
        if for_update:
            stmt = stmt.with_for_update()
        return await self._session.scalar(stmt)

    async def registration_taken(
        self, council: str, number: str, *, exclude_user: uuid.UUID
    ) -> bool:
        stmt = select(Doctor.user_id).where(
            Doctor.registration_council == council,
            Doctor.registration_number == number,
            Doctor.user_id != exclude_user,
        )
        return (await self._session.scalar(stmt)) is not None

    async def list_by_status(
        self, status: VerificationStatus | None, *, limit: int, offset: int
    ) -> list[Doctor]:
        stmt = (
            select(Doctor).order_by(Doctor.created_at, Doctor.user_id).limit(limit).offset(offset)
        )
        if status:
            stmt = stmt.where(Doctor.verification_status == status)
        return list((await self._session.scalars(stmt)).all())

    async def get_bookable_with_next_slot(
        self, user_id: uuid.UUID, now: datetime
    ) -> tuple[Doctor, datetime | None] | None:
        stmt = select(Doctor, self._next_slot(now)).where(Doctor.user_id == user_id, bookable())
        row = (await self._session.execute(stmt)).first()
        return (row[0], row[1]) if row else None

    async def search(
        self, params: DoctorSearchParams, now: datetime
    ) -> list[tuple[Doctor, datetime | None]]:
        stmt = self._search_stmt(params, now).limit(params.limit + 1).offset(params.offset)
        return [(row[0], row[1]) for row in (await self._session.execute(stmt)).all()]

    @staticmethod
    def _next_slot(now: datetime) -> Any:
        return (
            select(func.min(AvailabilitySlot.start_at))
            .where(
                AvailabilitySlot.doctor_id == Doctor.user_id,
                AvailabilitySlot.status == SlotStatus.OPEN,
                AvailabilitySlot.start_at > now,
            )
            .correlate(Doctor)
            .scalar_subquery()
            .label("next_available_at")
        )

    def _search_stmt(self, p: DoctorSearchParams, now: datetime) -> Select[Any]:
        # The stored column, not a per-row subquery: a past value means "nothing open".
        next_slot = case((Doctor.next_available_at > now, Doctor.next_available_at), else_=None)
        stmt = select(Doctor, next_slot.label("next_available_at")).where(bookable())

        rank = None
        tsquery_text = prefix_tsquery(p.q) if p.q else None
        if tsquery_text:
            tsquery = func.to_tsquery("simple", tsquery_text)
            stmt = stmt.where(Doctor.search_vector.op("@@")(tsquery))
            rank = func.ts_rank(Doctor.search_vector, tsquery)
        if p.specialty:
            stmt = stmt.where(Doctor.specialties.contains([p.specialty]))
        if p.language:
            stmt = stmt.where(Doctor.languages.contains([p.language]))
        if p.min_fee is not None:
            stmt = stmt.where(Doctor.fee_paise >= p.min_fee)
        if p.max_fee is not None:
            stmt = stmt.where(Doctor.fee_paise <= p.max_fee)
        if p.min_rating is not None:
            stmt = stmt.where(Doctor.rating_avg >= p.min_rating)
        if p.available_from or p.available_to:
            window = [
                AvailabilitySlot.doctor_id == Doctor.user_id,
                AvailabilitySlot.status == SlotStatus.OPEN,
                AvailabilitySlot.start_at > now,
            ]
            if p.available_from:
                window.append(AvailabilitySlot.start_at >= p.available_from)
            if p.available_to:
                window.append(AvailabilitySlot.start_at < p.available_to)
            stmt = stmt.where(exists().where(*window))

        order: list[Any]
        match p.sort:
            case DoctorSort.RELEVANCE if rank is not None:
                order = [rank.desc(), Doctor.rating_avg.desc()]
            case DoctorSort.RELEVANCE | DoctorSort.RATING:
                order = [Doctor.rating_avg.desc(), Doctor.rating_count.desc()]
            case DoctorSort.FEE_ASC:
                order = [Doctor.fee_paise.asc()]
            case DoctorSort.FEE_DESC:
                order = [Doctor.fee_paise.desc()]
            case DoctorSort.EXPERIENCE:
                order = [Doctor.years_experience.desc()]
            case DoctorSort.NEXT_AVAILABLE:
                order = [Doctor.next_available_at.asc().nulls_last()]
        # user_id tiebreak makes pagination deterministic.
        return stmt.order_by(*order, Doctor.user_id)


async def refresh_next_available(
    session: AsyncSession, now: datetime, doctor_id: uuid.UUID | None = None
) -> int:
    """Recompute ``doctors.next_available_at`` (all bookable doctors, or one). Writes only rows
    whose value changed, so a quiet minute costs a read and no WAL."""
    nxt = (
        select(func.min(AvailabilitySlot.start_at))
        .where(
            AvailabilitySlot.doctor_id == Doctor.user_id,
            AvailabilitySlot.status == SlotStatus.OPEN,
            AvailabilitySlot.start_at > now,
        )
        .correlate(Doctor)
        .scalar_subquery()
    )
    stmt = (
        update(Doctor)
        .where(Doctor.next_available_at.is_distinct_from(nxt))
        .values(next_available_at=nxt)
        .execution_options(synchronize_session=False)
    )
    stmt = stmt.where(Doctor.user_id == doctor_id) if doctor_id else stmt.where(bookable())
    result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0)
