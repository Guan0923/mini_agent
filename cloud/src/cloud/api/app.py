"""FastAPI application for the remote account and snapshot control plane."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from cloud.auth.mail import NullMailer, SMTPMailer, SMTPSettings
from cloud.auth.service import CloudAuthService
from cloud.auth.types import AuthStorageUnavailable
from cloud.storage import CloudMasterCipher, PostgresAuthRepository, PostgresCloudSnapshotRepository

from .routes import build_router


@dataclass
class CloudAppState:
    auth: Any
    snapshots: Any
    auth_service: CloudAuthService
    mailer: Any

    def close(self) -> None:
        for resource in (self.mailer, self.auth, self.snapshots):
            close = getattr(resource, "close", None)
            if callable(close):
                close()


def create_app(
    *,
    database_url: str | None = None,
    secret_key: str | None = None,
    auth_repository: Any | None = None,
    snapshot_repository: Any | None = None,
    mailer: Any | None = None,
) -> FastAPI:
    """Create the cloud app.

    Repositories may be injected by tests. Production startup requires a
    PostgreSQL URL and a master secret for user data key envelopes.  The
    master-secret check is performed here, at the cloud boundary, so a
    misconfigured deployment cannot start and accept account traffic before
    its snapshot encryption path is usable.
    """

    configured_url = (database_url or os.environ.get("DATABASE_URL", "")).strip()
    if auth_repository is None and not configured_url:
        raise RuntimeError("DATABASE_URL is required for the cloud service.")
    auth = auth_repository or PostgresAuthRepository(configured_url)
    if snapshot_repository is None:
        master_cipher = CloudMasterCipher(secret_key)
        # Resolve the active key once during startup.  ``CloudMasterCipher``
        # intentionally derives versioned keys lazily for rotations, but an
        # absent deployment secret must be a startup error rather than a
        # delayed 500 on the first account synchronization.
        master_cipher.validate()
        snapshots = PostgresCloudSnapshotRepository(configured_url, master_cipher=master_cipher)
    else:
        snapshots = snapshot_repository
    resolved_mailer = mailer
    if resolved_mailer is None:
        smtp = SMTPSettings.from_environment()
        resolved_mailer = SMTPMailer(smtp) if smtp is not None else NullMailer()
    state = CloudAppState(
        auth=auth,
        snapshots=snapshots,
        auth_service=CloudAuthService(auth, resolved_mailer),
        mailer=resolved_mailer,
    )
    app = FastAPI(title="Mini-Agent Cloud", version="0.3.0")
    app.state.cloud = state
    app.include_router(build_router())

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "mini-agent-cloud"}

    @app.get("/ready")
    def ready() -> dict[str, str]:
        state.auth.ping()
        # Authentication and snapshot repositories are separate adapters even
        # though production currently shares one PostgreSQL database.  Probe
        # both when the adapter exposes ``ping`` so a partially initialized
        # cloud deployment cannot report ready while sync traffic is broken.
        ping_snapshots = getattr(state.snapshots, "ping", None)
        if callable(ping_snapshots):
            ping_snapshots()
        return {"status": "ready", "service": "mini-agent-cloud", "database": "ok"}

    @app.exception_handler(AuthStorageUnavailable)
    async def storage_unavailable(_request, _exc) -> JSONResponse:
        return JSONResponse({"detail": "云端认证数据库暂不可用。"}, status_code=503)

    @app.exception_handler(Exception)
    async def unexpected(_request, _exc) -> JSONResponse:
        return JSONResponse({"detail": "云端服务暂时无法处理请求。"}, status_code=500)

    @app.on_event("shutdown")
    def close_state() -> None:
        state.close()

    return app


__all__ = ["CloudAppState", "create_app"]
