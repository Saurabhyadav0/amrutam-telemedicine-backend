"""Retry with exponential backoff + full jitter, and a circuit breaker for external calls.

Full jitter (``sleep = random(0, min(cap, base * 2**attempt))``) spreads retries from many
clients so a recovering dependency isn't hit by synchronized waves (AWS Architecture Blog,
"Exponential Backoff and Jitter"). Only retry operations that are idempotent: every gateway
call carries an idempotency key, so a retried refund can never refund twice.
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum

import structlog
from prometheus_client import Counter, Gauge

logger = structlog.get_logger(__name__)

RETRIES = Counter("dependency_retries_total", "Retried external calls", ["dependency"])
BREAKER_STATE = Gauge(
    "circuit_breaker_open", "1 when the breaker is open (calls fail fast)", ["dependency"]
)


class TransientError(Exception):
    """Failure worth retrying: timeouts, 5xx, connection resets."""


class CircuitOpenError(TransientError):
    pass


def backoff_delay(attempt: int, *, base: float, cap: float) -> float:
    return random.uniform(0, min(cap, base * 2**attempt))  # noqa: S311  # nosec B311


async def retry_async[T](
    operation: Callable[[], Awaitable[T]],
    *,
    dependency: str,
    attempts: int = 3,
    base_delay: float = 0.2,
    max_delay: float = 2.0,
    retry_on: tuple[type[BaseException], ...] = (TransientError,),
) -> T:
    for attempt in range(attempts):
        try:
            return await operation()
        except CircuitOpenError:
            raise  # retrying against an open breaker only adds load
        except retry_on as exc:
            if attempt == attempts - 1:
                raise
            delay = backoff_delay(attempt, base=base_delay, cap=max_delay)
            RETRIES.labels(dependency).inc()
            logger.warning(
                "retrying_dependency",
                dependency=dependency,
                attempt=attempt + 1,
                delay_s=round(delay, 3),
                error=type(exc).__name__,
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Opens after ``failure_threshold`` consecutive failures; after ``reset_timeout`` lets
    one trial call through (half-open) and closes again if it succeeds."""

    def __init__(
        self,
        dependency: str,
        *,
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.dependency = dependency
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> BreakerState:
        if self._opened_at is None:
            return BreakerState.CLOSED
        if self._clock() - self._opened_at >= self.reset_timeout:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    async def call[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        if self.state == BreakerState.OPEN:
            raise CircuitOpenError(f"{self.dependency} circuit is open")
        try:
            result = await operation()
        except TransientError:
            self._record_failure()
            raise
        self._failures = 0
        self._opened_at = None
        BREAKER_STATE.labels(self.dependency).set(0)
        return result

    def _record_failure(self) -> None:
        self._failures += 1
        if self.state == BreakerState.HALF_OPEN or self._failures >= self.failure_threshold:
            self._opened_at = self._clock()
            BREAKER_STATE.labels(self.dependency).set(1)
            logger.error("circuit_opened", dependency=self.dependency, failures=self._failures)
