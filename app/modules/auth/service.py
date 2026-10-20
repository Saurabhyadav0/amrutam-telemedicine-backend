import hmac
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import pyotp
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import FieldCipher, sha256_hex
from app.core.db import utcnow
from app.core.deps import RequestContext
from app.core.errors import BadRequest, Conflict, Locked, Unauthorized
from app.core.rate_limit import RateLimiter
from app.core.security import PasswordService, TokenService, new_opaque_token
from app.modules.audit.service import AuditService
from app.modules.auth.models import MfaRecoveryCode, RefreshToken
from app.modules.users.models import PRIVILEGED_ROLES, Consent, Profile, User, UserRole
from app.modules.users.repository import UserRepository

TOTP_ISSUER = "Amrutam"
RECOVERY_CODE_COUNT = 10
DATA_PROCESSING_CONSENT = "data_processing"


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int


@dataclass(frozen=True, slots=True)
class LoginOutcome:
    tokens: TokenPair | None = None
    mfa_token: str | None = None
    mfa_enrollment_required: bool = False


@dataclass(frozen=True, slots=True)
class NewAccount:
    email: str
    password: str
    full_name: str
    role: UserRole
    phone: str | None = None
    consent_version: str | None = None


def mfa_secret_aad(user_id: uuid.UUID) -> str:
    return f"users.mfa_secret:{user_id}"


def phone_aad(user_id: uuid.UUID) -> str:
    return f"profiles.phone:{user_id}"


def normalize_recovery_code(code: str) -> str:
    return code.strip().lower().replace("-", "")


