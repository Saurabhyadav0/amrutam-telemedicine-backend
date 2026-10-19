import base64
import binascii
from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from environment variables (12-factor). Secrets have no defaults."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "amrutam-telemed"
    environment: Literal["local", "test", "staging", "production"] = "local"
    log_level: str = "INFO"
    json_logs: bool = True

    database_url: str = "postgresql+asyncpg://amrutam:amrutam@localhost:5432/amrutam"
    # Optional read replica for search/listing traffic; falls back to the primary.
    database_read_url: str | None = None
    db_pool_size: int = 20
    db_max_overflow: int = 10
    redis_url: str = "redis://localhost:6379/0"

    # {kid: secret}. The active kid signs; every kid still verifies, so keys rotate without logouts.
    jwt_keys: dict[str, str]
    jwt_active_kid: str
    jwt_issuer: str = "amrutam-telemed"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 30
    mfa_token_ttl_seconds: int = 300

    # {version: base64(32 bytes)} for AES-256-GCM fields, plus an HMAC key for blind indexes.
    data_encryption_keys: dict[str, str]
    data_active_key_version: str
    blind_index_key: str

    cors_origins: list[str] = Field(default_factory=list)
    otel_exporter_otlp_endpoint: str | None = None
    max_request_body_bytes: int = 1_048_576

    # Payments: shared secret for gateway webhooks (HMAC-SHA256 over "timestamp.body").
    payment_webhook_secret: str
    payment_webhook_tolerance_seconds: int = 300
    mockpay_failure_rate: float = Field(default=0.0, ge=0, le=1)  # chaos testing knob

    # Ed25519 seed (base64, 32 bytes) used to sign e-prescriptions; public key is published.
    prescription_signing_key: str
    prescription_signing_key_id: str = "rx1"

    slot_hold_seconds: int = 600
    booking_min_lead_minutes: int = 5
    patient_refund_cutoff_hours: int = 2
    join_window_minutes: int = 15
    worker_metrics_port: int = 9100

    login_max_failures: int = 5
    login_lockout_seconds: int = 900
    idempotency_ttl_seconds: int = 60 * 60 * 24
    idempotency_lock_seconds: int = 60

    @model_validator(mode="after")
    def _validate_keys(self) -> Self:
        if self.jwt_active_kid not in self.jwt_keys:
            raise ValueError("JWT_ACTIVE_KID must be one of JWT_KEYS")
        for kid, secret in self.jwt_keys.items():
            if len(secret.encode()) < 32:
                raise ValueError(f"JWT key {kid!r} must be at least 32 bytes")
        if self.data_active_key_version not in self.data_encryption_keys:
            raise ValueError("DATA_ACTIVE_KEY_VERSION must be one of DATA_ENCRYPTION_KEYS")
        for version, key in self.data_encryption_keys.items():
            if "." in version:
                raise ValueError("Encryption key versions must not contain '.'")
            if len(_b64(key, f"data key {version!r}")) != 32:
                raise ValueError(f"Data key {version!r} must decode to 32 bytes")
        if len(_b64(self.prescription_signing_key, "PRESCRIPTION_SIGNING_KEY")) != 32:
            raise ValueError("PRESCRIPTION_SIGNING_KEY must decode to 32 bytes")
        if len(self.payment_webhook_secret.encode()) < 32:
            raise ValueError("PAYMENT_WEBHOOK_SECRET must be at least 32 bytes")
        if len(_b64(self.blind_index_key, "BLIND_INDEX_KEY")) < 32:
            raise ValueError("BLIND_INDEX_KEY must decode to at least 32 bytes")
        return self


def _b64(value: str, name: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{name} must be valid base64") from exc


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # populated from the environment
