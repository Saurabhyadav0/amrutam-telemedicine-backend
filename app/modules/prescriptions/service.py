import json
import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import FieldCipher
from app.core.db import utcnow
from app.core.deps import RequestContext
from app.core.errors import AppError, Conflict, NotFound
from app.modules.audit.service import AuditService
from app.modules.availability.models import ConsultationMode
from app.modules.consultations.models import Consultation, ConsultationStatus
from app.modules.doctors.models import Doctor
from app.modules.outbox.service import emit
from app.modules.prescriptions.models import Medicine, MedicineList, Prescription, PrescriptionItem
from app.modules.prescriptions.schemas import MedicineOut, PrescriptionIn, PrescriptionOut
from app.modules.prescriptions.signing import PrescriptionSigner, canonical
from app.modules.users.models import User, UserRole
from app.modules.users.repository import UserRepository
from app.modules.users.service import dob_aad

# A prescription may be written during the consult or shortly after completing it.
PRESCRIBE_WINDOW = timedelta(hours=72)
STAFF_ROLES = frozenset({UserRole.ADMIN, UserRole.SUPPORT})


class MedicineNotPermitted(AppError):
    status_code = 422
    code = "medicine_not_permitted"
    title = "Unprocessable Content"


def document_aad(prescription_id: uuid.UUID) -> str:
    return f"prescriptions.document:{prescription_id}"


def _age(dob: date | None) -> int | None:
    if dob is None:
        return None
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def build_document(
    *,
    prescription_id: uuid.UUID,
    consultation: Consultation,
    issued_at_iso: str,
    doctor: Doctor,
    patient_name: str | None,
    patient_age: int | None,
    diagnosis: str,
    advice: str | None,
    items: list[tuple[str, str, str, str, int, str | None]],
) -> dict[str, Any]:
    """The signed document. TPG 2020 sample format: practitioner and patient identity, date,
    diagnosis, Rx, advice. Issuing and re-reading both go through here so they stay identical."""
    return {
        "prescription_id": str(prescription_id),
        "consultation_id": str(consultation.id),
        "issued_at": issued_at_iso,
        "mode": consultation.mode.value,
        "doctor": {
            "name": doctor.display_name,
            "qualification": doctor.qualification,
            "registration_number": doctor.registration_number,
            "registration_council": doctor.registration_council,
        },
        "patient": {"id": str(consultation.patient_id), "name": patient_name, "age": patient_age},
        "diagnosis": diagnosis,
        "advice": advice,
        "items": [
            {
                "medicine": name,
                "form": form,
                "dose": dose,
                "frequency": frequency,
                "duration_days": days,
                "instructions": instructions,
            }
            for name, form, dose, frequency, days, instructions in items
        ],
    }


