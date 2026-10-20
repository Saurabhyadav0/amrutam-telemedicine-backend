import uuid
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import FieldCipher
from app.core.deps import RequestContext
from app.core.errors import Conflict, NotFound
from app.modules.audit.service import AuditService
from app.modules.auth.service import phone_aad
from app.modules.users.models import Profile, User, UserRole
from app.modules.users.repository import UserRepository
from app.modules.users.schemas import MeOut, ProfileOut, ProfileUpdate, UserOut, UserPage


def dob_aad(user_id: uuid.UUID) -> str:
    return f"profiles.date_of_birth:{user_id}"


class UserService:
    def __init__(self, session: AsyncSession, cipher: FieldCipher) -> None:
        self._session = session
        self._cipher = cipher
        self._users = UserRepository(session)
        self._audit = AuditService(session)

    async def get_me(self, user: User) -> MeOut:
        profile = await self._users.get_profile(user.id)
        return MeOut(
            user=UserOut.model_validate(user),
            profile=self._profile_out(profile) if profile else None,
        )

    async def update_profile(
        self, user: User, changes: ProfileUpdate, ctx: RequestContext
    ) -> MeOut:
        profile = await self._users.get_profile(user.id)
        if profile is None:
            raise NotFound("Profile not found")

        fields = changes.model_dump(exclude_unset=True)
        if "phone" in fields:
            phone = fields.pop("phone")
            bidx = self._cipher.blind_index(phone) if phone else None
            if bidx and await self._users.phone_taken(bidx, exclude_user=user.id):
                raise Conflict("This phone number is already registered", code="phone_taken")
            profile.phone_bidx = bidx
            profile.phone_enc = (
                self._cipher.encrypt(phone, aad=phone_aad(user.id)) if phone else None
            )
        if "date_of_birth" in fields:
            dob: date | None = fields.pop("date_of_birth")
            profile.date_of_birth_enc = (
                self._cipher.encrypt(dob.isoformat(), aad=dob_aad(user.id)) if dob else None
            )
        for name, value in fields.items():
            if value is not None:  # full_name etc. are non-nullable
                setattr(profile, name, value)

        await self._audit.record(
            "profile.updated",
            ctx=ctx,
            actor_id=user.id,
            actor_role=user.role.value,
            resource_type="profile",
            resource_id=user.id,
            # Field names only: the audit trail must not become a second copy of the PHI.
            metadata={"fields": sorted(changes.model_fields_set)},
        )
        await self._session.commit()
        return await self.get_me(user)

    async def list_users(
        self, *, role: UserRole | None, cursor: datetime | None, limit: int
    ) -> UserPage:
        users = await self._users.list(role=role, created_before=cursor, limit=limit + 1)
        has_more = len(users) > limit
        users = users[:limit]
        return UserPage(
            items=[UserOut.model_validate(u) for u in users],
            next_cursor=users[-1].created_at if has_more else None,
        )

    async def change_role(
        self, actor: User, target_id: uuid.UUID, role: UserRole, ctx: RequestContext
    ) -> UserOut:
        if actor.id == target_id:
            raise Conflict("Admins cannot change their own role", code="self_role_change")
        target = await self._users.get(target_id, for_update=True)
        if target is None:
            raise NotFound("User not found")
        previous = target.role
        target.role = role
        await self._audit.record(
            "user.role_changed",
            ctx=ctx,
            actor_id=actor.id,
            actor_role=actor.role.value,
            resource_type="user",
            resource_id=target.id,
            metadata={"from": previous.value, "to": role.value},
        )
        await self._session.commit()
        return UserOut.model_validate(target)

    def _profile_out(self, profile: Profile) -> ProfileOut:
        dob = (
            date.fromisoformat(
                self._cipher.decrypt(profile.date_of_birth_enc, aad=dob_aad(profile.user_id))
            )
            if profile.date_of_birth_enc
            else None
        )
        return ProfileOut(
            full_name=profile.full_name,
            phone=(
                self._cipher.decrypt(profile.phone_enc, aad=phone_aad(profile.user_id))
                if profile.phone_enc
                else None
            ),
            date_of_birth=dob,
            gender=profile.gender,
            preferred_language=profile.preferred_language,
            timezone=profile.timezone,
        )
