"""arq worker: ``arq app.worker.main.WorkerSettings``.

Cron jobs use ``unique=True`` so each tick runs once across all worker replicas; the jobs
themselves use SKIP LOCKED, so even overlapping runs never process a row twice.
"""

from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings
from prometheus_client import start_http_server

from app.core.config import get_settings
from app.core.db import Database
from app.core.deps import get_payment_gateway
from app.core.logging import configure_logging
from app.worker.jobs import (
    WorkerDeps,
    dispatch_outbox,
    ensure_partitions,
    purge_idempotency_keys,
    refresh_analytics,
    refresh_doctor_availability,
    release_expired_holds,
)
from app.worker.notifications import LogNotifier


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.json_logs)
    start_http_server(settings.worker_metrics_port)
    ctx["deps"] = WorkerDeps(
        db=Database(settings.database_url, pool_size=5, max_overflow=5),
        settings=settings,
        gateway=get_payment_gateway(),
        notifier=LogNotifier(),
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["deps"].db.dispose()


async def outbox_tick(ctx: dict[str, Any]) -> int:
    return await dispatch_outbox(ctx["deps"])


async def holds_tick(ctx: dict[str, Any]) -> int:
    return await release_expired_holds(ctx["deps"])


async def idempotency_purge_tick(ctx: dict[str, Any]) -> int:
    return await purge_idempotency_keys(ctx["deps"])


async def analytics_tick(ctx: dict[str, Any]) -> None:
    await refresh_analytics(ctx["deps"])


async def availability_tick(ctx: dict[str, Any]) -> int:
    return await refresh_doctor_availability(ctx["deps"])


async def partitions_tick(ctx: dict[str, Any]) -> None:
    await ensure_partitions(ctx["deps"])


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    on_startup = startup
    on_shutdown = shutdown
    functions: ClassVar[list[Any]] = []
    cron_jobs: ClassVar[list[Any]] = [
        cron(outbox_tick, second=set(range(0, 60, 2)), unique=True, run_at_startup=True),
        cron(holds_tick, second={0, 30}, unique=True),
        cron(availability_tick, second={45}, unique=True),
        cron(analytics_tick, minute=set(range(0, 60, 10)), second={15}, unique=True),
        cron(idempotency_purge_tick, minute={17}, second={0}, unique=True),
        cron(partitions_tick, hour={3}, minute={0}, second={0}, unique=True, run_at_startup=True),
    ]
    max_jobs = 10
    job_timeout = 120
