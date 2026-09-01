"""FastAPI application: chat with the agent, plus an optional benchmark sub-app.

The main chat backend never imports the benchmark harness; benchmark routes
live in a separately mounted sub-application under /benchmark.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .error_handlers import install_error_handlers
from .security import LocalWebSettings, origin_allowed
from .state import DEFAULT_DATA_ROOT, WebAppState

REPO_ROOT = Path(__file__).resolve().parents[3]


def create_app(state: WebAppState | None = None) -> FastAPI:
    resolved = state or WebAppState(DEFAULT_DATA_ROOT)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            resolved.close()

    app = FastAPI(title="Mini-Agent Web", version="0.0.1", lifespan=lifespan)
    install_error_handlers(app)
    app.state.web = resolved
    web_settings = LocalWebSettings.from_env()

    @app.middleware("http")
    async def enforce_local_browser_origin(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not origin_allowed(request, web_settings):
            return JSONResponse(
                {"detail": "不允许的请求来源。"},
                status_code=403,
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(web_settings.allowed_origins),
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=False,
    )

    from .chat.decisions import router as decisions_router
    from .routes.agent_threads import router as agent_threads_router
    from .routes.jobs import router as jobs_router
    from .routes.mcp_settings import router as mcp_settings_router
    from .routes.projects import router as projects_router
    from .routes.right_panel import router as right_panel_router
    from .routes.sandbox import router as sandbox_router
    from .routes.settings import router as settings_router
    from .routes.sidebar_threads import router as sidebar_threads_router
    from .routes.skill_settings import router as skill_settings_router
    from .routes.turns import router as turns_router
    from .session_files import router as session_files_router
    from .shared.benchmark import create_benchmark_app
    from .shared.info import router as info_router

    app.include_router(settings_router)
    app.include_router(skill_settings_router)
    app.include_router(mcp_settings_router)
    app.include_router(agent_threads_router)
    app.include_router(decisions_router)
    app.include_router(jobs_router)
    app.include_router(sandbox_router)
    app.include_router(projects_router)
    app.include_router(right_panel_router)
    app.include_router(info_router)
    app.include_router(sidebar_threads_router)
    app.include_router(turns_router)
    app.include_router(session_files_router)
    app.mount("/benchmark", create_benchmark_app(resolved))

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "service": "mini-agent-backend"}

    @app.get("/api/ready")
    def ready() -> dict:
        try:
            resolved.settings.ping()
            resolved.projects.list("all")
            resolved.message_queue.ping()
        except Exception as exc:
            from backend.domain import MessageQueueUnavailable

            if isinstance(exc, MessageQueueUnavailable):
                raise HTTPException(status_code=503, detail="message_queue_unavailable") from exc
            raise
        return {"status": "ready", "service": "mini-agent-backend", "database": "ok", "redis": "ok"}

    @app.api_route(
        "/api/{missing_path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    def missing_api(missing_path: str) -> None:
        del missing_path
        raise HTTPException(status_code=404, detail="Not Found")

    # In production the local backend can serve the browser bundle from the
    # same loopback origin.  Development keeps using Vite's proxy, and an
    # absent ``dist`` directory simply leaves the API-only app unchanged.
    frontend_dist = Path(os.environ.get("MINI_AGENT_FRONTEND_DIST", str(REPO_ROOT / "frontend" / "dist"))).expanduser()
    if frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")

    return app
