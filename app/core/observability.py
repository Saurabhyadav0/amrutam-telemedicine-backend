"""OpenTelemetry tracing and Prometheus metrics wiring."""

import asyncio

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Histogram
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings

EXCLUDED_URLS = "/metrics,/health/live,/health/ready"
SLO_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 0.75, 1, 2.5, 5)


def setup_tracing(app: FastAPI, settings: Settings) -> None:
    if not settings.otel_exporter_otlp_endpoint:
        return
    resource = Resource.create(
        {"service.name": settings.app_name, "deployment.environment": settings.environment}
    )
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=f"{settings.otel_exporter_otlp_endpoint}/v1/traces")
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app, excluded_urls=EXCLUDED_URLS)
    RedisInstrumentor().instrument()


def instrument_database(engine: AsyncEngine, settings: Settings) -> None:
    if settings.otel_exporter_otlp_endpoint:
        SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)


def setup_metrics(app: FastAPI) -> None:
    # Histograms per route template (not raw path) keep label cardinality bounded.
    Instrumentator(
        should_group_status_codes=False,
        excluded_handlers=["/metrics", "/health/live", "/health/ready"],
    ).instrument(
        app,
        # Per-route histogram: needs bucket edges at the SLO thresholds (200ms reads, 500ms
        # writes) or p95 can't be judged against them. The library default is (0.1, 0.5, 1).
        latency_lowr_buckets=SLO_BUCKETS,
        latency_highr_buckets=SLO_BUCKETS,
    ).expose(app, endpoint="/metrics", include_in_schema=False)


EVENT_LOOP_LAG = Histogram(
    "event_loop_lag_seconds",
    "How late a 250ms timer fires: time requests spend queued behind other work on the loop",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1),
)


async def monitor_event_loop_lag(interval: float = 0.25) -> None:
    """Background task: a saturated (or blocked) event loop shows up here before it shows up
    as mysterious latency in every route at once."""
    loop = asyncio.get_running_loop()
    while True:
        start = loop.time()
        await asyncio.sleep(interval)
        EVENT_LOOP_LAG.observe(max(0.0, loop.time() - start - interval))
