from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.core.deps import (
    FieldCipherDep,
    PasswordServiceDep,
    RateLimiterDep,
    RequestContextDep,
    SessionDep,
    SettingsDep,
    TokenServiceDep,
)
from app.core.rate_limit import rate_limit
from app.middleware.idempotency import require_idempotency_key
from app.modules.auth.dependencies import CurrentUser
from app.modules.auth.schemas import (
    LoginRequest,
    LoginResponse,
    MfaCodeRequest,
    MfaEnableResponse,
    MfaSetupResponse,
    MfaVerifyRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
)
from app.modules.auth.service import AuthService, NewAccount, TokenPair
from app.modules.users.models import UserRole
from app.modules.users.schemas import UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


def get_auth_service(
    session: SessionDep,
    settings: SettingsDep,
    tokens: TokenServiceDep,
    cipher: FieldCipherDep,
    passwords: PasswordServiceDep,
    rate_limiter: RateLimiterDep,
) -> AuthService:
    return AuthService(
        session,
        settings=settings,
        tokens=tokens,
        cipher=cipher,
        passwords=passwords,
        rate_limiter=rate_limiter,
    )


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


def _token_response(pair: TokenPair) -> TokenResponse:
    return TokenResponse(
        access_token=pair.access_token, refresh_token=pair.refresh_token, expires_in=pair.expires_in
    )


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=UserOut,
    dependencies=[
        Depends(require_idempotency_key),
        Depends(rate_limit("register", limit=10, window_seconds=3600)),
    ],
)
async def register(
    body: RegisterRequest, service: AuthServiceDep, ctx: RequestContextDep
) -> UserOut:
    user = await service.create_account(
        NewAccount(
            email=body.email,
            password=body.password,
            full_name=body.full_name,
            phone=body.phone,
            role=UserRole(body.role),
            consent_version=body.consent_version,
        ),
        ctx,
    )
    return UserOut.model_validate(user)


@router.post(
    "/login",
    response_model=LoginResponse,
    dependencies=[Depends(rate_limit("login", limit=20, window_seconds=60))],
)
async def login(
    body: LoginRequest, service: AuthServiceDep, ctx: RequestContextDep
) -> LoginResponse:
    outcome = await service.login(body.email, body.password, ctx)
    if outcome.mfa_token:
        return LoginResponse(mfa_required=True, mfa_token=outcome.mfa_token)
    if outcome.tokens is None:  # pragma: no cover - service always returns one of the two
        raise RuntimeError("login produced neither tokens nor an MFA challenge")
    return LoginResponse(
        mfa_required=False,
        tokens=_token_response(outcome.tokens),
        mfa_enrollment_required=outcome.mfa_enrollment_required,
    )


@router.post(
    "/mfa/verify",
    response_model=TokenResponse,
    dependencies=[Depends(rate_limit("mfa_verify", limit=10, window_seconds=60))],
)
async def verify_mfa(
    body: MfaVerifyRequest, service: AuthServiceDep, ctx: RequestContextDep
) -> TokenResponse:
    return _token_response(await service.verify_mfa(body.mfa_token, body.code, ctx))


@router.post("/mfa/setup", response_model=MfaSetupResponse)
async def setup_mfa(
    current: CurrentUser, service: AuthServiceDep, ctx: RequestContextDep
) -> MfaSetupResponse:
    secret, uri = await service.start_mfa_setup(current.user, ctx)
    return MfaSetupResponse(secret=secret, otpauth_uri=uri)


@router.post(
    "/mfa/enable",
    response_model=MfaEnableResponse,
    dependencies=[Depends(rate_limit("mfa_enable", limit=10, window_seconds=60))],
)
async def enable_mfa(
    body: MfaCodeRequest, current: CurrentUser, service: AuthServiceDep, ctx: RequestContextDep
) -> MfaEnableResponse:
    codes, pair = await service.enable_mfa(current.user, body.code, ctx)
    return MfaEnableResponse(recovery_codes=codes, tokens=_token_response(pair))


@router.post(
    "/refresh",
    response_model=TokenResponse,
    dependencies=[Depends(rate_limit("refresh", limit=30, window_seconds=60))],
)
async def refresh(
    body: RefreshRequest, service: AuthServiceDep, ctx: RequestContextDep
) -> TokenResponse:
    return _token_response(await service.refresh(body.refresh_token, ctx))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: RefreshRequest, service: AuthServiceDep, ctx: RequestContextDep) -> Response:
    await service.logout(body.refresh_token, ctx)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
