import uuid
from datetime import UTC, datetime
from typing import ClassVar

from sqlalchemy import DateTime, MetaData, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar = {datetime: DateTime(timezone=True)}


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    # Python-side defaults so values are known after flush without a lazy reload (async-safe).
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Database:
    """Owns the connection pools. One instance per process, created in the app lifespan."""

    def __init__(
        self,
        url: str,
        *,
        read_url: str | None = None,
        pool_size: int = 20,
        max_overflow: int = 10,
    ) -> None:
        self.engine: AsyncEngine = self._create_engine(url, pool_size, max_overflow)
        self.read_engine: AsyncEngine = (
            self._create_engine(read_url, pool_size, max_overflow) if read_url else self.engine
        )
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)
        self.read_sessionmaker = async_sessionmaker(self.read_engine, expire_on_commit=False)

    @staticmethod
    def _create_engine(url: str, pool_size: int, max_overflow: int) -> AsyncEngine:
        return create_async_engine(
            url,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args={"server_settings": {"application_name": "amrutam-api"}},
        )

    async def ping(self) -> bool:
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True

    async def dispose(self) -> None:
        await self.engine.dispose()
        if self.read_engine is not self.engine:
            await self.read_engine.dispose()


__all__ = ["AsyncSession", "Base", "Database", "TimestampMixin", "UUIDPrimaryKeyMixin", "utcnow"]
