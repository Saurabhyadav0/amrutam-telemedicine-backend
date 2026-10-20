"""Imports every ORM model so ``Base.metadata`` is complete (used by Alembic and tests)."""

from app.core.db import Base
from app.middleware.idempotency import IdempotencyKey
from app.modules.audit.models import AuditChainHead, AuditLog
from app.modules.auth.models import MfaRecoveryCode, RefreshToken
from app.modules.availability.models import AvailabilitySlot
from app.modules.consultations.models import Consultation
from app.modules.doctors.models import Doctor
from app.modules.outbox.models import OutboxEvent
from app.modules.payments.models import Payment, PaymentEvent
from app.modules.prescriptions.models import Medicine, Prescription, PrescriptionItem
from app.modules.users.models import Consent, Profile, User

__all__ = [
    "AuditChainHead",
    "AuditLog",
    "AvailabilitySlot",
    "Base",
    "Consent",
    "Consultation",
    "Doctor",
    "IdempotencyKey",
    "Medicine",
    "MfaRecoveryCode",
    "OutboxEvent",
    "Payment",
    "PaymentEvent",
    "Prescription",
    "PrescriptionItem",
    "Profile",
    "RefreshToken",
    "User",
]
