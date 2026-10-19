import asyncio
import json
import time
import uuid
from datetime import timedelta

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import utcnow
from app.modules.outbox.models import OutboxEvent
from app.modules.payments.gateway import sign_webhook
from app.worker.jobs import WorkerDeps, dispatch_outbox, release_expired_holds
from tests.integration.helpers import (
    admin_session,
    bearer,
    book,
    confirmed_consultation,
    idem,
    open_slot,
    patient_token,
    pay,
    verified_doctor,
)


async def public_slot_ids(client: AsyncClient, doctor_id: str) -> set[str]:
    resp = await client.get(
        f"/v1/doctors/{doctor_id}/slots", params={"to": (utcnow() + timedelta(days=3)).isoformat()}
    )
    return {s["id"] for s in resp.json()}


async def outbox_topics(app: FastAPI, aggregate_id: str) -> list[str]:
    async with app.state.db.sessionmaker() as session:
        rows = await session.scalars(
            select(OutboxEvent.topic).where(OutboxEvent.aggregate_id == aggregate_id)
        )
        return list(rows)


async def test_happy_path_hold_pay_confirm_notify(
    app: FastAPI, client: AsyncClient, worker: WorkerDeps
) -> None:
    _, admin_token = await admin_session(app, client)
    doctor, doctor_tok = await verified_doctor(client, admin_token)
    slot_id = await open_slot(client, doctor_tok)
    patient, token = await patient_token(client)

    resp = await book(client, token, slot_id, chief_complaint="Bloating")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["consultation"]["status"] == "pending_payment"
    assert body["consultation"]["chief_complaint"] == "Bloating"
    assert body["payment"]["status"] == "created" and body["payment"]["amount_paise"] == 50000
    assert body["checkout_url"] and body["hold_expires_at"]
    assert slot_id not in await public_slot_ids(client, doctor["id"])  # held = hidden

    assert await pay(client, token, body["payment"]["id"]) == "confirmed"
    consultation = (
        await client.get(f"/v1/consultations/{body['consultation']['id']}", headers=bearer(token))
    ).json()
    assert consultation["status"] == "confirmed"

    assert "consultation.confirmed" in await outbox_topics(app, body["consultation"]["id"])
    await dispatch_outbox(worker)
    notified = {(str(uid), t) for uid, t, _ in worker.notifier.sent}  # type: ignore[attr-defined]
    assert (patient["id"], "consultation_confirmed") in notified
    assert (doctor["id"], "consultation_confirmed") in notified

    mine = (await client.get("/v1/consultations", headers=bearer(doctor_tok))).json()["items"]
    assert [c["id"] for c in mine] == [body["consultation"]["id"]]


async def test_only_one_of_many_concurrent_bookings_wins(app: FastAPI, client: AsyncClient) -> None:
    _, admin_token = await admin_session(app, client)
    _, doctor_tok = await verified_doctor(client, admin_token)
    slot_id = await open_slot(client, doctor_tok)
    tokens = [(await patient_token(client))[1] for _ in range(8)]

    results = await asyncio.gather(*[book(client, t, slot_id) for t in tokens])
    statuses = sorted(r.status_code for r in results)
    assert statuses.count(201) == 1, statuses
    assert all(r.json()["code"] == "slot_unavailable" for r in results if r.status_code == 409)


async def test_retries_never_double_book(app: FastAPI, client: AsyncClient) -> None:
    _, admin_token = await admin_session(app, client)
    _, doctor_tok = await verified_doctor(client, admin_token)
    slot_id = await open_slot(client, doctor_tok)
    _, token = await patient_token(client)
    payload = {"slot_id": slot_id, "consent_to_teleconsultation": True}

    key = idem()
    first = await client.post("/v1/consultations", json=payload, headers={**bearer(token), **key})
    same_key = await client.post(
        "/v1/consultations", json=payload, headers={**bearer(token), **key}
    )
    assert same_key.headers["Idempotent-Replayed"] == "true"
    # A new key for the same slot (e.g. app restarted mid-checkout) resumes the same booking.
    new_key = await client.post(
        "/v1/consultations", json=payload, headers={**bearer(token), **idem()}
    )
    ids = {r.json()["consultation"]["id"] for r in (first, same_key, new_key)}
    assert len(ids) == 1
    assert new_key.json()["payment"]["id"] == first.json()["payment"]["id"]


