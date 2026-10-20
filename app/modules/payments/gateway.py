"""Payment gateway port + an in-process mock adapter (swap for Razorpay/Stripe in prod).

Every call carries our payment id as the provider idempotency key, so the retries done by
``ResilientGateway`` can never create two intents or two refunds.
"""

import asyncio
import hashlib
import hmac
import json
import random
import time
import uuid
from dataclasses import dataclass
from typing import Protocol

from app.core.resilience import CircuitBreaker, TransientError, retry_async


@dataclass(frozen=True, slots=True)
class PaymentIntent:
    provider_payment_id: str
    checkout_url: str


class PaymentGateway(Protocol):
    provider: str

    async def create_intent(
        self, *, idempotency_key: uuid.UUID, amount_paise: int, currency: str
    ) -> PaymentIntent: ...

    async def refund(
        self, *, idempotency_key: uuid.UUID, provider_payment_id: str, amount_paise: int
    ) -> str: ...


class MockPayGateway:
    """Deterministic ids derived from the idempotency key; optional random failures."""

    provider = "mockpay"

    def __init__(self, *, failure_rate: float = 0.0, latency_s: float = 0.0) -> None:
        self._failure_rate = failure_rate
        self._latency = latency_s

    async def create_intent(
        self, *, idempotency_key: uuid.UUID, amount_paise: int, currency: str
    ) -> PaymentIntent:
        await self._simulate()
        pid = f"mp_{idempotency_key.hex}"
        return PaymentIntent(
            provider_payment_id=pid, checkout_url=f"https://mockpay.test/checkout/{pid}"
        )

    async def refund(
        self, *, idempotency_key: uuid.UUID, provider_payment_id: str, amount_paise: int
    ) -> str:
        await self._simulate()
        return f"mprf_{idempotency_key.hex}"

    async def _simulate(self) -> None:
        if self._latency:
            await asyncio.sleep(self._latency)
        if self._failure_rate and random.random() < self._failure_rate:  # noqa: S311  # nosec B311
            raise TransientError("mockpay: simulated 503")


class ResilientGateway:
    """Wraps any gateway with retry (backoff + jitter) and a circuit breaker."""

    def __init__(self, inner: PaymentGateway, breaker: CircuitBreaker | None = None) -> None:
        self._inner = inner
        self.provider = inner.provider
        self._breaker = breaker or CircuitBreaker(f"payments:{inner.provider}")

    async def create_intent(
        self, *, idempotency_key: uuid.UUID, amount_paise: int, currency: str
    ) -> PaymentIntent:
        return await retry_async(
            lambda: self._breaker.call(
                lambda: self._inner.create_intent(
                    idempotency_key=idempotency_key, amount_paise=amount_paise, currency=currency
                )
            ),
            dependency=self._breaker.dependency,
        )

    async def refund(
        self, *, idempotency_key: uuid.UUID, provider_payment_id: str, amount_paise: int
    ) -> str:
        return await retry_async(
            lambda: self._breaker.call(
                lambda: self._inner.refund(
                    idempotency_key=idempotency_key,
                    provider_payment_id=provider_payment_id,
                    amount_paise=amount_paise,
                )
            ),
            dependency=self._breaker.dependency,
        )


# ---- webhook signatures (Stripe-style: "t=<unix>,v1=<hex hmac of 't.body'>") ---------------


class InvalidSignature(Exception):
    pass


def sign_webhook(secret: str, body: bytes, timestamp: int | None = None) -> str:
    ts = int(time.time()) if timestamp is None else timestamp
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify_webhook(secret: str, body: bytes, header: str | None, *, tolerance_s: int) -> dict:
    if not header:
        raise InvalidSignature("missing signature")
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    try:
        ts = int(parts["t"])
        given = parts["v1"]
    except (KeyError, ValueError) as exc:
        raise InvalidSignature("malformed signature") from exc
    if abs(time.time() - ts) > tolerance_s:  # bounds replay of captured requests
        raise InvalidSignature("timestamp outside tolerance")
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, given):
        raise InvalidSignature("signature mismatch")
    try:
        event: dict = json.loads(body)
    except json.JSONDecodeError as exc:
        raise InvalidSignature("body is not JSON") from exc
    return event
