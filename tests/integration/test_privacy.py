from fastapi import FastAPI
from httpx import AsyncClient

from tests.integration.helpers import (
    PASSWORD,
    admin_session,
    bearer,
    confirmed_consultation,
    idem,
    login,
    patient_token,
)

ERASE = {"password": PASSWORD, "confirm": "ERASE MY ACCOUNT"}


async def test_export_contains_decrypted_records(app: FastAPI, client: AsyncClient) -> None:
    _, admin_token = await admin_session(app, client)
    c = await confirmed_consultation(client, admin_token)
    export = (
        await client.get("/v1/users/me/data-export", headers=bearer(c["patient_token"]))
    ).json()
    assert export["account"]["id"] == c["patient"]["id"]
    assert {x["purpose"] for x in export["consents"]} == {"data_processing", "teleconsultation"}
    [consultation] = export["consultations"]
    assert consultation["chief_complaint"] == "Acidity after meals"
    assert consultation["status"] == "confirmed"


async def test_erasure_rules_and_effects(app: FastAPI, client: AsyncClient) -> None:
    _, admin_token = await admin_session(app, client)
    c = await confirmed_consultation(client, admin_token)
    token = c["patient_token"]
    url = "/v1/users/me/erasure"

    assert (
        await client.post(url, json={**ERASE, "password": "wrong-password"}, headers=bearer(token))
    ).status_code == 401
    assert (
        await client.post(url, json={**ERASE, "confirm": "yes"}, headers=bearer(token))
    ).status_code == 422
    blocked = await client.post(url, json=ERASE, headers=bearer(token))
    assert blocked.status_code == 409 and blocked.json()["code"] == "active_consultations"

    await client.post(
        f"/v1/consultations/{c['id']}/cancel",
        json={"reason": "leaving"},
        headers={**bearer(token), **idem()},
    )
    refresh = (await login(client, c["patient"]["email"]))["tokens"]["refresh_token"]
    assert (await client.post(url, json=ERASE, headers=bearer(token))).status_code == 204

    # Account unusable: token rejected, login fails, refresh revoked.
    assert (await client.get("/v1/users/me", headers=bearer(token))).status_code == 401
    assert (
        await client.post(
            "/v1/auth/login", json={"email": c["patient"]["email"], "password": PASSWORD}
        )
    ).status_code == 401
    assert (
        await client.post("/v1/auth/refresh", json={"refresh_token": refresh})
    ).status_code == 401

    # Clinical record retained for the doctor, without the patient's identity.
    seen_by_doctor = await client.get(
        f"/v1/consultations/{c['id']}", headers=bearer(c["doctor_token"])
    )
    assert seen_by_doctor.status_code == 200 and seen_by_doctor.json()["status"] == "cancelled"
    logs = (
        await client.get(
            "/v1/admin/audit-logs",
            params={"action": "privacy.account_erased", "actor_id": c["patient"]["id"]},
            headers=bearer(admin_token),
        )
    ).json()
    assert len(logs) == 1


async def test_doctors_cannot_self_erase(app: FastAPI, client: AsyncClient) -> None:
    _, admin_token = await admin_session(app, client)
    c = await confirmed_consultation(client, admin_token)
    resp = await client.post("/v1/users/me/erasure", json=ERASE, headers=bearer(c["doctor_token"]))
    assert resp.status_code == 403 and resp.json()["code"] == "erasure_requires_support"


async def test_new_patient_can_erase_immediately(client: AsyncClient) -> None:
    _, token = await patient_token(client)
    assert (
        await client.post("/v1/users/me/erasure", json=ERASE, headers=bearer(token))
    ).status_code == 204
