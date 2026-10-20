import pyotp
from fastapi import FastAPI
from httpx import AsyncClient

from app.modules.users.models import UserRole
from tests.integration.helpers import (
    admin_session,
    bearer,
    enrol_mfa,
    login,
    next_totp,
    register,
    set_role,
)


async def test_mfa_login_flow_and_code_replay_blocked(client: AsyncClient) -> None:
    user = await register(client)
    tokens = (await login(client, user["email"]))["tokens"]
    secret, enabled = await enrol_mfa(client, tokens["access_token"])
    assert len(enabled["recovery_codes"]) == 10

    challenge = await login(client, user["email"])
    assert challenge["mfa_required"] is True and challenge["tokens"] is None

    code = next_totp(secret)
    ok = await client.post(
        "/v1/auth/mfa/verify", json={"mfa_token": challenge["mfa_token"], "code": code}
    )
    assert ok.status_code == 200

    replay = await client.post(
        "/v1/auth/mfa/verify", json={"mfa_token": challenge["mfa_token"], "code": code}
    )
    assert replay.status_code == 401


async def test_recovery_code_is_single_use(client: AsyncClient) -> None:
    user = await register(client)
    tokens = (await login(client, user["email"]))["tokens"]
    _, enabled = await enrol_mfa(client, tokens["access_token"])
    recovery = enabled["recovery_codes"][0]

    first = await login(client, user["email"])
    used = await client.post(
        "/v1/auth/mfa/verify", json={"mfa_token": first["mfa_token"], "code": recovery}
    )
    assert used.status_code == 200
    second = await login(client, user["email"])
    reused = await client.post(
        "/v1/auth/mfa/verify", json={"mfa_token": second["mfa_token"], "code": recovery}
    )
    assert reused.status_code == 401


async def test_enabling_mfa_revokes_older_sessions(client: AsyncClient) -> None:
    user = await register(client)
    old = (await login(client, user["email"]))["tokens"]
    await enrol_mfa(client, old["access_token"])
    resp = await client.post("/v1/auth/refresh", json={"refresh_token": old["refresh_token"]})
    assert resp.status_code == 401


async def test_doctor_is_told_to_enrol_mfa(client: AsyncClient) -> None:
    doctor = await register(client, role="doctor")
    body = await login(client, doctor["email"])
    assert body["mfa_enrollment_required"] is True


async def test_patient_cannot_use_admin_routes(client: AsyncClient) -> None:
    user = await register(client)
    tokens = (await login(client, user["email"]))["tokens"]
    resp = await client.get("/v1/admin/users", headers=bearer(tokens["access_token"]))
    assert resp.status_code == 403
    assert resp.json()["code"] == "insufficient_role"


async def test_admin_without_mfa_is_blocked(app: FastAPI, client: AsyncClient) -> None:
    user = await register(client)
    await set_role(app, user["id"], UserRole.ADMIN)
    tokens = (await login(client, user["email"]))["tokens"]
    resp = await client.get("/v1/admin/users", headers=bearer(tokens["access_token"]))
    assert resp.status_code == 403
    assert resp.json()["code"] == "mfa_required"


async def test_role_change_takes_effect_immediately(app: FastAPI, client: AsyncClient) -> None:
    admin, admin_token = await admin_session(app, client)
    assert (await client.get("/v1/admin/users", headers=bearer(admin_token))).status_code == 200

    target = await register(client)
    changed = await client.patch(
        f"/v1/admin/users/{target['id']}/role", json={"role": "doctor"}, headers=bearer(admin_token)
    )
    assert changed.status_code == 200 and changed.json()["role"] == "doctor"

    self_change = await client.patch(
        f"/v1/admin/users/{admin['id']}/role", json={"role": "patient"}, headers=bearer(admin_token)
    )
    assert self_change.status_code == 409

    # Demote the admin directly: the same still-valid token loses access at once.
    await set_role(app, admin["id"], UserRole.PATIENT)
    assert (await client.get("/v1/admin/users", headers=bearer(admin_token))).status_code == 403


async def test_totp_secret_is_valid_base32(client: AsyncClient) -> None:
    user = await register(client)
    tokens = (await login(client, user["email"]))["tokens"]
    setup = await client.post("/v1/auth/mfa/setup", headers=bearer(tokens["access_token"]))
    secret = setup.json()["secret"]
    assert pyotp.TOTP(secret).now().isdigit()
    assert setup.json()["otpauth_uri"].startswith("otpauth://totp/Amrutam")
