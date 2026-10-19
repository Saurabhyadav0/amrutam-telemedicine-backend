import uuid

import pytest

from app.core.config import Settings
from app.core.errors import Unauthorized
from app.core.security import TokenService


def make_settings(keys: dict[str, str], active: str) -> Settings:
    return Settings(
        jwt_keys=keys,
        jwt_active_kid=active,
        data_encryption_keys={"v1": "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE="},
        data_active_key_version="v1",
        blind_index_key="YmJiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmI=",
        payment_webhook_secret="w" * 32,
        prescription_signing_key="cnJycnJycnJycnJycnJycnJycnJycnJycnJycnJycnI=",
    )


K1 = "k1-secret-0123456789abcdef0123456789ab"
K2 = "k2-secret-0123456789abcdef0123456789ab"


def test_access_token_round_trip() -> None:
    svc = TokenService(make_settings({"k1": K1}, "k1"))
    uid = uuid.uuid4()
    claims = svc.decode(svc.create_access_token(uid, "patient", ["pwd"]), expected_type="access")
    assert claims["sub"] == str(uid)
    assert claims["role"] == "patient"
    assert claims["amr"] == ["pwd"]


def test_token_type_is_enforced() -> None:
    svc = TokenService(make_settings({"k1": K1}, "k1"))
    mfa_token = svc.create_mfa_token(uuid.uuid4())
    with pytest.raises(Unauthorized):
        svc.decode(mfa_token, expected_type="access")


def test_key_rotation_keeps_old_tokens_valid() -> None:
    old = TokenService(make_settings({"k1": K1}, "k1")).create_access_token(
        uuid.uuid4(), "patient", []
    )
    rotated = TokenService(make_settings({"k1": K1, "k2": K2}, "k2"))
    assert rotated.decode(old, expected_type="access")["typ"] == "access"
    # After k1 is retired, its tokens stop working.
    retired = TokenService(make_settings({"k2": K2}, "k2"))
    with pytest.raises(Unauthorized):
        retired.decode(old, expected_type="access")


def test_tampered_token_rejected() -> None:
    svc = TokenService(make_settings({"k1": K1}, "k1"))
    token = svc.create_access_token(uuid.uuid4(), "patient", [])
    with pytest.raises(Unauthorized):
        svc.decode(
            token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1], expected_type="access"
        )


def test_short_keys_rejected() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        make_settings({"k1": "short"}, "k1")