async def test_patient_cannot_double_book_same_time(app: FastAPI, client: AsyncClient) -> None:
    _, admin_token = await admin_session(app, client)
    _, doc1 = await verified_doctor(client, admin_token)
    _, doc2 = await verified_doctor(client, admin_token)
    slot1 = await open_slot(client, doc1, hours=30)
    slot2 = await open_slot(client, doc2, hours=30.25)  # overlaps slot1
    _, token = await patient_token(client)
    assert (await book(client, token, slot1)).status_code == 201
    clash = await book(client, token, slot2)
    assert clash.status_code == 409 and clash.json()["code"] == "patient_time_conflict"
    # The failed attempt released its hold: another patient can take slot2.
    _, other = await patient_token(client)
    assert (await book(client, other, slot2)).status_code == 201


async def test_booking_guards(app: FastAPI, client: AsyncClient) -> None:
    _, admin_token = await admin_session(app, client)
    _, doctor_tok = await verified_doctor(client, admin_token)
    slot_id = await open_slot(client, doctor_tok)
    _, token = await patient_token(client)

    no_consent = await client.post(
        "/v1/consultations", json={"slot_id": slot_id}, headers={**bearer(token), **idem()}
    )
    assert no_consent.status_code == 422
    no_key = await client.post(
        "/v1/consultations",
        json={"slot_id": slot_id, "consent_to_teleconsultation": True},
        headers=bearer(token),
    )
    assert no_key.status_code == 400
    doctor_books = await book(client, doctor_tok, slot_id)
    assert doctor_books.status_code == 403
    assert (await book(client, token, str(uuid.uuid4()))).status_code == 409

    too_soon = await open_slot(client, doctor_tok, hours=2 / 60)  # inside the 5-minute lead time
    assert (await book(client, token, too_soon)).json()["code"] == "slot_unavailable"


async def test_failed_payment_releases_slot(app: FastAPI, client: AsyncClient) -> None:
    _, admin_token = await admin_session(app, client)
    doctor, doctor_tok = await verified_doctor(client, admin_token)
    slot_id = await open_slot(client, doctor_tok)
    _, token = await patient_token(client)
    body = (await book(client, token, slot_id)).json()

    assert await pay(client, token, body["payment"]["id"], outcome="failed") == "failed"
    consultation = (
        await client.get(f"/v1/consultations/{body['consultation']['id']}", headers=bearer(token))
    ).json()
    assert (
        consultation["status"] == "cancelled" and consultation["cancel_reason"] == "payment_failed"
    )
    assert slot_id in await public_slot_ids(client, doctor["id"])
    _, other = await patient_token(client)
    assert (await book(client, other, slot_id)).status_code == 201


async def test_webhook_security_and_dedup(app: FastAPI, client: AsyncClient) -> None:
    _, admin_token = await admin_session(app, client)
    _, doctor_tok = await verified_doctor(client, admin_token)
    slot_id = await open_slot(client, doctor_tok)
    _, token = await patient_token(client)
    booked = (await book(client, token, slot_id)).json()
    secret = get_settings().payment_webhook_secret
    event = json.dumps(
        {
            "id": f"evt_{uuid.uuid4().hex}",
            "type": "payment.succeeded",
            "data": {
                "provider_payment_id": booked["payment"]["provider_payment_id"],
                "amount_paise": 50000,
            },
        }
    ).encode()
    url = "/v1/payments/webhooks/mockpay"

    forged = await client.post(
        url, content=event, headers={"X-MockPay-Signature": sign_webhook("wrong-secret" * 4, event)}
    )
    assert forged.status_code == 401
    stale = await client.post(
        url,
        content=event,
        headers={"X-MockPay-Signature": sign_webhook(secret, event, int(time.time()) - 3600)},
    )
    assert stale.status_code == 401
    unsigned = await client.post(url, content=event)
    assert unsigned.status_code == 401

    good = {"X-MockPay-Signature": sign_webhook(secret, event)}
    assert (await client.post(url, content=event, headers=good)).json() == {"status": "confirmed"}
    assert (await client.post(url, content=event, headers=good)).json() == {"status": "duplicate"}
    assert (await outbox_topics(app, booked["consultation"]["id"])).count(
        "consultation.confirmed"
    ) == 1


