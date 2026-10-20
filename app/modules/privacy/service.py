"""DPDP Act 2023 data-principal rights: access (export) and erasure.

Erasure removes identity and contact data but keeps clinical records (consultations, signed
prescriptions, payments, audit trail): medical-record retention rules require them, and the
DPDP Act allows retention where another law demands it. The account becomes unusable and
anonymous; the medical record stays intact and verifiable.
"""

import json
import secrets
from datetime import date
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import FieldCipher
from app.core.db import utcnow
from app.core.deps import RequestContext
from app.core.errors import Conflict, Forbidden, Unauthorized
from app.core.security import PasswordService
from app.modules.audit.service import AuditService
from app.modules.auth.models import MfaRecoveryCode, RefreshToken
from app.modules.auth.service import phone_aad
from app.modules.consultations.models import ACTIVE_STATUSES, Consultation
from app.modules.consultations.service import complaint_aad
from app.modules.doctors.models import Doctor
from app.modules.prescriptions.models import Prescription
from app.modules.prescriptions.service import document_aad
from app.modules.users.models import Consent, Profile, User, UserRole, UserStatus
from app.modules.users.service import dob_aad

ERASED_NAME = "Erased user"


class PrivacyService:
    def __init__(
        self, session: AsyncSession, *, cipher: FieldCipher, passwords: PasswordService
    ) -> None:
        self._session = session
        self._cipher = cipher
        self._passwords = passwords
        self._audit = AuditService(session)

    async def export(self, user: User, ctx: RequestContext) -> dict[str, Any]:
        profile = await self._session.get(Profile, user.id)
        consents = (
            await self._session.scalars(
                select(Consent).where(Consent.user_id == user.id).order_by(Consent.created_at)
            )
        ).all()
        rows = (
            await self._session.execute(
                select(Consultation, Doctor.display_name)
                .join(Doctor, Doctor.user_id == Consultation.doctor_id)
                .where(Consultation.patient_id == user.id)
                .order_by(Consultation.scheduled_start)
            )
        ).all()
        prescriptions = (
            await self._session.scalars(
                select(Prescription).where(Prescription.patient_id == user.id)
            )
        ).all()

        export = {
            "generated_at": utcnow().isoformat(),
            "account": {
                "id": str(user.id),
                "email": user.email,
                "role": user.role.value,
                "created_at": user.created_at.isoformat(),
                "mfa_enabled": user.mfa_enabled,
            },
            "profile": self._profile(profile) if profile else None,
            "consents": [
                {
                    "purpose": c.purpose,
                    "version": c.version,
                    "granted": c.granted,
                    "at": c.created_at.isoformat(),
                }
                for c in consents
            ],
            "consultations": [
                {
                    "id": str(c.id),
                    "doctor": doctor_name,
                    "mode": c.mode.value,
                    "status": c.status.value,
                    "scheduled_start": c.scheduled_start.isoformat(),
                    "fee_paise": c.fee_paise,
                    "rating": c.rating,
                    "chief_complaint": (
                        self._cipher.decrypt(c.chief_complaint_enc, aad=complaint_aad(c.id))
                        if c.chief_complaint_enc
                        else None
                    ),
                }
                for c, doctor_name in rows
            ],
            "prescriptions": [
                {
                    "document": json.loads(
                        self._cipher.decrypt(p.document_enc, aad=document_aad(p.id))
                    ),
                    "signature": p.signature,
                    "signing_key_id": p.signing_key_id,
                }
                for p in prescriptions
            ],
        }
        await self._audit.record(
            "privacy.data_exported",
            ctx=ctx,
            actor_id=user.id,
            actor_role=user.role.value,
            resource_type="user",
            resource_id=user.id,
        )
        await self._session.commit()
        return export

    async def erase(self, user: User, password: str, ctx: RequestContext) -> None:
        # Step-up: a stolen access token alone must not be able to wipe an account.
        if not await self._passwords.verify(user.password_hash, password):
            raise Unauthorized("Password is incorrect", code="invalid_credentials")
        if user.role != UserRole.PATIENT:
            raise Forbidden(
                "Practitioner and staff accounts are closed through support: their records "
                "are referenced by prescriptions and audits",
                code="erasure_requires_support",
            )
        active = await self._session.scalar(
            select(Consultation.id)
            .where(Consultation.patient_id == user.id, Consultation.status.in_(ACTIVE_STATUSES))
            .limit(1)
        )
        if active is not None:
            raise Conflict(
                "Cancel or complete your upcoming consultations first", code="active_consultations"
            )

        now = utcnow()
        locked = await self._session.scalar(
            select(User).where(User.id == user.id).with_for_update()
        )
        if locked is None:  # pragma: no cover - authenticated moments ago
            raise Unauthorized("Account is not active", code="account_inactive")
        user = locked
        user.email = f"erased+{user.id}@erased.invalid"
        user.password_hash = await self._passwords.hash(secrets.token_urlsafe(32))
        user.status = UserStatus.DISABLED
        user.deleted_at = now
        user.mfa_enabled = False
        user.mfa_secret_enc = None
        profile = await self._session.get(Profile, user.id)
        if profile is not None:
            profile.full_name = ERASED_NAME
            profile.phone_enc = profile.phone_bidx = profile.date_of_birth_enc = None
            profile.gender = None
        await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now, revoke_reason="account_erased")
        )
        await self._session.execute(
            update(MfaRecoveryCode)
            .where(MfaRecoveryCode.user_id == user.id, MfaRecoveryCode.used_at.is_(None))
            .values(used_at=now)
        )
        self._session.add(
            Consent(
                user_id=user.id,
                purpose="data_processing",
                version="withdrawn",
                granted=False,
                ip=ctx.ip,
                user_agent=ctx.user_agent,
            )
        )
        await self._audit.record(
            "privacy.account_erased",
            ctx=ctx,
            actor_id=user.id,
            actor_role=user.role.value,
            resource_type="user",
            resource_id=user.id,
            metadata={"retained": ["consultations", "prescriptions", "payments", "audit_logs"]},
        )
        await self._session.commit()

    def _profile(self, profile: Profile) -> dict[str, Any]:
        dob = (
            date.fromisoformat(
                self._cipher.decrypt(profile.date_of_birth_enc, aad=dob_aad(profile.user_id))
            ).isoformat()
            if profile.date_of_birth_enc
            else None
        )
        return {
            "full_name": profile.full_name,
            "phone": (
                self._cipher.decrypt(profile.phone_enc, aad=phone_aad(profile.user_id))
                if profile.phone_enc
                else None
            ),
            "date_of_birth": dob,
            "gender": profile.gender,
            "preferred_language": profile.preferred_language,
        }