class AuthService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        tokens: TokenService,
        cipher: FieldCipher,
        passwords: PasswordService,
        rate_limiter: RateLimiter,
    ) -> None:
        self._session = session
        self._settings = settings
        self._tokens = tokens
        self._cipher = cipher
        self._passwords = passwords
        self._rate_limiter = rate_limiter
        self._users = UserRepository(session)
        self._audit = AuditService(session)

    # ---- registration -------------------------------------------------------------------

    async def create_account(self, account: NewAccount, ctx: RequestContext | None) -> User:
        email = account.email.lower()
        if await self._users.get_by_email(email):
            raise Conflict("An account with this email already exists", code="email_taken")
        phone_bidx = self._cipher.blind_index(account.phone) if account.phone else None
        if phone_bidx and await self._users.phone_taken(phone_bidx):
            raise Conflict("This phone number is already registered", code="phone_taken")

        user = User(
            id=uuid.uuid4(),
            email=email,
            password_hash=await self._passwords.hash(account.password),
            role=account.role,
        )
        self._session.add(user)
        # Child rows reference users.id and there are no ORM relationships to order the inserts.
        await self._flush_or_conflict()
        self._session.add(
            Profile(
                user_id=user.id,
                full_name=account.full_name,
                phone_enc=(
                    self._cipher.encrypt(account.phone, aad=phone_aad(user.id))
                    if account.phone
                    else None
                ),
                phone_bidx=phone_bidx,
            )
        )
        if account.consent_version:
            self._session.add(
                Consent(
                    user_id=user.id,
                    purpose=DATA_PROCESSING_CONSENT,
                    version=account.consent_version,
                    granted=True,
                    ip=ctx.ip if ctx else None,
                    user_agent=ctx.user_agent if ctx else None,
                )
            )
        await self._audit.record(
            "user.registered",
            ctx=ctx,
            actor_id=user.id,
            actor_role=user.role.value,
            resource_type="user",
            resource_id=user.id,
            metadata={"role": user.role.value},
        )
        await self._flush_or_conflict()
        await self._session.commit()
        return user

    async def _flush_or_conflict(self) -> None:
        try:
            await self._session.flush()
        except IntegrityError as exc:  # lost a race with a concurrent signup
            await self._session.rollback()
            raise Conflict(
                "An account with these details already exists", code="email_taken"
            ) from exc

    # ---- login --------------------------------------------------------------------------

    async def login(self, email: str, password: str, ctx: RequestContext) -> LoginOutcome:
        email = email.lower()
        await self._rate_limiter.hit(
            f"login:email:{sha256_hex(email)}", limit=10, window_seconds=300
        )
        user = await self._users.get_by_email(email, for_update=True)
        if user is None:
            await self._passwords.verify_dummy(password)
            raise Unauthorized("Invalid email or password", code="invalid_credentials")

        self._ensure_not_locked(user)
        if not user.is_active or not await self._passwords.verify(user.password_hash, password):
            await self._register_failure(user, ctx, reason="bad_password")
            raise Unauthorized("Invalid email or password", code="invalid_credentials")

        if self._passwords.needs_rehash(user.password_hash):
            user.password_hash = await self._passwords.hash(password)

        if user.mfa_enabled:
            await self._session.commit()
            return LoginOutcome(mfa_token=self._tokens.create_mfa_token(user.id))

        tokens = await self._complete_login(user, ctx, amr=("pwd",))
        return LoginOutcome(tokens=tokens, mfa_enrollment_required=user.role in PRIVILEGED_ROLES)

    async def verify_mfa(self, mfa_token: str, code: str, ctx: RequestContext) -> TokenPair:
        claims = self._tokens.decode(mfa_token, expected_type="mfa")
        user = await self._users.get(uuid.UUID(claims["sub"]), for_update=True)
        if user is None or not user.is_active or not user.mfa_enabled:
            raise Unauthorized("Invalid or expired token", code="invalid_token")
        self._ensure_not_locked(user)

        method = "totp" if self._verify_totp(user, code) else None
        if method is None and await self._consume_recovery_code(user, code):
            method = "recovery_code"
        if method is None:
            await self._register_failure(user, ctx, reason="bad_mfa_code")
            raise Unauthorized("Invalid verification code", code="invalid_mfa_code")
        return await self._complete_login(user, ctx, amr=("pwd", "mfa"), mfa_method=method)

    # ---- MFA enrolment ------------------------------------------------------------------

    async def start_mfa_setup(self, user: User, ctx: RequestContext) -> tuple[str, str]:
        if user.mfa_enabled:
            raise Conflict("MFA is already enabled", code="mfa_already_enabled")
        secret = pyotp.random_base32()
        user.mfa_secret_enc = self._cipher.encrypt(secret, aad=mfa_secret_aad(user.id))
        user.mfa_last_used_step = None
        await self._audit.record(
            "mfa.setup_started",
            ctx=ctx,
            actor_id=user.id,
            actor_role=user.role.value,
            resource_type="user",
            resource_id=user.id,
        )
        await self._session.commit()
        uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name=TOTP_ISSUER)
        return secret, uri

    async def enable_mfa(
        self, user: User, code: str, ctx: RequestContext
    ) -> tuple[list[str], TokenPair]:
        if user.mfa_enabled:
            raise Conflict("MFA is already enabled", code="mfa_already_enabled")
        if not user.mfa_secret_enc:
            raise Conflict("Call /auth/mfa/setup first", code="mfa_setup_required")
        if not self._verify_totp(user, code):
            raise BadRequest("Invalid verification code", code="invalid_mfa_code")

        user.mfa_enabled = True
        await self._session.execute(
            delete(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user.id)
        )
        codes = [secrets.token_hex(5) for _ in range(RECOVERY_CODE_COUNT)]
        self._session.add_all(
            MfaRecoveryCode(user_id=user.id, code_hash=sha256_hex(code)) for code in codes
        )
        # Sessions created before MFA was enabled are weaker: end them.
        await self._revoke_user_tokens(user.id, reason="mfa_enabled")
        tokens = await self._issue_tokens(user, ctx, amr=("pwd", "mfa"))
        await self._audit.record(
            "mfa.enabled",
            ctx=ctx,
            actor_id=user.id,
            actor_role=user.role.value,
            resource_type="user",
            resource_id=user.id,
        )
        await self._session.commit()
        return [f"{c[:5]}-{c[5:]}" for c in codes], tokens

    # ---- refresh / logout ---------------------------------------------------------------

    async def refresh(self, raw_token: str, ctx: RequestContext) -> TokenPair:
        record = await self._session.scalar(
            select(RefreshToken)
            .where(RefreshToken.token_hash == sha256_hex(raw_token))
            .with_for_update()
        )
        if record is None:
            raise Unauthorized("Invalid refresh token", code="invalid_refresh_token")

        if record.rotated_at is not None or record.revoked_at is not None:
            await self._revoke_family(record.family_id, reason="reuse_detected")
            await self._audit.record(
                "auth.refresh_token_reuse_detected",
                ctx=ctx,
                actor_id=record.user_id,
                resource_type="token_family",
                resource_id=record.family_id,
            )
            await self._session.commit()
            raise Unauthorized("Refresh token was already used", code="refresh_token_reused")

        now = utcnow()
        if record.expires_at <= now:
            raise Unauthorized("Refresh token expired", code="refresh_token_expired")

        user = await self._users.get(record.user_id)
        if user is None or not user.is_active:
            await self._revoke_family(record.family_id, reason="account_inactive")
            await self._session.commit()
            raise Unauthorized("Account is not active", code="account_inactive")

        record.rotated_at = now
        # The family keeps its original expiry: an absolute session lifetime, not a sliding one.
        tokens = await self._issue_tokens(
            user,
            ctx,
            amr=tuple(record.amr.split()),
            family_id=record.family_id,
            expires_at=record.expires_at,
        )
        await self._session.commit()
        return tokens

    async def logout(self, raw_token: str, ctx: RequestContext) -> None:
        record = await self._session.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == sha256_hex(raw_token))
        )
        if record is None:
            return
        await self._revoke_family(record.family_id, reason="logout")
        await self._audit.record(
            "auth.logout",
            ctx=ctx,
            actor_id=record.user_id,
            resource_type="token_family",
            resource_id=record.family_id,
        )
        await self._session.commit()

    # ---- internals ----------------------------------------------------------------------

    def _ensure_not_locked(self, user: User) -> None:
        if user.is_locked(utcnow()):
            raise Locked(
                "Account temporarily locked after repeated failed attempts", code="account_locked"
            )

    async def _register_failure(self, user: User, ctx: RequestContext, *, reason: str) -> None:
        user.failed_login_attempts += 1
        locked = user.failed_login_attempts >= self._settings.login_max_failures
        if locked:
            user.locked_until = utcnow() + timedelta(seconds=self._settings.login_lockout_seconds)
            user.failed_login_attempts = 0
        await self._audit.record(
            "auth.account_locked" if locked else "auth.login_failed",
            ctx=ctx,
            actor_id=user.id,
            actor_role=user.role.value,
            resource_type="user",
            resource_id=user.id,
            metadata={"reason": reason},
        )
        await self._session.commit()

    async def _complete_login(
        self,
        user: User,
        ctx: RequestContext,
        *,
        amr: tuple[str, ...],
        mfa_method: str | None = None,
    ) -> TokenPair:
        user.failed_login_attempts = 0
        user.locked_until = None
        user.last_login_at = utcnow()
        tokens = await self._issue_tokens(user, ctx, amr=amr)
        await self._audit.record(
            "auth.login_succeeded",
            ctx=ctx,
            actor_id=user.id,
            actor_role=user.role.value,
            resource_type="user",
            resource_id=user.id,
            metadata={"amr": list(amr), **({"mfa_method": mfa_method} if mfa_method else {})},
        )
        await self._session.commit()
        return tokens

    async def _issue_tokens(
        self,
        user: User,
        ctx: RequestContext | None,
        *,
        amr: tuple[str, ...],
        family_id: uuid.UUID | None = None,
        expires_at: datetime | None = None,
    ) -> TokenPair:
        raw = new_opaque_token()
        self._session.add(
            RefreshToken(
                user_id=user.id,
                family_id=family_id or uuid.uuid4(),
                token_hash=sha256_hex(raw),
                amr=" ".join(sorted(amr)),
                expires_at=expires_at
                or utcnow() + timedelta(seconds=self._settings.refresh_token_ttl_seconds),
                ip=ctx.ip if ctx else None,
                user_agent=ctx.user_agent if ctx else None,
            )
        )
        return TokenPair(
            access_token=self._tokens.create_access_token(user.id, user.role.value, amr),
            refresh_token=raw,
            expires_in=self._settings.access_token_ttl_seconds,
        )

    def _verify_totp(self, user: User, code: str) -> bool:
        if not user.mfa_secret_enc or len(code) != 6 or not code.isdigit():
            return False
        totp = pyotp.TOTP(self._cipher.decrypt(user.mfa_secret_enc, aad=mfa_secret_aad(user.id)))
        current_step = int(time.time()) // totp.interval
        for step in (current_step - 1, current_step, current_step + 1):  # +-30s clock drift
            if user.mfa_last_used_step is not None and step <= user.mfa_last_used_step:
                continue
            if hmac.compare_digest(totp.at(step * totp.interval), code):
                user.mfa_last_used_step = step
                return True
        return False

    async def _consume_recovery_code(self, user: User, code: str) -> bool:
        result = await self._session.execute(
            update(MfaRecoveryCode)
            .where(
                MfaRecoveryCode.user_id == user.id,
                MfaRecoveryCode.code_hash == sha256_hex(normalize_recovery_code(code)),
                MfaRecoveryCode.used_at.is_(None),
            )
            .values(used_at=utcnow())
            .returning(MfaRecoveryCode.id)
        )
        return result.first() is not None

    async def _revoke_family(self, family_id: uuid.UUID, *, reason: str) -> None:
        await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=utcnow(), revoke_reason=reason)
        )

    async def _revoke_user_tokens(self, user_id: uuid.UUID, *, reason: str) -> None:
        await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=utcnow(), revoke_reason=reason)
        )