async def test_abandoned_checkout_expires_and_late_payment_is_refunded(
    app: FastAPI, client: AsyncClient, worker: WorkerDeps
) -> None:
    _, admin_token = await admin_session(app, client)
    doctor, doctor_tok = await verified_doctor(client, admin_token)
    slot_id = await open_slot(client, doctor_tok)
    _, token = await patient_token(client)
    booked = (await book(client, token, slot_id)).json()

    released = await release_expired_holds(worker, now=utcnow() + timedelta(minutes=11))
    assert released >= 1
    consultation = (
        await client.get(f"/v1/consultations/{booked['consultation']['id']}", headers=bearer(token))
    ).json()
    assert (
        consultation["status"] == "cancelled" and consultation["cancel_reason"] == "payment_timeout"
    )
    assert slot_id in await public_slot_ids(client, doctor["id"])

    # The patient finishes paying after the hold was released: money must go back.
    assert await pay(client, token, booked["payment"]["id"]) == "compensated"
    await dispatch_outbox(worker)
    async with app.state.db.sessionmaker() as session:
        from app.modules.payments.models import Payment

        payment = await session.get(Payment, uuid.UUID(booked["payment"]["id"]))
    assert payment is not None and payment.status == "refunded" and payment.provider_refund_id


async def test_cancellation_refund_policy(
    app: FastAPI, client: AsyncClient, worker: WorkerDeps
) -> None:
    _, admin_token = await admin_session(app, client)

    early = await confirmed_consultation(client, admin_token, hours=24)
    resp = await client.post(
        f"/v1/consultations/{early['id']}/cancel",
        json={"reason": "travelling"},
        headers={**bearer(early["patient_token"]), **idem()},
    )
    assert resp.status_code == 200 and resp.json()["refund_paise"] == 50000
    assert early["slot_id"] in await public_slot_ids(client, early["doctor"]["id"])

    late = await confirmed_consultation(client, admin_token, hours=1)
    resp = await client.post(
        f"/v1/consultations/{late['id']}/cancel",
        json={"reason": "busy"},
        headers={**bearer(late["patient_token"]), **idem()},
    )
    assert resp.json()["refund_paise"] == 0  # inside the 2h window

    by_doctor = await confirmed_consultation(client, admin_token, hours=1)
    resp = await client.post(
        f"/v1/consultations/{by_doctor['id']}/cancel",
        json={"reason": "emergency"},
        headers={**bearer(by_doctor["doctor_token"]), **idem()},
    )
    assert resp.json()["refund_paise"] == 50000  # doctor cancellations always refunded

    again = await client.post(
        f"/v1/consultations/{early['id']}/cancel",
        json={"reason": "twice"},
        headers={**bearer(early["patient_token"]), **idem()},
    )
    assert again.status_code == 409 and again.json()["code"] == "invalid_transition"
    assert await dispatch_outbox(worker) >= 2


