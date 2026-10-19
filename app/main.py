import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.core.db import Database
from app.core.deps import get_token_service
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.observability import (
    instrument_database,
    monitor_event_loop_lag,
    setup_metrics,
    setup_tracing,
)
from app.middleware.idempotency import IdempotencyMiddleware
from app.middleware.request_context import RequestContextMiddleware
from app.middleware.security_headers import BodySizeLimitMiddleware, SecurityHeadersMiddleware
from app.modules.admin.router import router as admin_router
from app.modules.analytics.router import router as analytics_router
from app.modules.auth.router import router as auth_router
from app.modules.consultations.router import router as consultations_router
from app.modules.doctors.router import admin_router as doctors_admin_router
from app.modules.doctors.router import me_router as doctor_me_router
from app.modules.doctors.router import public_router as doctors_public_router
from app.modules.health.router import router as health_router
from app.modules.payments.router import mock_router as payments_mock_router
from app.modules.payments.router import router as payments_router
from app.modules.prescriptions.router import router as prescriptions_router
from app.modules.privacy.router import router as privacy_router
from app.modules.users.router import router as users_router

API_PREFIX = "/v1"
# Token-issuing endpoints are never cached: a replayed response would hand out live credentials.
IDEMPOTENCY_EXCLUDED = frozenset(
    f"{API_PREFIX}/auth/{p}" for p in ("login", "refresh", "logout", "mfa/verify", "mfa/enable")
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_logs=settings.json_logs)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.db = Database(
            settings.database_url,
            read_url=settings.database_read_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
        )
        app.state.redis = Redis.from_url(
            settings.redis_url, socket_timeout=1.0, socket_connect_timeout=1.0
        )
        instrument_database(app.state.db.engine, settings)
        lag_monitor = asyncio.create_task(monitor_event_loop_lag())
        try:
            yield
        finally:
            lag_monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await lag_monitor
            await app.state.redis.aclose()
            await app.state.db.dispose()

    is_prod = settings.environment == "production"
    app = FastAPI(
        title="Amrutam Telemedicine API",
        version="0.1.0",
        description="Auth, doctor availability, booking, consultations, prescriptions and audit.",
        lifespan=lifespan,
        docs_url=None if is_prod else "/docs",
        redoc_url=None,
    )

    # Starlette runs the last-added middleware first; listed here innermost -> outermost.
    app.add_middleware(
        IdempotencyMiddleware,
        settings=settings,
        token_service=get_token_service(),
        excluded_paths=IDEMPOTENCY_EXCLUDED,
    )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(SecurityHeadersMiddleware)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
            expose_headers=["X-Request-ID", "Idempotent-Replayed", "Retry-After"],
        )
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)
    setup_tracing(app, settings)
    setup_metrics(app)

    api = APIRouter(prefix=API_PREFIX)
    api.include_router(auth_router)
    api.include_router(users_router)
    api.include_router(privacy_router)
    # /doctors/me must be registered before /doctors/{doctor_id}.
    api.include_router(doctor_me_router)
    api.include_router(doctors_public_router)
    api.include_router(consultations_router)
    api.include_router(prescriptions_router)
    api.include_router(payments_router)
    if not is_prod:
        api.include_router(payments_mock_router)
    api.include_router(admin_router)
    api.include_router(doctors_admin_router)
    api.include_router(analytics_router)
    app.include_router(api)
    app.include_router(health_router)
    return app
