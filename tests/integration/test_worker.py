import uuid
from datetime import timedelta

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from app.core.db import utcnow
from app.middleware.idempotency import IdempotencyKey
from app.modules.outbox.models import OutboxEvent, OutboxStatus
from app.worker.jobs import (
    MAX_ATTEMPTS,
    WorkerDeps,
    dispatch_outbox,
    ensure_partitions,
    purge_idempotency_keys,
)
from tests.integration.helpers import admin_session, bearer


async def add_event(app: FastAPI, topic: str) -> int:
    async with app.state.db.sessionmaker() as session, session.begin():
        event = OutboxEvent(topic=topic, aggregate_id=uuid.uuid4().hex, payload={})
        session.add(event)
        await session.flush()
        return event.id


async def load(app: FastAPI, event_id: int) -> OutboxEvent:
    async with app.state.db.sessionmaker() as session:
        event = await session.get(OutboxEvent, event_id)
        assert event is not None
        return event


async def test_failing_handler_backs_off_then_dead_letters(
    app: FastAPI, client: AsyncClient, worker: WorkerDeps
) -> None:
    calls = 0

    async def flaky(event: OutboxEvent, deps: WorkerDeps) -> None:
        nonlocal calls
        calls += 1
        raise ConnectionError("sms provider down")

    worker.handlers = {"test.flaky": flaky}
    event_id = await add_event(app, "test.flaky")

    await dispatch_outbox(worker)
    first = await load(app, event_id)
    assert first.status == OutboxStatus.PENDING and first.attempts == 1
    assert first.next_attempt_at > utcnow()  # backed off, not hot-looping
    await dispatch_outbox(worker)
    assert calls == 1  # not due yet

    for _ in range(MAX_ATTEMPTS):
        await dispatch_outbox(worker, now=utcnow() + timedelta(hours=1))
    dead = await load(app, event_id)
    assert dead.status == OutboxStatus.DEAD and dead.attempts == MAX_ATTEMPTS
    assert "sms provider down" in (dead.last_error or "")

    _, admin_token = await admin_session(app, client)
    dlq = (await client.get("/v1/admin/outbox", headers=bearer(admin_token))).json()
    assert event_id in {e["id"] for e in dlq}
    retried = await client.post(f"/v1/admin/outbox/{event_id}/retry", headers=bearer(admin_token))
    assert retried.json()["status"] == "pending" and retried.json()["attempts"] == 0


async def test_unknown_topic_goes_straight_to_dlq(app: FastAPI, worker: WorkerDeps) -> None:
    worker.handlers = {"something.else": lambda e, d: None}  # type: ignore[dict-item]
    event_id = await add_event(app, "nobody.listens")
    await dispatch_outbox(worker)
    assert (await load(app, event_id)).status == OutboxStatus.DEAD


async def test_successful_events_are_marked_done(app: FastAPI, worker: WorkerDeps) -> None:
    seen: list[int] = []

    async def ok(event: OutboxEvent, deps: WorkerDeps) -> None:
        seen.append(event.id)

    worker.handlers = {"test.ok": ok}
    event_id = await add_event(app, "test.ok")
    await dispatch_outbox(worker)
    await dispatch_outbox(worker)
    assert seen.count(event_id) == 1
    done = await load(app, event_id)
    assert done.status == OutboxStatus.DONE and done.processed_at is not None


async def test_housekeeping_jobs(app: FastAPI, worker: WorkerDeps) -> None:
    async with app.state.db.sessionmaker() as session, session.begin():
        session.add(
            IdempotencyKey(
                owner="anon",
                key=f"old-{uuid.uuid4().hex}",
                method="POST",
                path="/x",
                request_hash="0" * 64,
                status="completed",
                locked_until=utcnow(),
                expires_at=utcnow() - timedelta(days=1),
            )
        )
    assert await purge_idempotency_keys(worker) >= 1
    async with app.state.db.sessionmaker() as session:
        left = await session.scalars(
            select(IdempotencyKey).where(IdempotencyKey.expires_at < utcnow())
        )
        assert list(left) == []
    await ensure_partitions(worker, months_ahead=4)  # idempotent
    await ensure_partitions(worker, months_ahead=4)
