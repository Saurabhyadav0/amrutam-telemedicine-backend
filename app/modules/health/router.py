import asyncio

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/health", tags=["health"])
logger = structlog.get_logger(__name__)

CHECK_TIMEOUT_SECONDS = 2.0


@router.get("/live")
async def live() -> dict[str, str]:
    """Liveness: the process is up. Never checks dependencies (avoids restart storms)."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Readiness: dependencies reachable, so the load balancer may route traffic here."""
    checks: dict[str, str] = {}
    for name, probe in (
        ("postgres", request.app.state.db.ping()),
        ("redis", request.app.state.redis.ping()),
    ):
        try:
            await asyncio.wait_for(probe, CHECK_TIMEOUT_SECONDS)
            checks[name] = "ok"
        except Exception as exc:
            logger.warning("readiness_check_failed", dependency=name, error=type(exc).__name__)
            checks[name] = "unavailable"
    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse(
        {"status": "ok" if healthy else "degraded", "checks": checks},
        status_code=200 if healthy else 503,
    )