async def test_other_users_cannot_see_or_touch_consultation(
    app: FastAPI, client: AsyncClient
) -> None:
    _, admin_token = await admin_session(app, client)
    c = await confirmed_consultation(client, admin_token)
    _, stranger = await patient_token(client)
    assert (
        await client.get(f"/v1/consultations/{c['id']}", headers=bearer(stranger))
    ).status_code == 404
    cancel = await client.post(
        f"/v1/consultations/{c['id']}/cancel",
        json={"reason": "evil"},
        headers={**bearer(stranger), **idem()},
    )
    assert cancel.status_code == 404
    _, other_doctor = await verified_doctor(client, admin_token)
    assert (
        await client.post(f"/v1/consultations/{c['id']}/start", headers=bearer(other_doctor))
    ).status_code == 404

    # Staff can read, and that access is audited.
    staff_view = await client.get(f"/v1/consultations/{c['id']}", headers=bearer(admin_token))
    assert staff_view.status_code == 200
    logs = await client.get(
        "/v1/admin/audit-logs",
        params={"action": "consultation.viewed_by_staff", "resource_id": c["id"]},
        headers=bearer(admin_token),
    )
    assert len(logs.json()) == 1


async def test_free_consultation_confirms_immediately(app: FastAPI, client: AsyncClient) -> None:
    _, admin_token = await admin_session(app, client)
    c = await confirmed_consultation(client, admin_token, fee=0)
    assert c["payment"] is None
    got = (
        await client.get(f"/v1/consultations/{c['id']}", headers=bearer(c["patient_token"]))
    ).json()
    assert got["status"] == "confirmed"


async def test_lifecycle_start_join_complete_review(app: FastAPI, client: AsyncClient) -> None:
    _, admin_token = await admin_session(app, client)
    far = await confirmed_consultation(client, admin_token, hours=24)
    early = await client.post(
        f"/v1/consultations/{far['id']}/start", headers=bearer(far["doctor_token"])
    )
    assert early.status_code == 409 and early.json()["code"] == "outside_window"

    c = await confirmed_consultation(client, admin_token, hours=10 / 60)
    url = f"/v1/consultations/{c['id']}"
    assert (await client.get(f"{url}/join", headers=bearer(c["patient_token"]))).json()[
        "code"
    ] == "not_started"
    assert (
        await client.post(f"{url}/start", headers=bearer(c["patient_token"]))
    ).status_code == 403

    started = await client.post(f"{url}/start", headers=bearer(c["doctor_token"]))
    assert started.status_code == 200 and started.json()["room"] == f"consult-{c['id']}"
    joined = await client.get(f"{url}/join", headers=bearer(c["patient_token"]))
    assert joined.status_code == 200 and joined.json()["token"] != started.json()["token"]

    review_too_soon = await client.post(
        f"{url}/review", json={"rating": 5}, headers={**bearer(c["patient_token"]), **idem()}
    )
    assert review_too_soon.status_code == 409

    done = await client.post(
        f"{url}/complete", json={"notes": "Pitta aggravation"}, headers=bearer(c["doctor_token"])
    )
    assert done.json()["status"] == "completed"
    assert (
        await client.post(f"{url}/complete", json={}, headers=bearer(c["doctor_token"]))
    ).status_code == 409

    review = await client.post(
        f"{url}/review",
        json={"rating": 4, "review": "Helpful"},
        headers={**bearer(c["patient_token"]), **idem()},
    )
    assert review.json()["rating"] == 4
    again = await client.post(
        f"{url}/review", json={"rating": 1}, headers={**bearer(c["patient_token"]), **idem()}
    )
    assert again.json()["code"] == "already_reviewed"
    profile = (await client.get(f"/v1/doctors/{c['doctor']['id']}")).json()
    assert profile["rating_avg"] == 4.0 and profile["rating_count"] == 1


async def test_no_show_only_after_grace_period(app: FastAPI, client: AsyncClient) -> None:
    _, admin_token = await admin_session(app, client)
    c = await confirmed_consultation(client, admin_token, hours=10 / 60)
    resp = await client.post(
        f"/v1/consultations/{c['id']}/no-show", headers=bearer(c["doctor_token"])
    )
    assert resp.status_code == 409 and resp.json()["code"] == "too_early"
