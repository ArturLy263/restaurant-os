"""Exception handlers producing the error contract in app.core.errors."""

import logging

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import ErrorCode, error_code_for
from app.core.request_id import REQUEST_ID_HEADER, get_request_id


logger = logging.getLogger("app.errors")

INTERNAL_ERROR_MESSAGE = "Internal server error"
VALIDATION_ERROR_MESSAGE = "Request validation failed"


def error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    detail=None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = get_request_id(request)

    return JSONResponse(
        status_code=status_code,
        content={
            "detail": message if detail is None else detail,
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id,
            },
        },
        headers={**(headers or {}), REQUEST_ID_HEADER: request_id},
    )


async def http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    message = exc.detail if isinstance(exc.detail, str) else None

    return error_response(
        request,
        status_code=exc.status_code,
        code=error_code_for(exc),
        message=message or "HTTP error",
        detail=jsonable_encoder(exc.detail),
        headers=getattr(exc, "headers", None),
    )


async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    # `detail` keeps FastAPI's list of field errors.
    return error_response(
        request,
        status_code=422,
        code=ErrorCode.VALIDATION_ERROR,
        message=VALIDATION_ERROR_MESSAGE,
        detail=jsonable_encoder(exc.errors()),
    )


async def unhandled_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    # The details go to the log only, never to the client.
    logger.exception(
        "Unhandled error (request_id=%s) on %s %s",
        get_request_id(request),
        request.method,
        request.url.path,
    )

    return error_response(
        request,
        status_code=500,
        code=ErrorCode.INTERNAL_ERROR,
        message=INTERNAL_ERROR_MESSAGE,
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(
        RequestValidationError,
        validation_exception_handler,
    )
    app.add_exception_handler(Exception, unhandled_exception_handler)
