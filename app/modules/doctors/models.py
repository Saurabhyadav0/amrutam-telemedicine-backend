import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin
from app.modules.users.models import str_enum


class VerificationStatus(StrEnum):
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"


# Controlled vocabulary keeps filters index-friendly and typo-free.
AYURVEDA_SPECIALTIES = frozenset(
    {
        "kayachikitsa",  # general medicine
        "panchakarma",
        "skin_hair",
        "digestive_health",
        "womens_health",
        "joint_bone",
        "stress_sleep",
        "diabetes_metabolic",
        "weight_management",
        "respiratory",
        "pediatrics",  # kaumarabhritya
        "sexual_health",
        "lifestyle",
    }
)
LANGUAGES = frozenset({"en", "hi", "mr", "gu", "bn", "ta", "te", "kn", "ml", "pa", "or"})


class Doctor(TimestampMixin, Base):
    """Doctor practice profile. One row per user with the ``doctor`` role.

    Telemedicine Practice Guidelines 2020 require the registration number and qualification
    to be shown to patients, and the platform to verify them before the doctor can consult.
    """

    __tablename__ = "doctors"
    __table_args__ = (
        UniqueConstraint(
            "registration_council", "registration_number", name="uq_doctors_registration"
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    display_name: Mapped[str] = mapped_column(String(120))
    registration_number: Mapped[str] = mapped_column(String(64))
    registration_council: Mapped[str] = mapped_column(String(64))
    qualification: Mapped[str] = mapped_column(String(120))
    years_experience: Mapped[int]
    specialties: Mapped[list[str]] = mapped_column(ARRAY(String(32)))
    languages: Mapped[list[str]] = mapped_column(ARRAY(String(8)))
    fee_paise: Mapped[int]
    bio: Mapped[str | None] = mapped_column(Text)
    verification_status: Mapped[VerificationStatus] = mapped_column(
        str_enum(VerificationStatus), default=VerificationStatus.PENDING
    )
    verified_at: Mapped[datetime | None]
    verified_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    rejection_reason: Mapped[str | None] = mapped_column(String(500))
    # The doctor app's availability toggle: off = hidden from search, no new bookings.
    is_accepting: Mapped[bool] = mapped_column(default=True)
    rating_avg: Mapped[Decimal] = mapped_column(Numeric(3, 2), default=Decimal("0"))
    rating_count: Mapped[int] = mapped_column(default=0)
    # Denormalised for search (see migration 0005); may lag bookings by up to a minute.
    next_available_at: Mapped[datetime | None]
    # Maintained by a DB trigger (name, specialties, bio); never written by the app.
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR, deferred=True)

    @property
    def is_bookable(self) -> bool:
        return self.verification_status == VerificationStatus.VERIFIED and self.is_accepting
