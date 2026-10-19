from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import Field

from app.core.deps import FieldCipherDep, PasswordServiceDep, RequestContextDep, SessionDep
from app.core.rate_limit import rate_limit
from app.modules.auth.dependencies import CurrentUser
from app.modules.privacy.service import PrivacyService
from app.modules.users.schemas import StrictModel

router = APIRouter(prefix="/users/me", tags=["privacy (DPDP)"])


def get_privacy_service(
    session: SessionDep, cipher: FieldCipherDep, passwords: PasswordServiceDep
) -> PrivacyService:
    return PrivacyService(session, cipher=cipher, passwords=passwords)


ServiceDep = Annotated[PrivacyService, Depends(get_privacy_service)]


class ErasureIn(StrictModel):
    password: str = Field(min_length=1, max_length=128)
    confirm: Literal["ERASE MY ACCOUNT"]


@router.get(
    "/data-export",
    dependencies=[Depends(rate_limit("data_export", limit=5, window_seconds=3600))],
)
async def export_my_data(
    current: CurrentUser, service: ServiceDep, ctx: RequestContextDep
) -> dict[str, Any]:
    """Right of access: everything held about the caller, decrypted, in one JSON document."""
    return await service.export(current.user, ctx)


@router.post(
    "/erasure",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(rate_limit("erasure", limit=5, window_seconds=3600))],
)
async def erase_my_account(
    body: ErasureIn, current: CurrentUser, service: ServiceDep, ctx: RequestContextDep
) -> Response:
    """Right to erasure. Identity/contact data is removed and the account closed; clinical
    records are retained as medical-records rules require. Requires the password again."""
    await service.erase(current.user, body.password, ctx)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
