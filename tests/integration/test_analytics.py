from datetime import timedelta

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text

from app.modules.analytics.service import business_today
from app.worker.jobs import WorkerDeps, dispatch_outbox, refresh_analytics
from tests.integration.helpers import admin_session, bearer, confirmed_consultation, idem


async def test_rollups_reflect_bookings_revenue_and_refunds(
    app: FastAPI, client: AsyncClient, worker: WorkerDeps
) -> None:
    _, admin_token = await admin_session(app, client)
    # Isolate from other tests: scope to one doctor's numbers + platform deltas.
    await refresh_analytics(
        worker,
        from_day=business_today() - timedelta(days=1),
        to_day=business_today() + timedelta(days=3),
    )
    before = (
        await client.get(
            "/v1/admin/analytics/overview",
            params={"to": str(business_today() + timedelta(days=3))},
            headers=bearer(admin_token),
        )
    ).json()

    kept = await confirmed_consultation(client, admin_token, hours=24, fee=40000)
    cancelled = await confirmed_consultation(client, admin_token, hours=26, fee=60000)
    await client.post(
        f"/v1/consultations/{cancelled['id']}/cancel",
        json={"reason": "changed mind"},
        headers={**bearer(cancelled["patient_token"]), **idem()},
    )
    await dispatch_outbox(worker)  # processes the refund

    # Admin-triggered refresh goes through the outbox to the worker.
    day_to = business_today() + timedelta(days=3)
    accepted = await client.post(
        "/v1/admin/analytics/refresh",
        json={"from_day": str(business_today() - timedelta(days=1)), "to_day": str(day_to)},
        headers=bearer(admin_token),
    )
    assert accepted.status_code == 202
    await dispatch_outbox(worker)

    after = (
        await client.get(
            "/v1/admin/analytics/overview", params={"to": str(day_to)}, headers=bearer(admin_token)
        )
    ).json()
    assert after["consultations_booked"] - before["consultations_booked"] == 2
    assert after["consultations_cancelled"] - before["consultations_cancelled"] == 1
    assert after["gross_revenue_paise"] - before["gross_revenue_paise"] == 100000
    assert after["refunds_paise"] - before["refunds_paise"] == 60000
    assert after["net_revenue_paise"] == after["gross_revenue_paise"] - after["refunds_paise"]
    assert after["as_of"] is not None

    perf = (
        await client.get(
            "/v1/admin/analytics/doctors",
            params={"to": str(day_to), "limit": 200},
            headers=bearer(admin_token),
        )
    ).json()
    by_id = {p["doctor_id"]: p for p in perf}
    kept_row = by_id[kept["doctor"]["id"]]
    assert kept_row["slots_published"] == 1 and kept_row["slots_booked"] == 1
    assert kept_row["utilization"] == 1.0 and kept_row["revenue_paise"] == 40000
    assert by_id[cancelled["doctor"]["id"]]["revenue_paise"] == 0  # fully refunded

    specialties = (
        await client.get(
            "/v1/admin/analytics/specialties",
            params={"to": str(day_to)},
            headers=bearer(admin_token),
        )
    ).json()
    assert "digestive_health" in {s["specialty"] for s in specialties}
    daily = (
        await client.get(
            "/v1/admin/analytics/daily", params={"to": str(day_to)}, headers=bearer(admin_token)
        )
    ).json()
    assert len(daily) == 30


async def test_refresh_is_idempotent(app: FastAPI, worker: WorkerDeps) -> None:
    today = business_today()
    await refresh_analytics(worker, from_day=today, to_day=today)
    async with app.state.db.sessionmaker() as session:
        first = (
            await session.execute(
                text("SELECT * FROM analytics_daily WHERE day = :d"), {"d": today}
            )
        ).one()
    await refresh_analytics(worker, from_day=today, to_day=today)
    async with app.state.db.sessionmaker() as session:
        second = (
            await session.execute(
                text("SELECT * FROM analytics_daily WHERE day = :d"), {"d": today}
            )
        ).one()
    assert first.consultations_booked == second.consultations_booked
    assert first.gross_revenue_paise == second.gross_revenue_paise


async def test_analytics_is_admin_only_and_validates_range(
    app: FastAPI, client: AsyncClient
) -> None:
    _, admin_token = await admin_session(app, client)
    c = await confirmed_consultation(client, admin_token)
    assert (
        await client.get("/v1/admin/analytics/overview", headers=bearer(c["doctor_token"]))
    ).status_code == 403
    bad = await client.get(
        "/v1/admin/analytics/overview",
        params={"from": "2026-05-01", "to": "2026-04-01"},
        headers=bearer(admin_token),
    )
    assert bad.status_code == 400
    huge = await client.get(
        "/v1/admin/analytics/daily",
        params={"from": "2020-01-01", "to": "2026-01-01"},
        headers=bearer(admin_token),
    )
    assert huge.status_code == 400
    sort_injection = await client.get(
        "/v1/admin/analytics/doctors",
        params={"sort": "revenue; DROP TABLE users"},
        headers=bearer(admin_token),
    )
    assert sort_injection.status_code == 422
