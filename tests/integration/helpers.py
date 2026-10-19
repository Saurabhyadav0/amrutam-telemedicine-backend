import time
import uuid
from typing import Any

import pyotp
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import update

from app.modules.users.models import User, UserRole

PASSWORD = "Correct-Horse-Battery-9"


def unique_email() -> str:
    return f"user-{uuid.uuid4().hex[:12]}@example.com"


def idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def register(
    client: AsyncClient, *, email: str | None = None, role: str = "patient", **extra: Any
) -> dict[str, Any]:
    payload = {
        "email": email or unique_email(),
        "password": PASSWORD,
        "full_name": "Test User",
        "role": role,
        "accept_terms": True,
        **extra,
    }
    resp = await client.post("/v1/auth/register", json=payload, headers=idem())
    assert resp.status_code == 201, resp.text
    return {**resp.json(), "password": PASSWORD}


async def login(client: AsyncClient, email: str, password: str = PASSWORD) -> dict[str, Any]:
    resp = await client.post("/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def set_role(app: FastAPI, user_id: str, role: UserRole) -> None:
    async with app.state.db.sessionmaker() as session, session.begin():
        await session.execute(update(User).where(User.id == uuid.UUID(user_id)).values(role=role))


def next_totp(secret: str) -> str:
    """A code for the next 30s step: always newer than any step already used."""
    return pyotp.TOTP(secret).at(int(time.time()) + 30)


async def enrol_mfa(client: AsyncClient, access_token: str) -> tuple[str, dict[str, Any]]:
    setup = await client.post("/v1/auth/mfa/setup", headers=bearer(access_token))
    assert setup.status_code == 200, setup.text
    secret = setup.json()["secret"]
    enable = await client.post(
        "/v1/auth/mfa/enable",
        json={"code": pyotp.TOTP(secret).now()},
        headers=bearer(access_token),
    )
    assert enable.status_code == 200, enable.text
    return secret, enable.json()


async def admin_session(app: FastAPI, client: AsyncClient) -> tuple[dict[str, Any], str]:
    """An admin with MFA enrolled. Returns (user, mfa-backed access token)."""
    user = await register(client)
    await set_role(app, user["id"], UserRole.ADMIN)
    tokens = (await login(client, user["email"]))["tokens"]
    _, enabled = await enrol_mfa(client, tokens["access_token"])
    return user, enabled["tokens"]["access_token"]


def unique_token() -> str:
    """A searchable, collision-free word to isolate search results between tests."""
    return "zq" + uuid.uuid4().hex[:10]


def practice(**overrides: Any) -> dict[str, Any]:
    return {
        "registration_number": f"AYU-{uuid.uuid4().hex[:8]}",
        "registration_council": "NCISM",
        "qualification": "BAMS, MD (Ayurveda)",
        "years_experience": 8,
        "specialties": ["digestive_health", "panchakarma"],
        "languages": ["en", "hi"],
        "fee_paise": 50000,
        "bio": "Classical Ayurveda practitioner focused on gut health.",
        **overrides,
    }


async def doctor_session(
    client: AsyncClient, *, name: str | None = None
) -> tuple[dict[str, Any], str]:
    """A doctor with MFA enrolled. Returns (user, mfa-backed access token)."""
    user = await register(client, role="doctor", full_name=name or f"Vaidya {unique_token()}")
    tokens = (await login(client, user["email"]))["tokens"]
    _, enabled = await enrol_mfa(client, tokens["access_token"])
    return user, enabled["tokens"]["access_token"]


async def verified_doctor(
    client: AsyncClient, admin_token: str, *, name: str | None = None, **profile: Any
) -> tuple[dict[str, Any], str]:
    user, token = await doctor_session(client, name=name)
    resp = await client.put("/v1/doctors/me", json=practice(**profile), headers=bearer(token))
    assert resp.status_code == 200, resp.text
    approve = await client.post(
        f"/v1/admin/doctors/{user['id']}/verification",
        json={"decision": "approve"},
        headers=bearer(admin_token),
    )
    assert approve.status_code == 200, approve.text
    return user, token


def hours_from_now(hours: float) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC).replace(microsecond=0) + timedelta(hours=hours)).isoformat()


def medicine_id(name: str) -> str:
    """Seeded medicines have deterministic ids (see migration 0003)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"amrutam:medicine:{name}"))


async def patient_token(client: AsyncClient) -> tuple[dict[str, Any], str]:
    user = await register(client)
    return user, (await login(client, user["email"]))["tokens"]["access_token"]


async def open_slot(
    client: AsyncClient,
    doctor_token: str,
    *,
    hours: float = 24,
    minutes: int = 30,
    mode: str = "video",
) -> str:
    resp = await client.post(
        "/v1/doctors/me/slots",
        json={
            "mode": mode,
            "slots": [
                {"start_at": hours_from_now(hours), "end_at": hours_from_now(hours + minutes / 60)}
            ],
        },
        headers={**bearer(doctor_token), **idem()},
    )
    assert resp.status_code == 201 and resp.json()["created"] == 1, resp.text
    return str(resp.json()["slots"][0]["id"])


async def book(client: AsyncClient, token: str, slot_id: str, **extra: Any) -> Any:
    return await client.post(
        "/v1/consultations",
        json={"slot_id": slot_id, "consent_to_teleconsultation": True, **extra},
        headers={**bearer(token), **idem()},
    )


async def pay(client: AsyncClient, token: str, payment_id: str, outcome: str = "succeeded") -> str:
    resp = await client.post(
        f"/v1/payments/mock/{payment_id}/complete", json={"outcome": outcome}, headers=bearer(token)
    )
    assert resp.status_code == 200, resp.text
    return str(resp.json()["status"])


async def confirmed_consultation(
    client: AsyncClient,
    admin_token: str,
    *,
    hours: float = 24,
    mode: str = "video",
    fee: int = 50000,
) -> dict[str, Any]:
    """A paid, confirmed consultation. Returns ids and tokens for both participants."""
    doctor, doctor_tok = await verified_doctor(client, admin_token, fee_paise=fee)
    slot_id = await open_slot(client, doctor_tok, hours=hours, mode=mode)
    patient, patient_tok = await patient_token(client)
    booked = await book(client, patient_tok, slot_id, chief_complaint="Acidity after meals")
    assert booked.status_code == 201, booked.text
    body = booked.json()
    if body["payment"]:
        assert await pay(client, patient_tok, body["payment"]["id"]) == "confirmed"
    return {
        "id": body["consultation"]["id"],
        "slot_id": slot_id,
        "payment": body["payment"],
        "doctor": doctor,
        "doctor_token": doctor_tok,
        "patient": patient,
        "patient_token": patient_tok,
    }
