"""Domain errors mapped to HTTP responses."""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    status_code = 400

    def __init__(self, message: str, payload: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.payload = payload or {}


class NotFound(DomainError):
    status_code = 404


class Conflict(DomainError):
    status_code = 409


class Unprocessable(DomainError):
    status_code = 422


def register_handlers(app) -> None:
    from fastapi import Request
    from fastapi.encoders import jsonable_encoder
    from fastapi.responses import JSONResponse

    @app.exception_handler(DomainError)
    async def _domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
        body = {"detail": exc.message, **exc.payload}
        return JSONResponse(status_code=exc.status_code, content=jsonable_encoder(body))
