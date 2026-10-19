import uuid
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.modules.prescriptions.models import MedicineList
from app.modules.users.schemas import StrictModel

Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class MedicineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    form: str
    tpg_list: MedicineList
    amrutam_sku: str | None


class PrescriptionItemIn(StrictModel):
    medicine_id: uuid.UUID
    dose: Short = Field(examples=["1 tsp (3 g)"])
    frequency: Short = Field(examples=["twice daily after meals"])
    duration_days: int = Field(ge=1, le=180)
    instructions: str | None = Field(default=None, max_length=500)


class PrescriptionIn(StrictModel):
    diagnosis: str = Field(min_length=2, max_length=2000)
    advice: str | None = Field(
        default=None, max_length=4000, description="Diet (pathya), lifestyle"
    )
    items: list[PrescriptionItemIn] = Field(min_length=1, max_length=20)


class PrescriptionOut(BaseModel):
    """The signed document plus its signature. ``document`` is exactly what was signed."""

    id: uuid.UUID
    consultation_id: uuid.UUID
    document: dict[str, Any]
    content_sha256: str
    signature: str
    signing_key_id: str
    product_recommendations: list[MedicineOut]


class SigningKeyOut(BaseModel):
    key_id: str
    algorithm: str = "Ed25519"
    public_key: str
