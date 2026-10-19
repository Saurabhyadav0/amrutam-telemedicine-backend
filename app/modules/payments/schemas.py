import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.payments.models import PaymentStatus
from app.modules.users.schemas import StrictModel


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    amount_paise: int
    currency: str
    status: PaymentStatus
    provider: str
    provider_payment_id: str | None
    refund_amount_paise: int | None


class WebhookEventData(BaseModel):
    provider_payment_id: str = Field(max_length=64)
    amount_paise: int
    failure_reason: str | None = Field(default=None, max_length=64)


class WebhookEvent(BaseModel):
    id: str = Field(max_length=64)
    type: Literal["payment.succeeded", "payment.failed"]
    data: WebhookEventData


class MockCompleteIn(StrictModel):
    outcome: Literal["succeeded", "failed"] = "succeeded"
