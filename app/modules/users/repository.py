import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.users.models import Profile, User, UserRole


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: uuid.UUID, *, for_update: bool = False) -> User | None:
        stmt = select(User).where(User.id == user_id)
        if for_update:
            stmt = stmt.with_for_update()
        return await self._session.scalar(stmt)

    async def get_by_email(self, email: str, *, for_update: bool = False) -> User | None:
        stmt = select(User).where(User.email == email.lower())
        if for_update:
            stmt = stmt.with_for_update()
        return await self._session.scalar(stmt)

    async def get_profile(self, user_id: uuid.UUID) -> Profile | None:
        return await self._session.get(Profile, user_id)

    async def phone_taken(self, phone_bidx: str, *, exclude_user: uuid.UUID | None = None) -> bool:
        stmt = select(Profile.user_id).where(Profile.phone_bidx == phone_bidx)
        if exclude_user:
            stmt = stmt.where(Profile.user_id != exclude_user)
        return (await self._session.scalar(stmt)) is not None

    async def list(
        self,
        *,
        role: UserRole | None,
        created_before: datetime | None,
        limit: int,
    ) -> list[User]:
        stmt = (
            select(User)
            .where(User.deleted_at.is_(None))
            .order_by(User.created_at.desc(), User.id.desc())
            .limit(limit)
        )
        if role:
            stmt = stmt.where(User.role == role)
        if created_before:
            stmt = stmt.where(User.created_at < created_before)
        return list((await self._session.scalars(stmt)).all())
