import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.modules.prescriptions.signing import verify
from tests.integration.helpers import (
    admin_session,
    bearer,
    book,
    confirmed_consultation,
    idem,
    medicine_id,
    open_slot,
    pay,
)


def rx(*names: str) -> dict:
    return {
        "diagnosis": "Amlapitta (hyperacidity)",
        "advice": "Avoid spicy food; early dinner",
        "items": [
            {
                "medicine_id": medicine_id(n),
                "dose": "1 tsp",
                "frequency": "twice daily",
                "duration_days": 14,
            }
            for n in names
        ],
    }


async def in_progress(client: AsyncClient, admin_token: str, mode: str = "video") -> dict:
    c = await confirmed_consultation(client, admin_token, hours=10 / 60, mode=mode)
    started = await client.post(
        f"/v1/consultations/{c['id']}/start", headers=bearer(c["doctor_token"])
    )
    assert started.status_code == 200, started.text
    return c


async def prescribe(client: AsyncClient, c: dict, body: dict):  # type: ignore[no-untyped-def]
    return await client.post(
        f"/v1/consultations/{c['id']}/prescription",
        json=body,
        headers={**bearer(c["doctor_token"]), **idem()},
    )


async def test_signed_prescription_round_trip(app: FastAPI, client: AsyncClient) -> None:
    _, admin_token = await admin_session(app, client)
    c = await in_progress(client, admin_token)
    resp = await prescribe(
        client, c, rx("Avipattikar Churna", "Triphala Churna", "Yogaraja Guggulu")
    )
    assert resp.status_code == 201, resp.text
    issued = resp.json()

    doc = issued["document"]
    assert doc["doctor"]["registration_number"].startswith("AYU-")  # TPG-mandated fields
    assert doc["patient"]["name"] and doc["issued_at"]
    assert [i["medicine"] for i in doc["items"]] == [
        "Avipattikar Churna",
        "Triphala Churna",
        "Yogaraja Guggulu",
    ]
    assert [m["amrutam_sku"] for m in issued["product_recommendations"]] == ["AMR-TRIPHALA"]

    key = (await client.get("/v1/prescriptions/signing-key")).json()
    assert verify(key["public_key"], doc, issued["signature"])
    tampered = {**doc, "diagnosis": "something else"}
    assert not verify(key["public_key"], tampered, issued["signature"])

    as_patient = (
        await client.get(
            f"/v1/consultations/{c['id']}/prescription", headers=bearer(c["patient_token"])
        )
    ).json()
    assert as_patient["document"] == doc and as_patient["signature"] == issued["signature"]
    assert (await prescribe(client, c, rx("Triphala Churna"))).json()[
        "code"
    ] == "prescription_exists"


async def test_list_b_is_never_allowed(app: FastAPI, client: AsyncClient) -> None:
    _, admin_token = await admin_session(app, client)
    c = await in_progress(client, admin_token)
    resp = await prescribe(client, c, rx("Triphala Churna", "Agnitundi Vati"))
    assert resp.status_code == 422 and resp.json()["code"] == "medicine_not_permitted"
    assert "Agnitundi Vati" in resp.json()["detail"]


async def test_list_a_needs_video_first_then_refills_allowed(
    app: FastAPI, client: AsyncClient
) -> None:
    _, admin_token = await admin_session(app, client)
    audio = await in_progress(client, admin_token, mode="audio")
    first = await prescribe(client, audio, rx("Kaishore Guggulu"))
    assert first.status_code == 422 and "video" in first.json()["detail"]
    assert (
        await prescribe(client, audio, rx("Sitopaladi Churna"))
    ).status_code == 201  # List O fine

    # Same doctor + patient: video consult prescribes it, then an audio follow-up may refill.
    video = await in_progress(client, admin_token, mode="video")
    assert (await prescribe(client, video, rx("Kaishore Guggulu"))).status_code == 201
    slot = await open_slot(client, video["doctor_token"], hours=48, mode="audio")
    follow = (await book(client, video["patient_token"], slot)).json()
    await pay(client, video["patient_token"], follow["payment"]["id"])
    async with app.state.db.sessionmaker() as session, session.begin():
        # Fast-forward the follow-up into progress (its slot is two days out).
        await session.execute(
            text("UPDATE consultations SET status='in_progress' WHERE id = :id"),
            {"id": follow["consultation"]["id"]},
        )
    refill = await prescribe(
        client, {**video, "id": follow["consultation"]["id"]}, rx("Kaishore Guggulu")
    )
    assert refill.status_code == 201, refill.text


async def test_prescribing_rules(app: FastAPI, client: AsyncClient) -> None:
    _, admin_token = await admin_session(app, client)
    not_started = await confirmed_consultation(client, admin_token)
    resp = await prescribe(client, not_started, rx("Triphala Churna"))
    assert resp.status_code == 409 and resp.json()["code"] == "not_prescribable"

    c = await in_progress(client, admin_token)
    other = await confirmed_consultation(client, admin_token)
    stolen = await client.post(
        f"/v1/consultations/{c['id']}/prescription",
        json=rx("Triphala Churna"),
        headers={**bearer(other["doctor_token"]), **idem()},
    )
    assert stolen.status_code == 404
    patient_tries = await client.post(
        f"/v1/consultations/{c['id']}/prescription",
        json=rx("Triphala Churna"),
        headers={**bearer(c["patient_token"]), **idem()},
    )
    assert patient_tries.status_code == 403
    unknown = await prescribe(
        client,
        c,
        {
            **rx(),
            "items": [
                {
                    "medicine_id": "00000000-0000-0000-0000-000000000000",
                    "dose": "1",
                    "frequency": "od",
                    "duration_days": 3,
                }
            ],
        },
    )
    assert unknown.status_code == 404


async def test_prescriptions_are_immutable_in_db(app: FastAPI, client: AsyncClient) -> None:
    _, admin_token = await admin_session(app, client)
    c = await in_progress(client, admin_token)
    assert (await prescribe(client, c, rx("Triphala Churna"))).status_code == 201
    async with app.state.db.sessionmaker() as session:
        with pytest.raises(DBAPIError, match="append-only"):
            await session.execute(text("UPDATE prescriptions SET signature = 'x'"))
        await session.rollback()


async def test_medicine_catalogue_for_doctors(app: FastAPI, client: AsyncClient) -> None:
    _, admin_token = await admin_session(app, client)
    c = await confirmed_consultation(client, admin_token)
    found = (
        await client.get("/v1/medicines", params={"q": "gugg"}, headers=bearer(c["doctor_token"]))
    ).json()
    assert {m["name"] for m in found} == {"Yogaraja Guggulu", "Kaishore Guggulu"}
    b_list = (
        await client.get(
            "/v1/medicines", params={"tpg_list": "B"}, headers=bearer(c["doctor_token"])
        )
    ).json()
    assert len(b_list) == 3
    assert (
        await client.get("/v1/medicines", headers=bearer(c["patient_token"]))
    ).status_code == 403
