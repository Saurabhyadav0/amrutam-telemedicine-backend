"""Admin analytics over pre-aggregated daily rollups.

Rollups are recomputed by the worker (every 10 minutes for today/yesterday, or on demand for
a range) with set-based ``INSERT .. SELECT .. ON CONFLICT`` - idempotent, so re-running a day
is always safe. Business days are in IST. Only aggregates leave this module: no PHI.
"""

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequest
from app.modules.analytics.schemas import (
    DailyRow,
    DoctorPerformance,
    Overview,
    SpecialtyDemand,
)

BUSINESS_TZ = "Asia/Kolkata"
MAX_RANGE_DAYS = 366

_REFRESH_DAILY = text(
    """
    INSERT INTO analytics_daily AS a (
        day, consultations_booked, consultations_confirmed, consultations_completed,
        consultations_cancelled, consultations_no_show, gross_revenue_paise, refunds_paise,
        new_patients, new_doctors, ratings_count, ratings_sum, refreshed_at)
    SELECT d.day,
           coalesce(c.booked, 0), coalesce(c.confirmed, 0), coalesce(c.completed, 0),
           coalesce(c.cancelled, 0), coalesce(c.no_show, 0),
           coalesce(p.gross, 0), coalesce(p.refunds, 0),
           coalesce(np.n, 0), coalesce(nd.n, 0),
           coalesce(c.ratings_count, 0), coalesce(c.ratings_sum, 0), now()
    FROM generate_series(CAST(:from_day AS date), CAST(:to_day AS date), interval '1 day') AS g(ts)
    CROSS JOIN LATERAL (SELECT g.ts::date AS day) d
    LEFT JOIN (
        SELECT (scheduled_start AT TIME ZONE :tz)::date AS day,
               count(*) AS booked,
               count(*) FILTER (WHERE status IN ('confirmed','in_progress','completed','no_show')) AS confirmed,
               count(*) FILTER (WHERE status = 'completed') AS completed,
               count(*) FILTER (WHERE status = 'cancelled') AS cancelled,
               count(*) FILTER (WHERE status = 'no_show') AS no_show,
               count(rating) AS ratings_count,
               coalesce(sum(rating), 0) AS ratings_sum
        FROM consultations
        WHERE scheduled_start >= :from_ts AND scheduled_start < :to_ts
        GROUP BY 1
    ) c ON c.day = d.day
    LEFT JOIN (
        SELECT (c.scheduled_start AT TIME ZONE :tz)::date AS day,
               coalesce(sum(p.amount_paise)
                   FILTER (WHERE p.status IN ('succeeded','refund_pending','refunded')), 0) AS gross,
               coalesce(sum(p.refund_amount_paise) FILTER (WHERE p.status = 'refunded'), 0) AS refunds
        FROM payments p JOIN consultations c ON c.id = p.consultation_id
        WHERE c.scheduled_start >= :from_ts AND c.scheduled_start < :to_ts
        GROUP BY 1
    ) p ON p.day = d.day
    LEFT JOIN (
        SELECT (created_at AT TIME ZONE :tz)::date AS day, count(*) AS n
        FROM users
        WHERE role = 'patient' AND created_at >= :from_ts AND created_at < :to_ts
        GROUP BY 1
    ) np ON np.day = d.day
    LEFT JOIN (
        SELECT (verified_at AT TIME ZONE :tz)::date AS day, count(*) AS n
        FROM doctors
        WHERE verification_status = 'verified' AND verified_at >= :from_ts AND verified_at < :to_ts
        GROUP BY 1
    ) nd ON nd.day = d.day
    ON CONFLICT (day) DO UPDATE SET
        consultations_booked = EXCLUDED.consultations_booked,
        consultations_confirmed = EXCLUDED.consultations_confirmed,
        consultations_completed = EXCLUDED.consultations_completed,
        consultations_cancelled = EXCLUDED.consultations_cancelled,
        consultations_no_show = EXCLUDED.consultations_no_show,
        gross_revenue_paise = EXCLUDED.gross_revenue_paise,
        refunds_paise = EXCLUDED.refunds_paise,
        new_patients = EXCLUDED.new_patients,
        new_doctors = EXCLUDED.new_doctors,
        ratings_count = EXCLUDED.ratings_count,
        ratings_sum = EXCLUDED.ratings_sum,
        refreshed_at = EXCLUDED.refreshed_at
    """
)

