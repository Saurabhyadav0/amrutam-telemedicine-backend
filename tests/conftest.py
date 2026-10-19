import base64
import json
import os
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# Static, test-only secrets. Set before any app module reads settings.
os.environ.update(
    {
        "ENVIRONMENT": "test",
        "JSON_LOGS": "false",
        "LOG_LEVEL": "WARNING",
        "JWT_KEYS": json.dumps({"k1": "test-signing-key-k1-0123456789abcdef0123"}),
        "JWT_ACTIVE_KID": "k1",
        "DATA_ENCRYPTION_KEYS": json.dumps({"v1": base64.b64encode(b"a" * 32).decode()}),
        "DATA_ACTIVE_KEY_VERSION": "v1",
        "BLIND_INDEX_KEY": base64.b64encode(b"b" * 32).decode(),
        "PAYMENT_WEBHOOK_SECRET": "test-webhook-secret-0123456789abcdef0123",
        "PRESCRIPTION_SIGNING_KEY": base64.b64encode(b"r" * 32).decode(),
        "OTEL_EXPORTER_OTLP_ENDPOINT": "",
    }
)


@pytest.fixture(scope="session")
def infra() -> Iterator[None]:
    """Real Postgres + Redis: from TEST_* env vars (CI services) or throwaway containers."""
    containers = []
    db_url = os.environ.get("TEST_DATABASE_URL")
    redis_url = os.environ.get("TEST_REDIS_URL")
    if not db_url:
        try:  # testcontainers >= 4.10 moved community modules
            from testcontainers.community.postgres import PostgresContainer
        except ImportError:
            from testcontainers.postgres import PostgresContainer

        pg = PostgresContainer("postgres:16-alpine", driver="asyncpg")
        pg.start()
        containers.append(pg)
        db_url = pg.get_connection_url()
    if not redis_url:
        try:
            from testcontainers.community.redis import RedisContainer
        except ImportError:
            from testcontainers.redis import RedisContainer

        rc = RedisContainer("redis:7-alpine")
        rc.start()
        containers.append(rc)
        redis_url = f"redis://{rc.get_container_host_ip()}:{rc.get_exposed_port(6379)}/0"

    os.environ["DATABASE_URL"] = db_url
    os.environ["REDIS_URL"] = redis_url

    from alembic.config import Config

    from alembic import command

    cfg = Config("alembic.ini")
    cfg.attributes["database_url"] = db_url
    command.upgrade(cfg, "head")
    try:
        yield
    finally:
        for container in containers:
            container.stop()


@pytest.fixture(scope="session")
async def app(infra: None) -> AsyncIterator[FastAPI]:
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    application = create_app()
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    await app.state.redis.flushdb()  # fresh rate-limit counters per test
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
