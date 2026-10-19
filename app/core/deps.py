"""Dependency-injection providers shared by all modules."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.crypto import FieldCipher
from app.core.db import Database
from app.core.rate_limit import RateLimiter, client_ip
from app.core.security import PasswordService, TokenService

if TYPE_CHECKING:
    from app.modules.payments.gateway import PaymentGateway
    from app.modules.prescriptions.signing import PrescriptionSigner


def get_database(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


async def get_session(
    db: Annotated[Database, Depends(get_database)],
) -> AsyncIterator[AsyncSession]:
    async with db.sessionmaker() as session:
        yield session


async def get_read_session(
    db: Annotated[Database, Depends(get_database)],
) -> AsyncIterator[AsyncSession]:
    async with db.read_sessionmaker() as session:
        yield session


def get_redis(request: Request) -> Redis:
    redis: Redis = request.app.state.redis
    return redis


def get_rate_limiter(redis: Annotated[Redis, Depends(get_redis)]) -> RateLimiter:
    return RateLimiter(redis)


@lru_cache
def get_token_service() -> TokenService:
    return TokenService(get_settings())


@lru_cache
def get_field_cipher() -> FieldCipher:
    settings = get_settings()
    return FieldCipher(
        settings.data_encryption_keys, settings.data_active_key_version, settings.blind_index_key
    )


@lru_cache
def get_password_service() -> PasswordService:
    return PasswordService()


@dataclass(frozen=True, slots=True)
class RequestContext:
    ip: str | None
    user_agent: str | None
    request_id: str | None


def get_request_context(request: Request) -> RequestContext:
    user_agent = request.headers.get("user-agent")
    return RequestContext(
        ip=client_ip(request),
        user_agent=user_agent[:512] if user_agent else None,
        request_id=getattr(request.state, "request_id", None),
    )


SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
ReadSessionDep = Annotated[AsyncSession, Depends(get_read_session)]
RedisDep = Annotated[Redis, Depends(get_redis)]
RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]
TokenServiceDep = Annotated[TokenService, Depends(get_token_service)]
FieldCipherDep = Annotated[FieldCipher, Depends(get_field_cipher)]
PasswordServiceDep = Annotated[PasswordService, Depends(get_password_service)]
RequestContextDep = Annotated[RequestContext, Depends(get_request_context)]


@lru_cache
def get_payment_gateway() -> "PaymentGateway":
    from app.modules.payments.gateway import MockPayGateway, ResilientGateway

    # One process-wide breaker per provider; swap MockPay for a real adapter here.
    return ResilientGateway(MockPayGateway(failure_rate=get_settings().mockpay_failure_rate))


@lru_cache
def get_prescription_signer() -> "PrescriptionSigner":
    from app.modules.prescriptions.signing import PrescriptionSigner

    settings = get_settings()
    return PrescriptionSigner(
        settings.prescription_signing_key, settings.prescription_signing_key_id
    )
