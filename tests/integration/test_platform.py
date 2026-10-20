from httpx import AsyncClient


async def test_health_endpoints(client: AsyncClient) -> None:
    assert (await client.get("/health/live")).json() == {"status": "ok"}
    ready = await client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["checks"] == {"postgres": "ok", "redis": "ok"}


async def test_security_headers_and_request_id(client: AsyncClient) -> None:
    resp = await client.get("/health/live", headers={"X-Request-ID": "trace-me-12345"})
    assert resp.headers["X-Request-ID"] == "trace-me-12345"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Cache-Control"] == "no-store"
    assert "Strict-Transport-Security" in resp.headers


async def test_oversized_body_rejected(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/auth/login", content=b"x" * 1_100_000, headers={"content-type": "application/json"}
    )
    assert resp.status_code == 413


async def test_metrics_and_openapi_exposed(client: AsyncClient) -> None:
    assert "http_request_duration" in (await client.get("/metrics")).text
    spec = (await client.get("/openapi.json")).json()
    assert "/v1/auth/register" in spec["paths"]
