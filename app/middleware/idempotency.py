"""Idempotency for write requests (POST/PUT/PATCH/DELETE) via the ``Idempotency-Key`` header.

Semantics (modelled on the IETF httpapi idempotency-key draft and Stripe):

* first request with a key       -> processed, response stored for ``idempotency_ttl_seconds``
* same key + same request        -> stored response replayed, ``Idempotent-Replayed: true``
* same key + different request   -> 422 ``idempotency_key_mismatch``
* same key while still running   -> 409 ``idempotency_in_progress`` (client retries later)
* 5xx / transient 4xx            -> key released so the client can retry with the same key

Keys are scoped per caller (``user:<id>`` or ``anon``) so one user can never replay another's
response. The ``begin`` step is a single atomic ``INSERT .. ON CONFLICT`` so two concurrent
requests with the same key cannot both run. A lock lease (``locked_until``) lets a key whose
worker crashed be taken over.

Note: the stored response is written after the handler commits. Writes whose duplicates would
be harmful (bookings, payments) additionally carry DB-level uniqueness constraints, so even the
crash-between-commit-and-store window cannot create a duplicate.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any

import structlog
from fastapi import Header
from sqlalchemy import Integer, LargeBinary, String, delete, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings
from app.core.crypto import sha256_hex
from app.core.db import Base, utcnow
from app.core.errors import AppError, BadRequest, problem_response
from app.core.security import TokenService

logger = structlog.get_logger(__name__)

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
KEY_HEADER = "idempotency-key"
REPLAY_HEADER = "Idempotent-Replayed"
# Statuses that mean "not processed / retry may succeed": don't pin them to the key.
RELEASE_STATUSES = frozenset({401, 408, 409, 423, 429})
STORED_RESPONSE_HEADERS = frozenset({"content-type", "location"})
MAX_STORED_BODY = 256 * 1024


class IdempotencyStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    owner: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    method: Mapped[str] = mapped_column(String(8))
    path: Mapped[str] = mapped_column(String(512))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_headers: Mapped[dict[str, str] | None] = mapped_column(JSONB)
    response_body: Mapped[bytes | None] = mapped_column(LargeBinary)
    locked_until: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime]


@dataclass(frozen=True, slots=True)
class StoredResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class IdempotencyStore:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self._sessionmaker = sessionmaker
        self._ttl = timedelta(seconds=settings.idempotency_ttl_seconds)
        self._lock = timedelta(seconds=settings.idempotency_lock_seconds)

    async def acquire(
        self, owner: str, key: str, method: str, path: str, request_hash: str
    ) -> bool:
        """Claim the key. True if this request should run the handler."""
        now = utcnow()
        values: dict[str, Any] = {
            "owner": owner,
            "key": key,
            "method": method,
            "path": path,
            "request_hash": request_hash,
            "status": IdempotencyStatus.IN_PROGRESS.value,
            "locked_until": now + self._lock,
            "created_at": now,
            "expires_at": now + self._ttl,
        }
        table = IdempotencyKey.__table__
        stmt = (
            insert(IdempotencyKey)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[table.c.owner, table.c.key],
                set_={
                    **{k: v for k, v in values.items() if k not in ("owner", "key")},
                    "response_status": None,
                    "response_headers": None,
                    "response_body": None,
                },
                # Take over only an expired record, or a crashed run of the *same* request.
                where=or_(
                    table.c.expires_at < now,
                    (table.c.status == IdempotencyStatus.IN_PROGRESS.value)
                    & (table.c.locked_until < now)
                    & (table.c.request_hash == request_hash),
                ),
            )
            .returning(table.c.key)
        )
        async with self._sessionmaker() as session, session.begin():
            return (await session.execute(stmt)).first() is not None

    async def get(self, owner: str, key: str) -> IdempotencyKey | None:
        async with self._sessionmaker() as session:
            return await session.scalar(
                select(IdempotencyKey).where(
                    IdempotencyKey.owner == owner, IdempotencyKey.key == key
                )
            )

    async def complete(self, owner: str, key: str, response: StoredResponse) -> None:
        async with self._sessionmaker() as session, session.begin():
            await session.execute(
                update(IdempotencyKey)
                .where(IdempotencyKey.owner == owner, IdempotencyKey.key == key)
                .values(
                    status=IdempotencyStatus.COMPLETED.value,
                    response_status=response.status,
                    response_headers=response.headers,
                    response_body=response.body,
                )
            )

    async def release(self, owner: str, key: str) -> None:
        async with self._sessionmaker() as session, session.begin():
            await session.execute(
                delete(IdempotencyKey).where(
                    IdempotencyKey.owner == owner,
                    IdempotencyKey.key == key,
                    IdempotencyKey.status == IdempotencyStatus.IN_PROGRESS.value,
                )
            )


class _ClientDisconnected(Exception):
    pass


class IdempotencyMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        settings: Settings,
        token_service: TokenService,
        excluded_paths: frozenset[str] = frozenset(),
    ) -> None:
        self.app = app
        self.settings = settings
        self.tokens = token_service
        self.excluded_paths = excluded_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] not in WRITE_METHODS
            or scope["path"] in self.excluded_paths
        ):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        key = headers.get(KEY_HEADER)
        if key is None:
            await self.app(scope, receive, send)
            return

        request_id = scope.get("state", {}).get("request_id")
        if not is_valid_key(key):
            await problem_response(
                400,
                code="invalid_idempotency_key",
                title="Bad Request",
                detail="Idempotency-Key must be 8-128 chars of [A-Za-z0-9_.:-]",
                request_id=request_id,
            )(scope, receive, send)
            return

        try:
            body = await _read_body(receive)
        except _ClientDisconnected:
            return

        owner = self._owner(headers)
        method, path = scope["method"], scope["path"]
        request_hash = sha256_hex(
            b"\n".join([method.encode(), path.encode(), scope.get("query_string", b""), body])
        )
        store = IdempotencyStore(scope["app"].state.db.sessionmaker, self.settings)

        if not await store.acquire(owner, key, method, path, request_hash):
            response = await self._respond_existing(store, owner, key, request_hash, request_id)
            await response(scope, receive, send)
            return

        captured_status: int | None = None
        captured_headers: dict[str, str] = {}
        captured_body = bytearray()
        body_sent = False

        async def replay_receive() -> Message:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        async def capture_send(message: Message) -> None:
            nonlocal captured_status
            if message["type"] == "http.response.start":
                captured_status = message["status"]
                for name, value in Headers(raw=message.get("headers", [])).items():
                    if name in STORED_RESPONSE_HEADERS:
                        captured_headers[name] = value
            elif message["type"] == "http.response.body":
                captured_body.extend(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, replay_receive, capture_send)
        except BaseException:
            await store.release(owner, key)
            raise

        if (
            captured_status is None
            or captured_status >= 500
            or captured_status in RELEASE_STATUSES
            or len(captured_body) > MAX_STORED_BODY
        ):
            await store.release(owner, key)
        else:
            await store.complete(
                owner, key, StoredResponse(captured_status, captured_headers, bytes(captured_body))
            )

    def _owner(self, headers: Headers) -> str:
        auth = headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() == "bearer" and token:
            try:
                claims = self.tokens.decode(token, expected_type="access")
                return f"user:{claims['sub']}"
            except AppError:
                pass
        return "anon"

    async def _respond_existing(
        self,
        store: IdempotencyStore,
        owner: str,
        key: str,
        request_hash: str,
        request_id: str | None,
    ) -> Response:
        record = await store.get(owner, key)
        if record is not None and record.request_hash != request_hash:
            return problem_response(
                422,
                code="idempotency_key_mismatch",
                title="Unprocessable Content",
                detail="Idempotency-Key was already used with a different request",
                request_id=request_id,
            )
        if record is None or record.status != IdempotencyStatus.COMPLETED.value:
            return problem_response(
                409,
                code="idempotency_in_progress",
                title="Conflict",
                detail="A request with this Idempotency-Key is still being processed",
                request_id=request_id,
                headers={"Retry-After": "1"},
            )
        logger.info("idempotent_replay", path=record.path)
        return Response(
            content=record.response_body or b"",
            status_code=record.response_status or 200,
            headers={**(record.response_headers or {}), REPLAY_HEADER: "true"},
        )


def is_valid_key(key: str) -> bool:
    return 8 <= len(key) <= 128 and all(c.isascii() and (c.isalnum() or c in "_.:-") for c in key)


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    more = True
    while more:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise _ClientDisconnected
        chunks.append(message.get("body", b""))
        more = message.get("more_body", False)
    return b"".join(chunks)


def require_idempotency_key(
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            description="Client-generated unique key (e.g. UUIDv4). Retries must reuse it.",
        ),
    ] = None,
) -> None:
    """Route dependency for writes where a duplicate would be harmful; also documents the header."""
    if idempotency_key is None:
        raise BadRequest("Idempotency-Key header is required", code="idempotency_key_required")
