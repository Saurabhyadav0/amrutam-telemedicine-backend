import uuid

import pytest

from app.core.errors import Conflict
from app.modules.consultations.models import (
    ACTIVE_STATUSES,
    TRANSITIONS,
    Consultation,
    ConsultationStatus,
)
from app.modules.consultations.saga import transition


def make(status: ConsultationStatus) -> Consultation:
    return Consultation(
        id=uuid.uuid4(),
        patient_id=uuid.uuid4(),
        doctor_id=uuid.uuid4(),
        slot_id=uuid.uuid4(),
        status=status,
        version=1,
    )


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (ConsultationStatus.PENDING_PAYMENT, ConsultationStatus.CONFIRMED),
        (ConsultationStatus.PENDING_PAYMENT, ConsultationStatus.CANCELLED),
        (ConsultationStatus.CONFIRMED, ConsultationStatus.IN_PROGRESS),
        (ConsultationStatus.CONFIRMED, ConsultationStatus.NO_SHOW),
        (ConsultationStatus.CONFIRMED, ConsultationStatus.CANCELLED),
        (ConsultationStatus.IN_PROGRESS, ConsultationStatus.COMPLETED),
    ],
)
def test_legal_transition_bumps_version(
    start: ConsultationStatus, target: ConsultationStatus
) -> None:
    c = make(start)
    transition(c, target)
    assert c.status == target
    assert c.version == 2


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (ConsultationStatus.PENDING_PAYMENT, ConsultationStatus.IN_PROGRESS),  # must pay first
        (ConsultationStatus.PENDING_PAYMENT, ConsultationStatus.COMPLETED),
        (ConsultationStatus.CONFIRMED, ConsultationStatus.COMPLETED),  # must start first
        (ConsultationStatus.COMPLETED, ConsultationStatus.CANCELLED),  # terminal
        (ConsultationStatus.CANCELLED, ConsultationStatus.CONFIRMED),  # terminal
        (ConsultationStatus.NO_SHOW, ConsultationStatus.IN_PROGRESS),  # terminal
    ],
)
def test_illegal_transition_is_conflict_and_leaves_state(
    start: ConsultationStatus, target: ConsultationStatus
) -> None:
    c = make(start)
    with pytest.raises(Conflict) as exc:
        transition(c, target)
    assert exc.value.code == "invalid_transition"
    assert c.status == start  # unchanged
    assert c.version == 1


def test_terminal_states_have_no_exits() -> None:
    for terminal in (
        ConsultationStatus.COMPLETED,
        ConsultationStatus.CANCELLED,
        ConsultationStatus.NO_SHOW,
    ):
        assert TRANSITIONS[terminal] == frozenset()


def test_active_statuses_are_exactly_the_non_terminal_ones() -> None:
    assert (
        frozenset(
            {
                ConsultationStatus.PENDING_PAYMENT,
                ConsultationStatus.CONFIRMED,
                ConsultationStatus.IN_PROGRESS,
            }
        )
        == ACTIVE_STATUSES
    )
