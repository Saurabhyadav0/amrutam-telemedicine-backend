from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.deps import FieldCipherDep, RequestContextDep, SessionDep
from app.modules.auth.dependencies import CurrentUser
from app.modules.users.schemas import MeOut, ProfileUpdate
from app.modules.users.service import UserService

router = APIRouter(prefix="/users", tags=["users"])


def get_user_service(session: SessionDep, cipher: FieldCipherDep) -> UserService:
    return UserService(session, cipher)


UserServiceDep = Annotated[UserService, Depends(get_user_service)]


@router.get("/me", response_model=MeOut)
async def get_me(current: CurrentUser, service: UserServiceDep) -> MeOut:
    return await service.get_me(current.user)


@router.patch("/me/profile", response_model=MeOut)
async def update_my_profile(
    body: ProfileUpdate, current: CurrentUser, service: UserServiceDep, ctx: RequestContextDep
) -> MeOut:
    return await service.update_profile(current.user, body, ctx)
