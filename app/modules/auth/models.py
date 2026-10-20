import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UUIDPrimaryKeyMixin, utcnow


class RefreshToken(UUIDPrimaryKeyMixin, Base):
    """Opaque, rotating refresh tokens. Only the SHA-256 of the token is stored.

    Every rotation stays in the same ``family_id``. Presenting an already-rotated token means it
    was stolen or replayed, so the whole family is revoked (OAuth 2.0 Security BCP, sec. 4.14).
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    family_id: Mapped[uuid.UUID] = mapped_column(index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    amr: Mapped[str] = mapped_column(String(64))  # space-separated auth methods, e.g. "mfa pwd"
    expires_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    rotated_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
    revoke_reason: Mapped[str | None] = mapped_column(String(32))
    ip: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(512))


class MfaRecoveryCode(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "mfa_recovery_codes"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # Codes are 50 bits of randomness, so a fast hash is sufficient (no offline guessing risk).
    code_hash: Mapped[str] = mapped_column(String(64))
    used_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
