from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from app.modules.users.models import Profile
from tests.integration.helpers import bearer, login, register


async def test_profile_update_encrypts_phi_at_rest(app: FastAPI, client: AsyncClient) -> None:
    user = await register(client)
    token = (await login(client, user["email"]))["tokens"]["access_token"]

    resp = await client.patch(
        "/v1/users/me/profile",
        json={"phone": "+919812345678", "date_of_birth": "1990-05-17", "preferred_language": "hi"},
        headers=bearer(token),
    )
    assert resp.status_code == 200, resp.text
    profile = resp.json()["profile"]
    assert profile["phone"] == "+919812345678"
    assert profile["date_of_birth"] == "1990-05-17"

    async with app.state.db.sessionmaker() as session:
        row = await session.scalar(select(Profile).where(Profile.user_id == user["id"]))
    assert row is not None
    assert "9812345678" not in (row.phone_enc or "")
    assert "1990" not in (row.date_of_birth_enc or "")


async def test_phone_must_be_unique(client: AsyncClient) -> None:
    await register(client, phone="+919800000001")
    other = await register(client)
    token = (await login(client, other["email"]))["tokens"]["access_token"]
    resp = await client.patch(
        "/v1/users/me/profile", json={"phone": "+919800000001"}, headers=bearer(token)
    )
    assert resp.status_code == 409


async def test_unknown_profile_fields_rejected(client: AsyncClient) -> None:
    user = await register(client)
    token = (await login(client, user["email"]))["tokens"]["access_token"]
    resp = await client.patch("/v1/users/me/profile", json={"role": "admin"}, headers=bearer(token))
    assert resp.status_code == 422


async def test_future_date_of_birth_rejected(client: AsyncClient) -> None:
    user = await register(client)
    token = (await login(client, user["email"]))["tokens"]["access_token"]
    resp = await client.patch(
        "/v1/users/me/profile", json={"date_of_birth": "2999-01-01"}, headers=bearer(token)
    )
    assert resp.status_code == 422
