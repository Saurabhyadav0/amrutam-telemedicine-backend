import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.integration.helpers import admin_session, bearer, login, register


async def test_actions_are_audited_and_chain_verifies(app: FastAPI, client: AsyncClient) -> None:
    _, admin_token = await admin_session(app, client)
    user = await register(client)
    await login(client, user["email"])

    logs = await client.get(
        "/v1/admin/audit-logs", params={"actor_id": user["id"]}, headers=bearer(admin_token)
    )
    assert logs.status_code == 200
    actions = {row["action"] for row in logs.json()}
    assert {"user.registered", "auth.login_succeeded"} <= actions
    assert all("email" not in row["metadata"] for row in logs.json())

    verify = await client.get("/v1/admin/audit-logs/verify", headers=bearer(admin_token))
    assert verify.json()["valid"] is True
    assert verify.json()["checked"] > 0


@pytest.mark.parametrize(
    "statement", ["UPDATE audit_logs SET action = 'x'", "DELETE FROM audit_logs"]
)
async def test_audit_log_is_append_only(app: FastAPI, client: AsyncClient, statement: str) -> None:
    await register(client)
    async with app.state.db.sessionmaker() as session:
        with pytest.raises(DBAPIError, match="append-only"):
            await session.execute(text(statement))
        await session.rollback()


async def test_audit_rows_land_in_monthly_partition(app: FastAPI, client: AsyncClient) -> None:
    await register(client)
    async with app.state.db.sessionmaker() as session:
        partition = await session.scalar(
            text("SELECT tableoid::regclass::text FROM audit_logs ORDER BY id DESC LIMIT 1")
        )
    assert partition is not None and partition.startswith("audit_logs_20")
