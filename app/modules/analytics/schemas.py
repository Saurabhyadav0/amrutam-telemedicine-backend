import uuid
from datetime import date, datetime

from pydantic import BaseModel

from app.modules.users.schemas import StrictModel


class Overview(BaseModel):
    from_day: date
    to_day: date
    consultations_booked: int
    consultations_completed: int
    consultations_cancelled: int
    consultations_no_show: int
    gross_revenue_paise: int
    refunds_paise: int
    net_revenue_paise: int
    new_patients: int
    new_doctors: int
    cancellation_rate: float | None
    no_show_rate: float | None
    completion_rate: float | None
    average_rating: float | None
    as_of: datetime | None  # oldest refresh among the days in range


class DailyRow(BaseModel):
    day: date
    consultations_booked: int
    consultations_completed: int
    consultations_cancelled: int
    net_revenue_paise: int
    new_patients: int


class DoctorPerformance(BaseModel):
    doctor_id: uuid.UUID
    display_name: str
    rating_avg: float
    slots_published: int
    slots_booked: int
    consultations_completed: int
    consultations_cancelled: int
    revenue_paise: int
    utilization: float | None


class SpecialtyDemand(BaseModel):
    specialty: str
    doctors: int
    slots_published: int
    slots_booked: int
    revenue_paise: int
    utilization: float | None


class RefreshIn(StrictModel):
    from_day: date
    to_day: date


class RefreshAccepted(BaseModel):
    status: str = "queued"
    from_day: date
    to_day: date
