import hashlib
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import Cache
from app.core.db import utcnow
from app.core.deps import RequestContext
from app.core.errors import Conflict, NotFound
from app.modules.audit.service import AuditService
from app.modules.doctors.models import Doctor, VerificationStatus
from app.modules.doctors.repository import DoctorRepository
from app.modules.doctors.schemas import (
    DoctorAdminPage,
    DoctorCard,
    DoctorOwnOut,
    DoctorProfileIn,
    DoctorPublic,
    DoctorSearchPage,
    DoctorSearchParams,
    VerificationDecision,
)
from app.modules.users.models import User
from app.modules.users.repository import UserRepository

CACHE_NAMESPACE = "doctors"
# Listings tolerate ~30s staleness (e.g. next_available_at); booking always re-checks the DB.
SEARCH_TTL_SECONDS = 30
PROFILE_TTL_SECONDS = 60
# Changing these on a verified doctor sends them back for re-verification.
CREDENTIAL_FIELDS = ("registration_number", "registration_council", "qualification")


def _card(doctor: Doctor, next_available: object) -> dict[str, object]:
    return {
        "id": doctor.user_id,
        "display_name": doctor.display_name,
        "qualification": doctor.qualification,
        "registration_number": doctor.registration_number,
        "registration_council": doctor.registration_council,
        "years_experience": doctor.years_experience,
        "specialties": doctor.specialties,
        "languages": doctor.languages,
        "fee_paise": doctor.fee_paise,
        "rating_avg": float(doctor.rating_avg),
        "rating_count": doctor.rating_count,
        "next_available_at": next_available,
    }


