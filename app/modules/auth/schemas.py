from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, EmailStr, Field, StringConstraints

from app.modules.users.schemas import FullName, Phone, StrictModel

# NIST SP 800-63B: favour length over composition rules, and reject known-bad passwords.
_COMMON_PASSWORDS = frozenset(
    {"password1234", "123456789012", "qwertyuiopas", "iloveyou1234", "amrutam12345"}
)


def _password_policy(value: str) -> str:
    if value.lower() in _COMMON_PASSWORDS or len(set(value)) < 5:
        raise ValueError("password is too easy to guess")
    return value


Password = Annotated[
    str, StringConstraints(min_length=12, max_length=128), AfterValidator(_password_policy)
]
OtpCode = Annotated[str, StringConstraints(strip_whitespace=True, min_length=6, max_length=16)]


class RegisterRequest(StrictModel):
    email: EmailStr
    password: Password
    full_name: FullName
    phone: Phone | None = None
    role: Literal["patient", "doctor"] = "patient"
    accept_terms: Literal[True] = Field(
        description="Explicit consent to data processing (DPDP Act 2023); must be true"
    )
    consent_version: Annotated[str, StringConstraints(min_length=1, max_length=32)] = "2026-01"


class LoginRequest(StrictModel):
    email: EmailStr
    password: Annotated[str, StringConstraints(min_length=1, max_length=128)]


class MfaVerifyRequest(StrictModel):
    mfa_token: str
    code: OtpCode = Field(description="6-digit TOTP code or a recovery code")


class MfaCodeRequest(StrictModel):
    code: OtpCode


class RefreshRequest(StrictModel):
    refresh_token: Annotated[str, StringConstraints(min_length=16, max_length=256)]


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - OAuth token type, not a secret
    expires_in: int


class LoginResponse(BaseModel):
    """Either tokens, or ``mfa_required`` with a short-lived ``mfa_token`` for /auth/mfa/verify."""

    mfa_required: bool
    mfa_token: str | None = None
    tokens: TokenResponse | None = None
    mfa_enrollment_required: bool = Field(
        default=False,
        description="Privileged role without MFA: only MFA enrolment endpoints are usable",
    )


class MfaSetupResponse(BaseModel):
    secret: str
    otpauth_uri: str


class MfaEnableResponse(BaseModel):
    recovery_codes: list[str] = Field(description="Shown once. Store them somewhere safe.")
    tokens: TokenResponse
