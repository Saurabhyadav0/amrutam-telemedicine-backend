import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin, UUIDPrimaryKeyMixin, utcnow


class UserRole(StrEnum):
    PATIENT = "patient"
    DOCTOR = "doctor"
    ADMIN = "admin"
    SUPPORT = "support"


# Roles that can see other people's health data: MFA is mandatory for them.
PRIVILEGED_ROLES = frozenset({UserRole.DOCTOR, UserRole.ADMIN, UserRole.SUPPORT})
SELF_SIGNUP_ROLES = frozenset({UserRole.PATIENT, UserRole.DOCTOR})


class UserStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


def str_enum(enum_cls: type[StrEnum]) -> Enum:
    return Enum(
        enum_cls,
        native_enum=False,
        length=16,
        values_callable=lambda members: [m.value for m in members],
        validate_strings=True,
    )


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[UserRole] = mapped_column(str_enum(UserRole), index=True)
    status: Mapped[UserStatus] = mapped_column(str_enum(UserStatus), default=UserStatus.ACTIVE)
    mfa_enabled: Mapped[bool] = mapped_column(default=False)
    mfa_secret_enc: Mapped[str | None] = mapped_column(Text)
    # Last accepted TOTP time-step: a code can never be used twice (replay protection).
    mfa_last_used_step: Mapped[int | None] = mapped_column(BigInteger)
    failed_login_attempts: Mapped[int] = mapped_column(default=0)
    locked_until: Mapped[datetime | None]
    last_login_at: Mapped[datetime | None]
    deleted_at: Mapped[datetime | None]

    @property
    def is_active(self) -> bool:
        return self.status == UserStatus.ACTIVE and self.deleted_at is None

    def is_locked(self, now: datetime) -> bool:
        return self.locked_until is not None and self.locked_until > now


class Profile(TimestampMixin, Base):
    __tablename__ = "profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    full_name: Mapped[str] = mapped_column(String(120))
    phone_enc: Mapped[str | None] = mapped_column(Text)
    phone_bidx: Mapped[str | None] = mapped_column(String(64), unique=True)
    date_of_birth_enc: Mapped[str | None] = mapped_column(Text)
    gender: Mapped[str | None] = mapped_column(String(16))
    preferred_language: Mapped[str] = mapped_column(String(16), default="en")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")


class Consent(UUIDPrimaryKeyMixin, Base):
    """Append-only consent ledger (DPDP Act 2023 + Telemedicine Practice Guidelines 2020).

    Data-processing consent and teleconsultation consent are separate purposes and separate rows.
    Withdrawal is a new row with ``granted = false``; rows are never updated.
    """

    __tablename__ = "consents"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(32))
    granted: Mapped[bool]
    ip: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
