"""Uniform secret-safe exception projection for HTTP boundaries."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.domain import redact_sensitive_text, root_error, safe_error_message


def install_error_handlers(app: FastAPI) -> None:
    """Preserve HTTP control metadata while exposing only safe root messages."""

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, error: StarletteHTTPException) -> JSONResponse:
        detail: Any = error.detail
        if root_error(error) is not error:
            detail = safe_error_message(error)
        elif isinstance(detail, str):
            detail = redact_sensitive_text(detail)
        return JSONResponse(
            status_code=error.status_code,
            content={"detail": detail},
            headers=error.headers,
        )

    @app.exception_handler(Exception)
    async def unhandled_error(_request: Request, error: Exception) -> JSONResponse:
        return JSONResponse(status_code=500, content={"detail": safe_error_message(error)})


__all__ = ["install_error_handlers"]
