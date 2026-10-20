import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.deps import SessionDep, TokenServiceDep
from app.core.errors import Forbidden, Unauthorized
from app.modules.users.models import PRIVILEGED_ROLES, User, UserRole
from app.modules.users.repository import UserRepository

bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: uuid.UUID
    amr: frozenset[str]

    @property
    def mfa_verified(self) -> bool:
        return "mfa" in self.amr


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    user: User
    principal: Principal


async def get_principal(
    tokens: TokenServiceDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> Principal:
    if credentials is None:
        raise Unauthorized("Missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    claims = tokens.decode(credentials.credentials, expected_type="access")
    return Principal(user_id=uuid.UUID(claims["sub"]), amr=frozenset(claims.get("amr", [])))


async def get_current_user(
    principal: Annotated[Principal, Depends(get_principal)], session: SessionDep
) -> AuthenticatedUser:
    # Role and status come from the DB, not the token: revocation and role changes apply at once.
    user = await UserRepository(session).get(principal.user_id)
    if user is None or not user.is_active:
        raise Unauthorized("Account is not active", code="account_inactive")
    return AuthenticatedUser(user=user, principal=principal)


CurrentUser = Annotated[AuthenticatedUser, Depends(get_current_user)]


def require_roles(*roles: UserRole) -> Callable[..., Awaitable[AuthenticatedUser]]:
    """RBAC guard. Privileged roles must also have completed MFA in this session."""
    allowed = frozenset(roles)

    async def dependency(current: CurrentUser) -> AuthenticatedUser:
        if current.user.role not in allowed:
            raise Forbidden("Your role cannot perform this action", code="insufficient_role")
        if current.user.role in PRIVILEGED_ROLES and not current.principal.mfa_verified:
            raise Forbidden("Multi-factor authentication is required", code="mfa_required")
        return current

    return dependency