# Slots can be deleted, so the per-doctor range is rebuilt (delete + insert) rather than upserted.
_CLEAR_DOCTOR_DAILY = text(
    "DELETE FROM analytics_doctor_daily WHERE day BETWEEN :from_day AND :to_day"
)
_REFRESH_DOCTOR_DAILY = text(
    """
    INSERT INTO analytics_doctor_daily (
        day, doctor_id, slots_published, slots_booked, consultations_completed,
        consultations_cancelled, revenue_paise, refreshed_at)
    SELECT day, doctor_id, sum(published), sum(booked), sum(completed), sum(cancelled),
           sum(revenue), now()
    FROM (
        SELECT (start_at AT TIME ZONE :tz)::date AS day, doctor_id,
               count(*) AS published, count(*) FILTER (WHERE status = 'booked') AS booked,
               0 AS completed, 0 AS cancelled, 0 AS revenue
        FROM availability_slots
        WHERE start_at >= :from_ts AND start_at < :to_ts
        GROUP BY 1, 2
        UNION ALL
        SELECT (c.scheduled_start AT TIME ZONE :tz)::date, c.doctor_id, 0, 0,
               count(*) FILTER (WHERE c.status = 'completed'),
               count(*) FILTER (WHERE c.status = 'cancelled'),
               coalesce(sum(p.amount_paise - CASE WHEN p.status = 'refunded'
                                                  THEN coalesce(p.refund_amount_paise, 0) ELSE 0 END)
                   FILTER (WHERE p.status IN ('succeeded','refund_pending','refunded')), 0)
        FROM consultations c LEFT JOIN payments p ON p.consultation_id = c.id
        WHERE c.scheduled_start >= :from_ts AND c.scheduled_start < :to_ts
        GROUP BY 1, 2
    ) x
    GROUP BY day, doctor_id
    """
)


def day_bounds(from_day: date, to_day: date) -> tuple[datetime, datetime]:
    tz = ZoneInfo(BUSINESS_TZ)
    start = datetime.combine(from_day, time.min, tzinfo=tz).astimezone(UTC)
    end = datetime.combine(to_day + timedelta(days=1), time.min, tzinfo=tz).astimezone(UTC)
    return start, end


def business_today() -> date:
    return datetime.now(ZoneInfo(BUSINESS_TZ)).date()


def validate_range(from_day: date, to_day: date) -> None:
    if to_day < from_day:
        raise BadRequest("'to' must not be before 'from'", code="invalid_range")
    if (to_day - from_day).days >= MAX_RANGE_DAYS:
        raise BadRequest(f"Range may span at most {MAX_RANGE_DAYS} days", code="range_too_large")


async def refresh_rollups(session: AsyncSession, from_day: date, to_day: date) -> None:
    """Recompute both rollups for [from_day, to_day] in one transaction (caller commits)."""
    from_ts, to_ts = day_bounds(from_day, to_day)
    params: dict[str, Any] = {
        "from_day": from_day,
        "to_day": to_day,
        "from_ts": from_ts,
        "to_ts": to_ts,
        "tz": BUSINESS_TZ,
    }
    await session.execute(_REFRESH_DAILY, params)
    await session.execute(_CLEAR_DOCTOR_DAILY, params)
    await session.execute(_REFRESH_DOCTOR_DAILY, params)


