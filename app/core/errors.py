"""One error shape for the whole API (D22).

    {"status": "FAILED", "error_code": "INVALID_CREDENTIALS", "message": "..."}

`error_code` is for machines and never changes wording; `message` is for people
and may. A predictable failure never surfaces as a 500 (invariant 5), so the
handlers below cover not just our own exception but every other way FastAPI can
answer -- otherwise a validation error would leave through the default
`{"detail": [...]}` and the contract would hold only for the paths we remembered.
"""

from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException


class DomainError(Exception):
    """A failure the API knows how to describe: it has a status and a code."""

    def __init__(self, status_code: int, error_code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message


class ErrorResponse(BaseModel):
    """The body of every failed request. Declared as a model so it appears in
    the OpenAPI schema and Swagger stops implying that only 200 exists."""

    status: str = Field(default="FAILED", examples=["FAILED"])
    error_code: str = Field(description="Stable, for code to branch on.")
    message: str = Field(description="For a person to read. Wording may change.")


def error_body(error_code: str, message: str) -> dict[str, str]:
    return {"status": "FAILED", "error_code": error_code, "message": message}


def documented_errors(**by_status: str) -> dict[int | str, dict[str, Any]]:
    """Declares the failures of a route for the docs, one line per status:

        responses=documented_errors(**{"401": "NOT_AUTHENTICATED — no token"})

    Swagger then shows the D22 envelope and the exact `error_code` to expect,
    instead of leaving the caller to discover them by trial.
    """
    return {
        int(status): {
            "model": ErrorResponse,
            "description": description,
            "content": {
                "application/json": {
                    "example": error_body(description.split(" —")[0], "…"),
                }
            },
        }
        for status, description in by_status.items()
    }


async def handle_domain_error(request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(exc.error_code, exc.message),
        # A 401 without this header is incomplete: it tells the client it needs
        # to authenticate but not how.
        headers={"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None,
    )


async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Whatever FastAPI raises on its own -- 404 for an unknown path, 405 for the
    wrong method -- still leaves in the D22 envelope."""
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(HTTPStatus(exc.status_code).name, str(exc.detail)),
        headers=exc.headers,
    )


async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 from Pydantic. The default body is `{"detail": [...]}`, which is a
    second error format -- this collapses it into the only one."""
    problems = "; ".join(
        f"{'.'.join(str(p) for p in e['loc'][1:]) or 'body'}: {e['msg']}" for e in exc.errors()
    )
    return JSONResponse(
        status_code=422,
        content=error_body("VALIDATION_ERROR", problems or "Invalid request."),
    )


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """The only 500 the API emits, and it always means a bug on our side.

    The exception is deliberately not echoed to the client: it would leak table
    names and file paths. It is logged by the server instead.
    """
    return JSONResponse(
        status_code=500,
        content=error_body("INTERNAL_ERROR", "Unexpected error. The request was not completed."),
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, handle_domain_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(Exception, handle_unexpected_error)
