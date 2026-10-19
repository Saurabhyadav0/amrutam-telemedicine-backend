import json
import time

import pytest

from app.core.resilience import (
    BreakerState,
    CircuitBreaker,
    CircuitOpenError,
    TransientError,
    backoff_delay,
    retry_async,
)
from app.modules.payments.gateway import InvalidSignature, sign_webhook, verify_webhook

SECRET = "s" * 32


async def test_retry_recovers_from_transient_failures() -> None:
    calls = 0

    async def op() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TransientError("503")
        return "ok"

    assert await retry_async(op, dependency="t", attempts=3, base_delay=0.001) == "ok"
    assert calls == 3


async def test_retry_does_not_retry_permanent_errors() -> None:
    calls = 0

    async def op() -> None:
        nonlocal calls
        calls += 1
        raise ValueError("400 bad request")

    with pytest.raises(ValueError):
        await retry_async(op, dependency="t", attempts=5, base_delay=0.001)
    assert calls == 1


def test_backoff_is_capped_and_jittered() -> None:
    delays = [backoff_delay(10, base=1.0, cap=5.0) for _ in range(200)]
    assert max(delays) <= 5.0 and min(delays) >= 0
    assert len({round(d, 3) for d in delays}) > 50  # spread out, not synchronized


async def test_circuit_breaker_opens_and_half_opens() -> None:
    now = [0.0]
    breaker = CircuitBreaker("dep", failure_threshold=2, reset_timeout=10, clock=lambda: now[0])

    async def failing() -> None:
        raise TransientError("down")

    async def healthy() -> str:
        return "up"

    for _ in range(2):
        with pytest.raises(TransientError):
            await breaker.call(failing)
    assert breaker.state == BreakerState.OPEN
    with pytest.raises(CircuitOpenError):
        await breaker.call(healthy)  # fails fast, dependency not called

    now[0] = 11
    assert breaker.state == BreakerState.HALF_OPEN
    assert await breaker.call(healthy) == "up"
    assert breaker.state == BreakerState.CLOSED


def test_webhook_signature_rules() -> None:
    body = json.dumps({"id": "evt_1"}).encode()
    header = sign_webhook(SECRET, body)
    assert verify_webhook(SECRET, body, header, tolerance_s=300)["id"] == "evt_1"
    for bad_body, bad_header in [
        (body + b" ", header),
        (body, header.replace("v1=", "v1=0")),
        (body, sign_webhook(SECRET, body, int(time.time()) - 301)),
        (body, None),
        (body, "garbage"),
    ]:
        with pytest.raises(InvalidSignature):
            verify_webhook(SECRET, bad_body, bad_header, tolerance_s=300)
