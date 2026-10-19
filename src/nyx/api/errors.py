"""Structured API errors and the shared exception handlers.

Implements the envelope and status mapping from ``docs/api-error-conventions.md``. Error
messages never echo secrets, credentials, or raw payloads.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from nyx.error_codes import ErrorCode, http_status_for, spec_for
from nyx.ingestion.errors import FieldDetail, StructuredError

_logger = logging.getLogger("nyx.api")


class ApiError(Exception):
    """An error that maps onto the NYX error envelope and an HTTP status."""

    def __init__(
        self,
        code: ErrorCode,
        *,
        message: str | None = None,
        field_details: list[FieldDetail] | None = None,
        status_override: int | None = None,
    ) -> None:
        self.code = code
        self.message = message or spec_for(code).default_message
        self.field_details = field_details or []
        self.status = status_override or http_status_for(code)
        super().__init__(self.message)


def request_id_for(request: Request) -> str:
    """Return the request id bound by the middleware, or a safe fallback."""

    return str(getattr(request.state, "request_id", "-"))


def api_error_from(structured: StructuredError, request: Request) -> ApiError:
    """Convert an ingestion :class:`StructuredError` into an :class:`ApiError`."""

    try:
        code = ErrorCode(structured.code)
    except ValueError:
        code = ErrorCode.INTERNAL_ERROR
    return ApiError(code, message=structured.message, field_details=structured.field_details)


def _envelope(
    code: ErrorCode,
    *,
    message: str,
    request_id: str,
    field_details: list[FieldDetail] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "code": code.value,
        "message": message[:500],
        "request_id": request_id,
    }
    if field_details:
        payload["field_details"] = [
            detail.model_dump(exclude_none=True) for detail in field_details
        ]
    return payload


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    if exc.status >= 500:
        _logger.error(
            "api error",
            extra={"request_id": request_id_for(request), "code": exc.code.value},
        )
    return JSONResponse(
        status_code=exc.status,
        content=_envelope(
            exc.code,
            message=exc.message,
            request_id=request_id_for(request),
            field_details=exc.field_details,
        ),
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    details: list[FieldDetail] = []
    malformed = False
    for error in exc.errors():
        error_type = str(error.get("type", ""))
        if error_type == "json_invalid":
            malformed = True
        location = "/" + "/".join(
            str(token).replace("~", "~0").replace("/", "~1") for token in error.get("loc", ())
        )
        details.append(
            FieldDetail(
                field=location,
                issue=str(error.get("msg", "invalid value")),
                code="invalid_format" if malformed else None,
            )
        )
    code = ErrorCode.INVALID_REQUEST if malformed else ErrorCode.SCHEMA_VALIDATION_FAILED
    return JSONResponse(
        status_code=http_status_for(code),
        content=_envelope(
            code,
            message=spec_for(code).default_message,
            request_id=request_id_for(request),
            field_details=details,
        ),
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if exc.status_code == 404:
        code = ErrorCode.NOT_FOUND
    elif exc.status_code == 405:
        code = ErrorCode.INVALID_REQUEST
    else:
        code = ErrorCode.INVALID_REQUEST if exc.status_code < 500 else ErrorCode.INTERNAL_ERROR
    message = exc.detail if isinstance(exc.detail, str) else spec_for(code).default_message
    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(code, message=message, request_id=request_id_for(request)),
    )


async def storage_error_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    _logger.error(
        "storage error",
        extra={"request_id": request_id_for(request), "error_type": type(exc).__name__},
    )
    return JSONResponse(
        status_code=http_status_for(ErrorCode.STORAGE_UNAVAILABLE),
        content=_envelope(
            ErrorCode.STORAGE_UNAVAILABLE,
            message=spec_for(ErrorCode.STORAGE_UNAVAILABLE).default_message,
            request_id=request_id_for(request),
        ),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    _logger.error(
        "unhandled exception",
        extra={"request_id": request_id_for(request), "error_type": type(exc).__name__},
    )
    return JSONResponse(
        status_code=500,
        content=_envelope(
            ErrorCode.INTERNAL_ERROR,
            message=spec_for(ErrorCode.INTERNAL_ERROR).default_message,
            request_id=request_id_for(request),
        ),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach every shared exception handler to ``app``."""

    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(SQLAlchemyError, storage_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)