class DoctorService:
    def __init__(self, session: AsyncSession, cache: Cache) -> None:
        self._session = session
        self._cache = cache
        self._doctors = DoctorRepository(session)
        self._audit = AuditService(session)

    # ---- doctor self-service ------------------------------------------------------------

    async def get_own(self, user: User) -> DoctorOwnOut:
        doctor = await self._doctors.get(user.id)
        if doctor is None:
            raise NotFound("Submit your practice profile first", code="doctor_profile_missing")
        return DoctorOwnOut.model_validate(doctor)

    async def upsert_own(
        self, user: User, data: DoctorProfileIn, ctx: RequestContext
    ) -> DoctorOwnOut:
        if await self._doctors.registration_taken(
            data.registration_council, data.registration_number, exclude_user=user.id
        ):
            raise Conflict(
                "This registration number is already linked to another account",
                code="registration_taken",
            )

        doctor = await self._doctors.get(user.id, for_update=True)
        fields = data.model_dump()
        if doctor is None:
            profile = await UserRepository(self._session).get_profile(user.id)
            doctor = Doctor(
                user_id=user.id, display_name=profile.full_name if profile else user.email, **fields
            )
            self._session.add(doctor)
            action, metadata = "doctor.profile_submitted", {}
        else:
            credentials_changed = any(getattr(doctor, f) != fields[f] for f in CREDENTIAL_FIELDS)
            for name, value in fields.items():
                setattr(doctor, name, value)
            if credentials_changed and doctor.verification_status != VerificationStatus.PENDING:
                doctor.verification_status = VerificationStatus.PENDING
                doctor.verified_at = None
                doctor.verified_by = None
            action, metadata = "doctor.profile_updated", {"reverification": credentials_changed}

        await self._audit.record(
            action,
            ctx=ctx,
            actor_id=user.id,
            actor_role=user.role.value,
            resource_type="doctor",
            resource_id=user.id,
            metadata=metadata,
        )
        try:
            await self._session.commit()
        except IntegrityError as exc:  # concurrent claim of the same registration number
            await self._session.rollback()
            raise Conflict(
                "This registration number is already linked to another account",
                code="registration_taken",
            ) from exc
        await self._cache.invalidate(CACHE_NAMESPACE)
        return DoctorOwnOut.model_validate(doctor)

    async def set_accepting(self, user: User, accepting: bool, ctx: RequestContext) -> DoctorOwnOut:
        doctor = await self._doctors.get(user.id, for_update=True)
        if doctor is None:
            raise NotFound("Submit your practice profile first", code="doctor_profile_missing")
        doctor.is_accepting = accepting
        await self._audit.record(
            "doctor.availability_toggled",
            ctx=ctx,
            actor_id=user.id,
            actor_role=user.role.value,
            resource_type="doctor",
            resource_id=user.id,
            metadata={"is_accepting": accepting},
        )
        await self._session.commit()
        await self._cache.invalidate(CACHE_NAMESPACE)
        return DoctorOwnOut.model_validate(doctor)

    # ---- admin verification -------------------------------------------------------------

    async def list_for_review(
        self, status: VerificationStatus | None, *, limit: int, offset: int
    ) -> DoctorAdminPage:
        rows = await self._doctors.list_by_status(status, limit=limit + 1, offset=offset)
        return DoctorAdminPage(
            items=[DoctorOwnOut.model_validate(d) for d in rows[:limit]],
            next_offset=offset + limit if len(rows) > limit else None,
        )

    async def decide_verification(
        self, admin: User, doctor_id: uuid.UUID, decision: VerificationDecision, ctx: RequestContext
    ) -> DoctorOwnOut:
        doctor = await self._doctors.get(doctor_id, for_update=True)
        if doctor is None:
            raise NotFound("Doctor not found")
        previous = doctor.verification_status
        if decision.decision == "approve":
            doctor.verification_status = VerificationStatus.VERIFIED
            doctor.verified_at = utcnow()
            doctor.verified_by = admin.id
            doctor.rejection_reason = None
        else:  # also acts as suspension of a verified doctor
            doctor.verification_status = VerificationStatus.REJECTED
            doctor.verified_at = None
            doctor.verified_by = admin.id
            doctor.rejection_reason = decision.reason
        await self._audit.record(
            "doctor.verified" if decision.decision == "approve" else "doctor.rejected",
            ctx=ctx,
            actor_id=admin.id,
            actor_role=admin.role.value,
            resource_type="doctor",
            resource_id=doctor.user_id,
            metadata={
                "from": previous.value,
                "to": doctor.verification_status.value,
                "registration_number": doctor.registration_number,
                "registration_council": doctor.registration_council,
            },
        )
        await self._session.commit()
        await self._cache.invalidate(CACHE_NAMESPACE)
        return DoctorOwnOut.model_validate(doctor)

    # ---- public discovery (read replica + cache) ----------------------------------------

    async def search(self, params: DoctorSearchParams) -> DoctorSearchPage:
        async def load() -> DoctorSearchPage:
            rows = await self._doctors.search(params, utcnow())
            has_more = len(rows) > params.limit
            return DoctorSearchPage(
                items=[DoctorCard.model_validate(_card(d, n)) for d, n in rows[: params.limit]],
                next_offset=params.offset + params.limit if has_more else None,
            )

        key = hashlib.sha256(params.model_dump_json().encode()).hexdigest()
        return await self._cache.get_or_load(
            CACHE_NAMESPACE, f"search:{key}", DoctorSearchPage, load, ttl_seconds=SEARCH_TTL_SECONDS
        )

    async def get_public(self, doctor_id: uuid.UUID) -> DoctorPublic:
        async def load() -> DoctorPublic:
            found = await self._doctors.get_bookable_with_next_slot(doctor_id, utcnow())
            if found is None:
                raise NotFound("Doctor not found")
            doctor, next_available = found
            return DoctorPublic.model_validate({**_card(doctor, next_available), "bio": doctor.bio})

        return await self._cache.get_or_load(
            CACHE_NAMESPACE,
            f"profile:{doctor_id}",
            DoctorPublic,
            load,
            ttl_seconds=PROFILE_TTL_SECONDS,
        )
