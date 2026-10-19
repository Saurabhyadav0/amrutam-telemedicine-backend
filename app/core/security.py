import secrets
import time
import uuid
from collections.abc import Iterable
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from starlette.concurrency import run_in_threadpool

from app.core.config import Settings
from app.core.errors import Unauthorized

JWT_ALGORITHM = "HS256"


class PasswordService:
    """Argon2id hashing. Hashing is CPU-bound (~50ms), so it runs off the event loop."""

    def __init__(self) -> None:
        self._hasher = PasswordHasher()
        self._dummy_hash = self._hasher.hash(secrets.token_urlsafe(16))

    async def hash(self, password: str) -> str:
        return await run_in_threadpool(self._hasher.hash, password)

    async def verify(self, password_hash: str, password: str) -> bool:
        return await run_in_threadpool(self._verify_sync, password_hash, password)

    async def verify_dummy(self, password: str) -> None:
        """Spend the same time as a real check so unknown emails can't be found by timing."""
        await self.verify(self._dummy_hash, password)

    def needs_rehash(self, password_hash: str) -> bool:
        return self._hasher.check_needs_rehash(password_hash)

    def _verify_sync(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False


class TokenService:
    """Short-lived JWTs signed with a key ring. ``kid`` in the header selects the verify key."""

    def __init__(self, settings: Settings) -> None:
        self._keys = settings.jwt_keys
        self._active_kid = settings.jwt_active_kid
        self._issuer = settings.jwt_issuer
        self._access_ttl = settings.access_token_ttl_seconds
        self._mfa_ttl = settings.mfa_token_ttl_seconds

    def create_access_token(self, user_id: uuid.UUID, role: str, amr: Iterable[str]) -> str:
        return self._encode(
            {"sub": str(user_id), "typ": "access", "role": role, "amr": sorted(amr)},
            self._access_ttl,
        )

    def create_mfa_token(self, user_id: uuid.UUID) -> str:
        return self._encode({"sub": str(user_id), "typ": "mfa", "amr": ["pwd"]}, self._mfa_ttl)

    def create_room_token(
        self, consultation_id: uuid.UUID, user_id: uuid.UUID, role: str, ttl: int
    ) -> str:
        """Short-lived credential a video/chat provider validates before admitting a user."""
        return self._encode(
            {
                "sub": str(user_id),
                "typ": "room",
                "room": f"consult-{consultation_id}",
                "role": role,
            },
            ttl,
        )

    def decode(self, token: str, *, expected_type: str) -> dict[str, Any]:
        try:
            kid = jwt.get_unverified_header(token).get("kid")
            key = self._keys.get(kid) if isinstance(kid, str) else None
            if key is None:
                raise Unauthorized("Invalid or expired token", code="invalid_token")
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=[JWT_ALGORITHM],
                issuer=self._issuer,
                options={"require": ["exp", "iat", "sub", "typ"]},
                leeway=5,
            )
        except jwt.PyJWTError as exc:
            raise Unauthorized("Invalid or expired token", code="invalid_token") from exc
        if claims.get("typ") != expected_type:
            raise Unauthorized("Invalid or expired token", code="invalid_token")
        return claims

    def _encode(self, claims: dict[str, Any], ttl: int) -> str:
        now = int(time.time())
        payload = {
            **claims,
            "iss": self._issuer,
            "iat": now,
            "nbf": now,
            "exp": now + ttl,
            "jti": uuid.uuid4().hex,
        }
        return jwt.encode(
            payload,
            self._keys[self._active_kid],
            algorithm=JWT_ALGORITHM,
            headers={"kid": self._active_kid},
        )


def new_opaque_token() -> str:
    return secrets.token_urlsafe(48)
