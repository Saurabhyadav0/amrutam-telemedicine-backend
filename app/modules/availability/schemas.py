import uuid
from datetime import date, datetime, time
from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.modules.availability.models import ConsultationMode, SlotStatus
from app.modules.users.schemas import StrictModel

MAX_SLOTS_PER_REQUEST = 500
MAX_RULE_DAYS = 31
BOOKING_HORIZON_DAYS = 90


def _valid_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone {value!r}") from exc
    return value


TimeZoneName = Annotated[str, AfterValidator(_valid_timezone)]


class SlotIn(StrictModel):
    start_at: AwareDatetime
    end_at: AwareDatetime

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        return self


class RecurrenceRule(StrictModel):
    """Weekly pattern in the doctor's local time, e.g. Mon-Fri 10:00-13:00 in 30-min slots."""

    start_date: date
    end_date: date
    weekdays: list[Annotated[int, Field(ge=0, le=6)]] = Field(
        min_length=1, max_length=7, description="0 = Monday ... 6 = Sunday"
    )
    day_start: time
    day_end: time
    slot_minutes: Literal[15, 20, 30, 45, 60]
    timezone: TimeZoneName = "Asia/Kolkata"

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        if (self.end_date - self.start_date).days >= MAX_RULE_DAYS:
            raise ValueError(f"a rule may span at most {MAX_RULE_DAYS} days")
        if self.day_end <= self.day_start:
            raise ValueError("day_end must be after day_start")
        return self


class SlotBatchIn(StrictModel):
    """Exactly one of ``rule`` or ``slots``."""

    mode: ConsultationMode = ConsultationMode.VIDEO
    rule: RecurrenceRule | None = None
    slots: list[SlotIn] | None = Field(default=None, max_length=MAX_SLOTS_PER_REQUEST)

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        if (self.rule is None) == (self.slots is None):
            raise ValueError("provide exactly one of 'rule' or 'slots'")
        return self


class SlotBatchOut(BaseModel):
    created: int
    skipped_overlapping: int
    slots: list["SlotOut"]


class SlotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    doctor_id: uuid.UUID
    start_at: datetime
    end_at: datetime
    mode: ConsultationMode
    status: SlotStatus
    version: int


class PublicSlotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    start_at: datetime
    end_at: datetime
    mode: ConsultationMode