class PrescriptionService:
    def __init__(
        self, session: AsyncSession, *, cipher: FieldCipher, signer: PrescriptionSigner
    ) -> None:
        self._session = session
        self._cipher = cipher
        self._signer = signer
        self._audit = AuditService(session)

    async def search_medicines(
        self, q: str | None, tpg_list: MedicineList | None
    ) -> list[MedicineOut]:
        stmt = select(Medicine).where(Medicine.is_active).order_by(Medicine.name).limit(50)
        if q:
            stmt = stmt.where(Medicine.name.ilike(f"%{q.replace('%', '').replace('_', '')}%"))
        if tpg_list:
            stmt = stmt.where(Medicine.tpg_list == tpg_list)
        return [MedicineOut.model_validate(m) for m in (await self._session.scalars(stmt)).all()]

    async def issue(
        self,
        doctor_user: User,
        consultation_id: uuid.UUID,
        data: PrescriptionIn,
        ctx: RequestContext,
    ) -> PrescriptionOut:
        consultation = await self._session.scalar(
            select(Consultation).where(Consultation.id == consultation_id).with_for_update()
        )
        if consultation is None or consultation.doctor_id != doctor_user.id:
            raise NotFound("Consultation not found")
        self._check_prescribable(consultation)

        medicines = await self._load_medicines({i.medicine_id for i in data.items})
        await self._enforce_tpg_lists(consultation, medicines)

        doctor = await self._session.get(Doctor, consultation.doctor_id)
        users = UserRepository(self._session)
        patient_profile = await users.get_profile(consultation.patient_id)
        if doctor is None:  # pragma: no cover - guaranteed by FK / earlier check
            raise RuntimeError("invariant violated: doctor missing")

        prescription_id = uuid.uuid4()
        issued_at = utcnow()
        dob = (
            date.fromisoformat(
                self._cipher.decrypt(
                    patient_profile.date_of_birth_enc, aad=dob_aad(patient_profile.user_id)
                )
            )
            if patient_profile and patient_profile.date_of_birth_enc
            else None
        )
        document = build_document(
            prescription_id=prescription_id,
            consultation=consultation,
            issued_at_iso=issued_at.isoformat(),
            doctor=doctor,
            patient_name=patient_profile.full_name if patient_profile else None,
            patient_age=_age(dob),
            diagnosis=data.diagnosis,
            advice=data.advice,
            items=[
                (
                    medicines[i.medicine_id].name,
                    medicines[i.medicine_id].form,
                    i.dose,
                    i.frequency,
                    i.duration_days,
                    i.instructions,
                )
                for i in data.items
            ],
        )
        content_hash, signature = self._signer.sign(document)
        prescription = Prescription(
            id=prescription_id,
            consultation_id=consultation.id,
            doctor_id=consultation.doctor_id,
            patient_id=consultation.patient_id,
            document_enc=self._cipher.encrypt(
                canonical(document).decode(), aad=document_aad(prescription_id)
            ),
            issued_at=issued_at,
            content_sha256=content_hash,
            signature=signature,
            signing_key_id=self._signer.key_id,
        )
        self._session.add(prescription)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise Conflict("A prescription was already issued", code="prescription_exists") from exc
        self._session.add_all(
            PrescriptionItem(
                prescription_id=prescription_id,
                medicine_id=i.medicine_id,
                position=pos,
                dose=i.dose,
                frequency=i.frequency,
                duration_days=i.duration_days,
                instructions=i.instructions,
            )
            for pos, i in enumerate(data.items)
        )
        emit(
            self._session,
            "prescription.issued",
            prescription_id,
            {"prescription_id": str(prescription_id), "patient_id": str(consultation.patient_id)},
        )
        await self._audit.record(
            "prescription.issued",
            ctx=ctx,
            actor_id=doctor_user.id,
            actor_role=doctor_user.role.value,
            resource_type="prescription",
            resource_id=prescription_id,
            metadata={
                "consultation_id": str(consultation.id),
                "items": len(data.items),
                "content_sha256": content_hash,
            },
        )
        await self._session.commit()
        return self._out(prescription, document, medicines.values())

    async def get_for_consultation(
        self, actor: User, consultation_id: uuid.UUID, ctx: RequestContext
    ) -> PrescriptionOut:
        prescription = await self._session.scalar(
            select(Prescription).where(Prescription.consultation_id == consultation_id)
        )
        allowed = prescription is not None and (
            actor.role in STAFF_ROLES
            or actor.id in (prescription.patient_id, prescription.doctor_id)
        )
        if not allowed:
            raise NotFound("Prescription not found")
        if prescription is None:  # pragma: no cover - guaranteed by FK / earlier check
            raise RuntimeError("invariant violated: prescription missing")
        if actor.role in STAFF_ROLES:
            await self._audit.record(
                "prescription.viewed_by_staff",
                ctx=ctx,
                actor_id=actor.id,
                actor_role=actor.role.value,
                resource_type="prescription",
                resource_id=prescription.id,
            )
            await self._session.commit()
        document = json.loads(
            self._cipher.decrypt(prescription.document_enc, aad=document_aad(prescription.id))
        )
        medicines = (
            await self._session.scalars(
                select(Medicine)
                .join(PrescriptionItem, PrescriptionItem.medicine_id == Medicine.id)
                .where(PrescriptionItem.prescription_id == prescription.id)
            )
        ).all()
        return self._out(prescription, document, medicines)

    # ---- rules ------------------------------------------------------------------------------

    @staticmethod
    def _check_prescribable(consultation: Consultation) -> None:
        if consultation.status == ConsultationStatus.IN_PROGRESS:
            return
        if (
            consultation.status == ConsultationStatus.COMPLETED
            and consultation.completed_at
            and utcnow() - consultation.completed_at <= PRESCRIBE_WINDOW
        ):
            return
        raise Conflict(
            "Prescriptions can be issued during a consultation or within 72h of completing it",
            code="not_prescribable",
        )

    async def _load_medicines(self, ids: set[uuid.UUID]) -> dict[uuid.UUID, Medicine]:
        rows = (
            await self._session.scalars(
                select(Medicine).where(Medicine.id.in_(ids), Medicine.is_active)
            )
        ).all()
        found = {m.id: m for m in rows}
        missing = ids - found.keys()
        if missing:
            raise NotFound(f"Unknown medicine(s): {', '.join(sorted(map(str, missing)))}")
        return found

    async def _enforce_tpg_lists(
        self, consultation: Consultation, medicines: dict[uuid.UUID, Medicine]
    ) -> None:
        list_b = sorted(m.name for m in medicines.values() if m.tpg_list == MedicineList.B)
        if list_b:
            raise MedicineNotPermitted(
                f"List B medicines need an in-person consultation: {', '.join(list_b)}"
            )
        if consultation.mode == ConsultationMode.VIDEO:
            return
        # List A over audio/chat is only a refill of what this doctor prescribed before.
        for medicine in medicines.values():
            if medicine.tpg_list != MedicineList.A:
                continue
            refill = await self._session.scalar(
                select(
                    exists().where(
                        PrescriptionItem.medicine_id == medicine.id,
                        PrescriptionItem.prescription_id == Prescription.id,
                        Prescription.patient_id == consultation.patient_id,
                        Prescription.doctor_id == consultation.doctor_id,
                    )
                )
            )
            if not refill:
                raise MedicineNotPermitted(
                    f"{medicine.name} (List A) can first be prescribed only after a video "
                    "consultation"
                )

    # ---- rendering --------------------------------------------------------------------------

    def _out(
        self, prescription: Prescription, document: dict[str, Any], medicines: Any
    ) -> PrescriptionOut:
        recommended = {m.id: m for m in medicines if m.amrutam_sku}
        return PrescriptionOut(
            id=prescription.id,
            consultation_id=prescription.consultation_id,
            document=document,
            content_sha256=prescription.content_sha256,
            signature=prescription.signature,
            signing_key_id=prescription.signing_key_id,
            product_recommendations=[MedicineOut.model_validate(m) for m in recommended.values()],
        )
