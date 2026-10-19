import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from app.modules.users.models import UserRole, UserStatus

E164_PATTERN = r"^\+[1-9]\d{7,14}$"

Phone = Annotated[str, StringConstraints(strip_whitespace=True, pattern=E164_PATTERN)]
FullName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Gender = Literal["female", "male", "other", "undisclosed"]
Language = Annotated[str, StringConstraints(pattern=r"^[a-z]{2}(-[A-Z]{2})?$")]


def _not_in_future(value: date) -> date:
    if value > date.today():
        raise ValueError("date_of_birth cannot be in the future")
    if value.year < 1900:
        raise ValueError("date_of_birth is not plausible")
    return value


DateOfBirth = Annotated[date, AfterValidator(_not_in_future)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    role: UserRole
    status: UserStatus
    mfa_enabled: bool
    created_at: datetime


class ProfileOut(BaseModel):
    full_name: str
    phone: str | None
    date_of_birth: date | None
    gender: str | None
    preferred_language: str
    timezone: str


class MeOut(BaseModel):
    user: UserOut
    profile: ProfileOut | None


class ProfileUpdate(StrictModel):
    full_name: FullName | None = None
    phone: Phone | None = None
    date_of_birth: DateOfBirth | None = None
    gender: Gender | None = None
    preferred_language: Language | None = None
    timezone: str | None = Field(default=None, max_length=64)


class RoleUpdate(StrictModel):
    role: UserRole


class UserPage(BaseModel):
    items: list[UserOut]
    next_cursor: datetime | None
