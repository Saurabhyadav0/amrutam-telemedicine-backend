import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from app.modules.doctors.models import AYURVEDA_SPECIALTIES, LANGUAGES, VerificationStatus
from app.modules.users.schemas import StrictModel


def _known(vocabulary: frozenset[str], label: str) -> AfterValidator:
    def check(values: list[str]) -> list[str]:
        unknown = sorted(set(values) - vocabulary)
        if unknown:
            raise ValueError(f"unknown {label}: {', '.join(unknown)}")
        return sorted(set(values))

    return AfterValidator(check)


Specialties = Annotated[
    list[str], Field(min_length=1, max_length=8), _known(AYURVEDA_SPECIALTIES, "specialty")
]
Languages = Annotated[list[str], Field(min_length=1, max_length=8), _known(LANGUAGES, "language")]


def _upper(value: object) -> object:
    # Normalise before pattern checks so "ayu-123" and "AYU-123" are the same registration.
    return value.strip().upper() if isinstance(value, str) else value


RegistrationNumber = Annotated[
    str, BeforeValidator(_upper), StringConstraints(pattern=r"^[A-Z0-9/\-]{3,64}$")
]


class DoctorProfileIn(StrictModel):
    registration_number: RegistrationNumber
    registration_council: Annotated[
        str,
        BeforeValidator(_upper),
        StringConstraints(min_length=2, max_length=64),
        Field(description="Issuing body, e.g. NCISM or a State Board of Ayurveda"),
    ]
    qualification: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=2, max_length=120)
    ]
    years_experience: int = Field(ge=0, le=70)
    specialties: Specialties
    languages: Languages
    fee_paise: int = Field(ge=0, le=10_000_000, description="Consultation fee in paise (INR)")
    bio: str | None = Field(default=None, max_length=2000)


class DoctorOwnOut(BaseModel):
    """The doctor's view of their own profile, including verification state."""

    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    display_name: str
    registration_number: str
    registration_council: str
    qualification: str
    years_experience: int
    specialties: list[str]
    languages: list[str]
    fee_paise: int
    bio: str | None
    verification_status: VerificationStatus
    rejection_reason: str | None
    is_accepting: bool
    rating_avg: float
    rating_count: int


class AcceptingUpdate(StrictModel):
    is_accepting: bool


class VerificationDecision(StrictModel):
    decision: Literal["approve", "reject"]
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _reason_for_rejection(self) -> Self:
        if self.decision == "reject" and not (self.reason and self.reason.strip()):
            raise ValueError("reason is required when rejecting")
        return self


class DoctorCard(BaseModel):
    """Search result. Shows what TPG 2020 requires patients to see before consulting."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    display_name: str
    qualification: str
    registration_number: str
    registration_council: str
    years_experience: int
    specialties: list[str]
    languages: list[str]
    fee_paise: int
    rating_avg: float
    rating_count: int
    next_available_at: datetime | None


class DoctorPublic(DoctorCard):
    bio: str | None


class DoctorSort(StrEnum):
    RELEVANCE = "relevance"
    RATING = "rating"
    FEE_ASC = "fee_asc"
    FEE_DESC = "fee_desc"
    EXPERIENCE = "experience"
    NEXT_AVAILABLE = "next_available"


class DoctorSearchParams(BaseModel):
    """Normalised search input; also the cache key, so field order/values must be canonical."""

    q: str | None = None
    specialty: str | None = None
    language: str | None = None
    min_fee: int | None = None
    max_fee: int | None = None
    min_rating: float | None = None
    available_from: datetime | None = None
    available_to: datetime | None = None
    sort: DoctorSort = DoctorSort.RELEVANCE
    limit: int = 20
    offset: int = 0


class DoctorSearchPage(BaseModel):
    items: list[DoctorCard]
    next_offset: int | None


class DoctorAdminPage(BaseModel):
    items: list[DoctorOwnOut]
    next_offset: int | None
