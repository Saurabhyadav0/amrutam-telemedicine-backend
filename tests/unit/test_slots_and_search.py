from datetime import UTC, date, datetime, time, timedelta

import pytest

from app.core.errors import BadRequest
from app.modules.availability.schemas import RecurrenceRule
from app.modules.availability.service import expand_rule, validate_windows
from app.modules.doctors.repository import prefix_tsquery


def test_rule_keeps_local_wall_clock_across_dst() -> None:
    # Europe/London moves clocks forward on 29 March 2026.
    windows = list(
        expand_rule(
            RecurrenceRule(
                start_date=date(2026, 3, 27),
                end_date=date(2026, 3, 30),
                weekdays=[4, 0],
                day_start=time(10),
                day_end=time(11),
                slot_minutes=60,
                timezone="Europe/London",
            )
        )
    )
    assert [w[0].hour for w in windows] == [10, 9]  # 10:00 GMT, then 10:00 BST = 09:00 UTC


def test_rule_slot_count_and_boundaries() -> None:
    windows = list(
        expand_rule(
            RecurrenceRule(
                start_date=date(2026, 11, 2),
                end_date=date(2026, 11, 2),
                weekdays=[0],
                day_start=time(10),
                day_end=time(11, 50),
                slot_minutes=20,
            )
        )
    )
    assert len(windows) == 5  # 10:00..11:40; 11:40-12:00 would overrun day_end


def test_validate_windows_rejects_too_many() -> None:
    now = datetime.now(UTC)
    windows = [
        (now + timedelta(hours=1, minutes=15 * i), now + timedelta(hours=1, minutes=15 * i + 15))
        for i in range(501)
    ]
    with pytest.raises(BadRequest):
        validate_windows(windows, now)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("Priya Sharma", "priya:* & sharma:*"), ("':* | !", None), ("a&b", "a:* & b:*"), ("", None)],
)
def test_prefix_tsquery_strips_operators(text: str, expected: str | None) -> None:
    assert prefix_tsquery(text) == expected
