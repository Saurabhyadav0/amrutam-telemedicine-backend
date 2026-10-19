import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.deps import FieldCipherDep, RequestContextDep, SessionDep, get_prescription_signer
from app.middleware.idempotency import require_idempotency_key
from app.modules.auth.dependencies import AuthenticatedUser, require_roles
from app.modules.prescriptions.models import MedicineList
from app.modules.prescriptions.schemas import (
    MedicineOut,
    PrescriptionIn,
    PrescriptionOut,
    SigningKeyOut,
)
from app.modules.prescriptions.service import PrescriptionService
from app.modules.prescriptions.signing import PrescriptionSigner
from app.modules.users.models import UserRole

router = APIRouter(tags=["prescriptions"])

Doctor = Annotated[AuthenticatedUser, Depends(require_roles(UserRole.DOCTOR))]
Participant = Annotated[
    AuthenticatedUser,
    Depends(require_roles(UserRole.PATIENT, UserRole.DOCTOR, UserRole.ADMIN, UserRole.SUPPORT)),
]
SignerDep = Annotated[PrescriptionSigner, Depends(get_prescription_signer)]


def get_prescription_service(
    session: SessionDep, cipher: FieldCipherDep, signer: SignerDep
) -> PrescriptionService:
    return PrescriptionService(session, cipher=cipher, signer=signer)


ServiceDep = Annotated[PrescriptionService, Depends(get_prescription_service)]


@router.get("/medicines", response_model=list[MedicineOut])
async def search_medicines(
    _: Doctor,
    service: ServiceDep,
    q: Annotated[str | None, Query(max_length=60)] = None,
    tpg_list: MedicineList | None = None,
) -> list[MedicineOut]:
    return await service.search_medicines(q, tpg_list)


@router.post(
    "/consultations/{consultation_id}/prescription",
    status_code=status.HTTP_201_CREATED,
    response_model=PrescriptionOut,
    dependencies=[Depends(require_idempotency_key)],
)
async def issue_prescription(
    consultation_id: uuid.UUID,
    body: PrescriptionIn,
    doctor: Doctor,
    service: ServiceDep,
    ctx: RequestContextDep,
) -> PrescriptionOut:
    """Enforces TPG 2020 medicine lists: List B never; List A first time only over video."""
    return await service.issue(doctor.user, consultation_id, body, ctx)


@router.get("/consultations/{consultation_id}/prescription", response_model=PrescriptionOut)
async def get_prescription(
    consultation_id: uuid.UUID, actor: Participant, service: ServiceDep, ctx: RequestContextDep
) -> PrescriptionOut:
    return await service.get_for_consultation(actor.user, consultation_id, ctx)


@router.get("/prescriptions/signing-key", response_model=SigningKeyOut)
async def prescription_signing_key(signer: SignerDep) -> SigningKeyOut:
    """Public key for verifying prescription signatures (e.g. by pharmacies)."""
    return SigningKeyOut(key_id=signer.key_id, public_key=signer.public_key_b64)
