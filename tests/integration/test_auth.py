import pytest
from httpx import AsyncClient

from tests.integration.helpers import PASSWORD, bearer, idem, login, register, unique_email


async def test_register_login_and_me(client: AsyncClient) -> None:
    user = await register(client, phone="+919876543210")
    assert user["role"] == "patient"
    assert "password_hash" not in user

    body = await login(client, user["email"])
    assert body["mfa_required"] is False
    assert body["mfa_enrollment_required"] is False

    me = await client.get("/v1/users/me", headers=bearer(body["tokens"]["access_token"]))
    assert me.status_code == 200
    assert me.json()["profile"]["phone"] == "+919876543210"


async def test_email_is_case_insensitive_and_unique(client: AsyncClient) -> None:
    email = unique_email()
    await register(client, email=email)
    resp = await client.post(
        "/v1/auth/register",
        json={"email": email.upper(), "password": PASSWORD, "full_name": "X", "accept_terms": True},
        headers=idem(),
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "email_taken"


async def test_register_requires_consent_and_idempotency_key(client: AsyncClient) -> None:
    payload = {"email": unique_email(), "password": PASSWORD, "full_name": "X"}
    no_consent = await client.post("/v1/auth/register", json=payload, headers=idem())
    assert no_consent.status_code == 422
    no_key = await client.post("/v1/auth/register", json={**payload, "accept_terms": True})
    assert no_key.status_code == 400
    assert no_key.json()["code"] == "idempotency_key_required"


async def test_admin_role_cannot_be_self_assigned(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/auth/register",
        json={
            "email": unique_email(),
            "password": PASSWORD,
            "full_name": "X",
            "accept_terms": True,
            "role": "admin",
        },
        headers=idem(),
    )
    assert resp.status_code == 422


async def test_validation_errors_never_echo_input(client: AsyncClient) -> None:
    secret = "tiny"
    resp = await client.post(
        "/v1/auth/register",
        json={"email": unique_email(), "password": secret, "full_name": "X", "accept_terms": True},
        headers=idem(),
    )
    assert resp.status_code == 422
    assert secret not in resp.text


async def test_wrong_password_and_unknown_email_look_the_same(client: AsyncClient) -> None:
    user = await register(client)
    wrong = await client.post(
        "/v1/auth/login", json={"email": user["email"], "password": "nope-nope-nope"}
    )
    unknown = await client.post(
        "/v1/auth/login", json={"email": unique_email(), "password": "nope-nope-nope"}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["code"] == unknown.json()["code"] == "invalid_credentials"


async def test_account_locks_after_repeated_failures(client: AsyncClient) -> None:
    user = await register(client)
    for _ in range(5):
        await client.post(
            "/v1/auth/login", json={"email": user["email"], "password": "wrong-password!"}
        )
    locked = await client.post(
        "/v1/auth/login", json={"email": user["email"], "password": PASSWORD}
    )
    assert locked.status_code == 423
    assert locked.json()["code"] == "account_locked"


async def test_refresh_rotation_and_reuse_detection(client: AsyncClient) -> None:
    user = await register(client)
    first = (await login(client, user["email"]))["tokens"]

    rotated = await client.post("/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert rotated.status_code == 200
    second = rotated.json()
    assert second["refresh_token"] != first["refresh_token"]

    # Replaying the old token = theft signal: it fails AND kills the whole family.
    replay = await client.post("/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert replay.status_code == 401
    assert replay.json()["code"] == "refresh_token_reused"
    after = await client.post("/v1/auth/refresh", json={"refresh_token": second["refresh_token"]})
    assert after.status_code == 401


async def test_logout_revokes_refresh_token(client: AsyncClient) -> None:
    user = await register(client)
    tokens = (await login(client, user["email"]))["tokens"]
    out = await client.post("/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    assert out.status_code == 204
    again = await client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert again.status_code == 401


@pytest.mark.parametrize("header", [None, "Bearer not-a-jwt", "Basic abc"])
async def test_protected_route_rejects_bad_credentials(
    client: AsyncClient, header: str | None
) -> None:
    headers = {"Authorization": header} if header else {}
    resp = await client.get("/v1/users/me", headers=headers)
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")


async def test_login_rate_limited_per_ip(client: AsyncClient) -> None:
    statuses = [
        (
            await client.post("/v1/auth/login", json={"email": unique_email(), "password": "x"})
        ).status_code
        for _ in range(21)
    ]
    assert statuses[-1] == 429
