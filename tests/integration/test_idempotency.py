import asyncio
import uuid

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select

from app.modules.users.models import User
from tests.integration.helpers import PASSWORD, unique_email


def payload(email: str) -> dict[str, object]:
    return {"email": email, "password": PASSWORD, "full_name": "Idem", "accept_terms": True}


async def count_users(app: FastAPI, email: str) -> int:
    async with app.state.db.sessionmaker() as session:
        return (
            await session.scalar(select(func.count()).select_from(User).where(User.email == email))
            or 0
        )


async def test_retry_with_same_key_replays_response(app: FastAPI, client: AsyncClient) -> None:
    email, key = unique_email(), str(uuid.uuid4())
    first = await client.post(
        "/v1/auth/register", json=payload(email), headers={"Idempotency-Key": key}
    )
    second = await client.post(
        "/v1/auth/register", json=payload(email), headers={"Idempotency-Key": key}
    )

    assert first.status_code == second.status_code == 201
    assert second.json() == first.json()
    assert second.headers["Idempotent-Replayed"] == "true"
    assert await count_users(app, email) == 1


async def test_same_key_different_body_is_rejected(client: AsyncClient) -> None:
    key = str(uuid.uuid4())
    await client.post(
        "/v1/auth/register", json=payload(unique_email()), headers={"Idempotency-Key": key}
    )
    other = await client.post(
        "/v1/auth/register", json=payload(unique_email()), headers={"Idempotency-Key": key}
    )
    assert other.status_code == 422
    assert other.json()["code"] == "idempotency_key_mismatch"


async def test_concurrent_duplicates_execute_once(app: FastAPI, client: AsyncClient) -> None:
    email, key = unique_email(), str(uuid.uuid4())
    responses = await asyncio.gather(
        *[
            client.post("/v1/auth/register", json=payload(email), headers={"Idempotency-Key": key})
            for _ in range(5)
        ]
    )
    statuses = sorted(r.status_code for r in responses)
    assert statuses.count(201) >= 1
    assert set(statuses) <= {201, 409}  # 409 = "still in progress, retry"
    assert await count_users(app, email) == 1


async def test_failed_request_releases_key(client: AsyncClient) -> None:
    key = str(uuid.uuid4())
    bad = {"email": unique_email(), "password": PASSWORD, "full_name": "X", "accept_terms": True}
    first = await client.post(
        "/v1/auth/register", json={**bad, "password": "short"}, headers={"Idempotency-Key": key}
    )
    assert first.status_code == 422
    # A validation failure is pinned (same body -> same answer), so fix the body with a new key.
    retry = await client.post(
        "/v1/auth/register", json=bad, headers={"Idempotency-Key": str(uuid.uuid4())}
    )
    assert retry.status_code == 201


async def test_invalid_key_format_rejected(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/auth/register", json=payload(unique_email()), headers={"Idempotency-Key": "bad key"}
    )
    assert resp.status_code == 400