class AnalyticsService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def overview(self, from_day: date, to_day: date) -> Overview:
        validate_range(from_day, to_day)
        row = (
            await self._session.execute(
                text(
                    """
                    SELECT coalesce(sum(consultations_booked), 0) AS booked,
                           coalesce(sum(consultations_confirmed), 0) AS confirmed,
                           coalesce(sum(consultations_completed), 0) AS completed,
                           coalesce(sum(consultations_cancelled), 0) AS cancelled,
                           coalesce(sum(consultations_no_show), 0) AS no_show,
                           coalesce(sum(gross_revenue_paise), 0) AS gross,
                           coalesce(sum(refunds_paise), 0) AS refunds,
                           coalesce(sum(new_patients), 0) AS new_patients,
                           coalesce(sum(new_doctors), 0) AS new_doctors,
                           coalesce(sum(ratings_count), 0) AS ratings_count,
                           coalesce(sum(ratings_sum), 0) AS ratings_sum,
                           min(refreshed_at) AS as_of
                    FROM analytics_daily WHERE day BETWEEN :f AND :t
                    """
                ),
                {"f": from_day, "t": to_day},
            )
        ).one()
        return Overview(
            from_day=from_day,
            to_day=to_day,
            consultations_booked=row.booked,
            consultations_completed=row.completed,
            consultations_cancelled=row.cancelled,
            consultations_no_show=row.no_show,
            gross_revenue_paise=row.gross,
            refunds_paise=row.refunds,
            net_revenue_paise=row.gross - row.refunds,
            new_patients=row.new_patients,
            new_doctors=row.new_doctors,
            cancellation_rate=_ratio(row.cancelled, row.booked),
            no_show_rate=_ratio(row.no_show, row.confirmed),
            completion_rate=_ratio(row.completed, row.confirmed),
            average_rating=round(row.ratings_sum / row.ratings_count, 2)
            if row.ratings_count
            else None,
            as_of=row.as_of,
        )

    async def daily(self, from_day: date, to_day: date) -> list[DailyRow]:
        validate_range(from_day, to_day)
        rows = await self._session.execute(
            # Continuous series: days not yet rolled up come back as zeros, not gaps.
            text(
                """
                SELECT g.ts::date AS day,
                       coalesce(a.consultations_booked, 0) AS consultations_booked,
                       coalesce(a.consultations_completed, 0) AS consultations_completed,
                       coalesce(a.consultations_cancelled, 0) AS consultations_cancelled,
                       coalesce(a.gross_revenue_paise, 0) AS gross_revenue_paise,
                       coalesce(a.refunds_paise, 0) AS refunds_paise,
                       coalesce(a.new_patients, 0) AS new_patients
                FROM generate_series(CAST(:f AS date), CAST(:t AS date), interval '1 day') AS g(ts)
                LEFT JOIN analytics_daily a ON a.day = g.ts::date
                ORDER BY 1
                """
            ),
            {"f": from_day, "t": to_day},
        )
        return [
            DailyRow(
                day=r.day,
                consultations_booked=r.consultations_booked,
                consultations_completed=r.consultations_completed,
                consultations_cancelled=r.consultations_cancelled,
                net_revenue_paise=r.gross_revenue_paise - r.refunds_paise,
                new_patients=r.new_patients,
            )
            for r in rows
        ]

    async def doctors(
        self, from_day: date, to_day: date, *, sort: str, limit: int
    ) -> list[DoctorPerformance]:
        validate_range(from_day, to_day)
        rows = await self._session.execute(
            text(
                """
                SELECT a.doctor_id, d.display_name, d.rating_avg,
                       sum(a.slots_published) AS published, sum(a.slots_booked) AS booked,
                       sum(a.consultations_completed) AS completed,
                       sum(a.consultations_cancelled) AS cancelled,
                       sum(a.revenue_paise) AS revenue_paise,
                       round(sum(a.slots_booked)::numeric / nullif(sum(a.slots_published), 0), 4)
                           AS utilization
                FROM analytics_doctor_daily a JOIN doctors d ON d.user_id = a.doctor_id
                WHERE a.day BETWEEN :f AND :t
                GROUP BY a.doctor_id, d.display_name, d.rating_avg
                -- Sort key is a bound parameter: no SQL is ever built from input.
                ORDER BY
                    CASE WHEN :sort = 'revenue' THEN sum(a.revenue_paise) END DESC NULLS LAST,
                    CASE WHEN :sort = 'completed' THEN sum(a.consultations_completed) END DESC NULLS LAST,
                    CASE WHEN :sort = 'utilization'
                         THEN sum(a.slots_booked)::numeric / nullif(sum(a.slots_published), 0) END DESC NULLS LAST,
                    a.doctor_id
                LIMIT :limit
                """
            ),
            {"f": from_day, "t": to_day, "limit": limit, "sort": sort},
        )
        return [
            DoctorPerformance(
                doctor_id=r.doctor_id,
                display_name=r.display_name,
                rating_avg=float(r.rating_avg),
                slots_published=r.published,
                slots_booked=r.booked,
                consultations_completed=r.completed,
                consultations_cancelled=r.cancelled,
                revenue_paise=r.revenue_paise,
                utilization=float(r.utilization) if r.utilization is not None else None,
            )
            for r in rows
        ]

    async def specialties(self, from_day: date, to_day: date) -> list[SpecialtyDemand]:
        validate_range(from_day, to_day)
        rows = await self._session.execute(
            text(
                """
                SELECT s.specialty, count(DISTINCT a.doctor_id) AS doctors,
                       sum(a.slots_published) AS published, sum(a.slots_booked) AS booked,
                       sum(a.revenue_paise) AS revenue_paise
                FROM analytics_doctor_daily a
                JOIN doctors d ON d.user_id = a.doctor_id
                CROSS JOIN LATERAL unnest(d.specialties) AS s(specialty)
                WHERE a.day BETWEEN :f AND :t
                GROUP BY s.specialty
                ORDER BY booked DESC, s.specialty
                """
            ),
            {"f": from_day, "t": to_day},
        )
        return [
            SpecialtyDemand(
                specialty=r.specialty,
                doctors=r.doctors,
                slots_published=r.published,
                slots_booked=r.booked,
                revenue_paise=r.revenue_paise,
                utilization=_ratio(r.booked, r.published),
            )
            for r in rows
        ]


def _ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None
