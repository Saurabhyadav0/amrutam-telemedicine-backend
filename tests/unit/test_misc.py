import uuid
from datetime import UTC, datetime

from app.core.logging import REDACTED, redact_processor
from app.middleware.idempotency import is_valid_key
from app.modules.audit.models import AuditLog
from app.modules.audit.service import GENESIS_HASH, compute_hash


def test_log_redaction_is_recursive() -> None:
    event = redact_processor(
        None, "info", {"event": "x", "password": "p", "body": {"phone": "+91", "ok": 1}}
    )
    assert event["password"] == REDACTED
    assert event["body"] == {"phone": REDACTED, "ok": 1}


def test_idempotency_key_format() -> None:
    assert is_valid_key(str(uuid.uuid4()))
    assert not is_valid_key("short")
    assert not is_valid_key("has space in it")
    assert not is_valid_key("x" * 129)


def test_audit_hash_detects_any_field_change() -> None:
    entry = AuditLog(
        id=1,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        actor_id=None,
        actor_role=None,
        action="user.registered",
        resource_type="user",
        resource_id="1",
        ip=None,
        request_id=None,
        metadata_={"role": "patient"},
    )
    original = compute_hash(GENESIS_HASH, entry)
    entry.metadata_ = {"role": "admin"}
    assert compute_hash(GENESIS_HASH, entry) != original
