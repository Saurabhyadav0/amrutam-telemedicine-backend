"""RFC 9457 problem+json errors: one response shape everywhere, always with the request id."""

from typing import Any, cast

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = structlog.get_logger(__name__)

PROBLEM_JSON = "application/problem+json"


class AppError(Exception):
    status_code = 400
    code = "bad_request"
    title = "Bad Request"

    def __init__(
        self,
        detail: str | None = None,
        *,
        code: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail or self.title)
        self.detail = detail or self.title
        if code:
            self.code = code
        self.headers = headers


class BadRequest(AppError):
    pass


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"
    title = "Unauthorized"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"
    title = "Forbidden"


class NotFound(AppError):
    status_code = 404
    code = "not_found"
    title = "Not Found"


class Conflict(AppError):
    status_code = 409
    code = "conflict"
    title = "Conflict"


class Locked(AppError):
    status_code = 423
    code = "locked"
    title = "Locked"


class TooManyRequests(AppError):
    status_code = 429
    code = "rate_limited"
    title = "Too Many Requests"


def problem_response(
    status: int,
    *,
    code: str,
    title: str,
    detail: str,
    request_id: str | None = None,
    headers: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"https://errors.amrutam.global/{code}",
        "title": title,
        "status": status,
        "code": code,
        "detail": detail,
        "request_id": request_id,
    }
    if extra:
        body.update(extra)
    return JSONResponse(body, status_code=status, headers=headers, media_type=PROBLEM_JSON)


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


async def _app_error(request: Request, exc: Exception) -> JSONResponse:
    exc = cast(AppError, exc)
    return problem_response(
        exc.status_code,
        code=exc.code,
        title=exc.title,
        detail=exc.detail,
        request_id=_request_id(request),
        headers=exc.headers,
    )


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    exc = cast(RequestValidationError, exc)
    # Never echo submitted values back: they may be passwords, OTPs or health data.
    errors = [
        {"loc": list(err.get("loc", ())), "msg": err.get("msg"), "type": err.get("type")}
        for err in exc.errors()
    ]
    return problem_response(
        422,
        code="validation_error",
        title="Unprocessable Content",
        detail="Request validation failed",
        request_id=_request_id(request),
        extra={"errors": errors},
    )


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    exc = cast(StarletteHTTPException, exc)
    return problem_response(
        exc.status_code,
        code="http_error",
        title=str(exc.detail),
        detail=str(exc.detail),
        request_id=_request_id(request),
        headers=getattr(exc, "headers", None),
    )


async def _unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled_error", error_type=type(exc).__name__)
    return problem_response(
        500,
        code="internal_error",
        title="Internal Server Error",
        detail="An unexpected error occurred",
        request_id=_request_id(request),
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _unhandled_error)
