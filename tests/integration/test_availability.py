import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, update

from app.modules.availability.models import AvailabilitySlot
from tests.integration.helpers import (
    admin_session,
    bearer,
    doctor_session,
    idem,
    practice,
    unique_token,
    verified_doctor,
)


def rule(days_ahead: int = 2, **overrides: object) -> dict:
    start = date.today() + timedelta(days=days_ahead)
    return {
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=6)).isoformat(),
        "weekdays": [0, 2],  # Mon, Wed
        "day_start": "10:00",
        "day_end": "13:00",
        "slot_minutes": 30,
        "timezone": "Asia/Kolkata",
        **overrides,
    }


def at(hours: float) -> str:
    return (datetime.now(UTC).replace(microsecond=0) + timedelta(hours=hours)).isoformat()


async def publish(client: AsyncClient, token: str, body: dict) -> dict:
    resp = await client.post("/v1/doctors/me/slots", json=body, headers={**bearer(token), **idem()})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_unverified_doctor_cannot_publish(client: AsyncClient) -> None:
    _, token = await doctor_session(client)
    await client.put("/v1/doctors/me", json=practice(), headers=bearer(token))
    resp = await client.post(
        "/v1/doctors/me/slots", json={"rule": rule()}, headers={**bearer(token), **idem()}
    )
    assert resp.status_code == 403 and resp.json()["code"] == "doctor_not_verified"


async def test_weekly_rule_expands_in_local_time(app: FastAPI, client: AsyncClient) -> None:
    _, admin_token = await admin_session(app, client)
    _, token = await verified_doctor(client, admin_token)
    out = await publish(client, token, {"rule": rule()})

    assert out["created"] == 12  # 2 weekdays in a 7-day span x 6 half-hour slots
    first = out["slots"][0]
    start = datetime.fromisoformat(first["start_at"])
    assert (start.hour, start.minute) == (4, 30)  # 10:00 IST == 04:30 UTC
    assert datetime.fromisoformat(first["end_at"]) - start == timedelta(minutes=30)


async def test_republishing_is_a_no_op(app: FastAPI, client: AsyncClient) -> None:
    _, admin_token = await admin_session(app, client)
    _, token = await verified_doctor(client, admin_token)
    await publish(client, token, {"rule": rule()})
    again = await publish(client, token, {"rule": rule()})
    assert again["created"] == 0 and again["skipped_overlapping"] == 12


async def test_overlaps_skipped_within_batch(app: FastAPI, client: AsyncClient) -> None:
    _, admin_token = await admin_session(app, client)
    _, token = await verified_doctor(client, admin_token)
    out = await publish(
        client,
        token,
        {
            "mode": "audio",
            "slots": [
                {"start_at": at(24), "end_at": at(24.5)},
                {"start_at": at(24.25), "end_at": at(24.75)},  # overlaps the first
                {"start_at": at(24.5), "end_at": at(25)},  # touches, doesn't overlap
            ],
        },
    )
    assert out["created"] == 2 and out["skipped_overlapping"] == 1
    assert {s["mode"] for s in out["slots"]} == {"audio"}


async def test_concurrent_publishes_never_overlap(app: FastAPI, client: AsyncClient) -> None:
    _, admin_token = await admin_session(app, client)
    doctor, token = await verified_doctor(client, admin_token)
    body = {"slots": [{"start_at": at(48), "end_at": at(48.5)}]}
    results = await asyncio.gather(
        *[
            client.post("/v1/doctors/me/slots", json=body, headers={**bearer(token), **idem()})
            for _ in range(6)
        ]
    )
    assert sum(r.json()["created"] for r in results) == 1
    async with app.state.db.sessionmaker() as session:
        count = len(
            (
                await session.scalars(
                    select(AvailabilitySlot).where(
                        AvailabilitySlot.doctor_id == uuid.UUID(doctor["id"])
                    )
                )
            ).all()
        )
    assert count == 1


async def test_invalid_batches_rejected(app: FastAPI, client: AsyncClient) -> None:
    _, admin_token = await admin_session(app, client)
    _, token = await verified_doctor(client, admin_token)
    headers = {**bearer(token), **idem()}
    cases = [
        ({"slots": [{"start_at": at(-2), "end_at": at(-1.5)}]}, 400),  # past
        (
            {"slots": [{"start_at": at(24 * 120), "end_at": at(24 * 120 + 0.5)}]},
            400,
        ),  # beyond horizon
        ({"slots": [{"start_at": at(24), "end_at": at(28)}]}, 400),  # 4h slot
        ({"rule": rule(end_date=(date.today() + timedelta(days=60)).isoformat())}, 422),
        ({"rule": rule(), "slots": []}, 422),  # both sources
        (
            {"slots": [{"start_at": "2030-01-01T10:00:00", "end_at": "2030-01-01T10:30:00"}]},
            422,
        ),  # naive
    ]
    for body, expected in cases:
        resp = await client.post("/v1/doctors/me/slots", json=body, headers={**headers, **idem()})
        assert resp.status_code == expected, (body, resp.text)
    no_key = await client.post("/v1/doctors/me/slots", json={"rule": rule()}, headers=bearer(token))
    assert no_key.status_code == 400


async def test_remove_slot_only_when_open(app: FastAPI, client: AsyncClient) -> None:
    _, admin_token = await admin_session(app, client)
    _, token = await verified_doctor(client, admin_token)
    _, other_token = await verified_doctor(client, admin_token)
    slots = (
        await publish(
            client,
            token,
            {
                "slots": [
                    {"start_at": at(30), "end_at": at(30.5)},
                    {"start_at": at(31), "end_at": at(31.5)},
                ]
            },
        )
    )["slots"]
    open_id, booked_id = slots[0]["id"], slots[1]["id"]
    async with app.state.db.sessionmaker() as session, session.begin():
        await session.execute(
            update(AvailabilitySlot)
            .where(AvailabilitySlot.id == uuid.UUID(booked_id))
            .values(status="booked")
        )

    assert (
        await client.delete(f"/v1/doctors/me/slots/{open_id}", headers=bearer(other_token))
    ).status_code == 404
    assert (
        await client.delete(f"/v1/doctors/me/slots/{booked_id}", headers=bearer(token))
    ).status_code == 409
    assert (
        await client.delete(f"/v1/doctors/me/slots/{open_id}", headers=bearer(token))
    ).status_code == 204


async def test_public_slots_and_next_available(app: FastAPI, client: AsyncClient) -> None:
    _, admin_token = await admin_session(app, client)
    tag = unique_token()
    doctor, token = await verified_doctor(client, admin_token, name=f"Vaidya {tag}")
    [card] = (await client.get("/v1/doctors", params={"q": tag})).json()["items"]
    assert card["next_available_at"] is None
    assert (
        await client.get(
            "/v1/doctors", params={"q": tag, "available_from": at(0), "available_to": at(72)}
        )
    ).json()["items"] == []

    published = await publish(
        client,
        token,
        {
            "slots": [
                {"start_at": at(26), "end_at": at(26.5)},
                {"start_at": at(50), "end_at": at(50.5)},
            ]
        },
    )
    public = (
        await client.get(f"/v1/doctors/{doctor['id']}/slots", params={"from": at(0), "to": at(72)})
    ).json()
    assert [s["id"] for s in public] == [s["id"] for s in published["slots"]]
    assert "status" not in public[0]  # internal state is not exposed publicly

    [card] = (await client.get("/v1/doctors", params={"q": tag})).json()["items"]
    assert datetime.fromisoformat(card["next_available_at"]) == datetime.fromisoformat(
        published["slots"][0]["start_at"]
    )
    found = (
        await client.get(
            "/v1/doctors", params={"q": tag, "available_from": at(40), "available_to": at(60)}
        )
    ).json()["items"]
    assert len(found) == 1
