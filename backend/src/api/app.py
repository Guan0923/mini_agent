"""FastAPI application: chat with the agent, plus an optional benchmark sub-app.

The main chat backend never imports the benchmark harness; benchmark routes
live in a separately mounted sub-application under /benchmark.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from .security import LocalWebSettings, origin_allowed  # noqa: E402
from .state import DEFAULT_DATA_ROOT, WebAppState  # noqa: E402


def create_app(state: WebAppState | None = None) -> FastAPI:
    app = FastAPI(title="Mini-Agent Web", version="0.0.1")
    resolved = state or WebAppState(DEFAULT_DATA_ROOT)
    app.state.web = resolved
    web_settings = LocalWebSettings.from_env()

    @app.middleware("http")
    async def enforce_local_browser_origin(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not origin_allowed(request, web_settings):
            return JSONResponse({"detail": "不允许的请求来源。"}, status_code=403)
        return await call_next(request)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(web_settings.allowed_origins),
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=False,
    )

    from .chat.decisions import router as decisions_router
    from .jobs_routes import router as jobs_router
    from .projects import router as projects_router
    from .sandbox_routes import router as sandbox_router
    from .session_files import router as session_files_router
    from .settings import router as settings_router
    from .shared.benchmark import create_benchmark_app
    from .shared.info import router as info_router
    from .sidebar_threads import router as sidebar_threads_router
    from .turns import router as turns_router

    app.include_router(settings_router)
    app.include_router(decisions_router)
    app.include_router(jobs_router)
    app.include_router(sandbox_router)
    app.include_router(projects_router)
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
        resolved.settings.ping()
        resolved.projects.list("all")
        return {"status": "ready", "service": "mini-agent-backend", "database": "ok"}

    # In production the local backend can serve the browser bundle from the
    # same loopback origin.  Development keeps using Vite's proxy, and an
    # absent ``dist`` directory simply leaves the API-only app unchanged.
    frontend_dist = Path(os.environ.get("MINI_AGENT_FRONTEND_DIST", str(REPO_ROOT / "frontend" / "dist"))).expanduser()
    if frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")

    @app.on_event("shutdown")
    def close_state() -> None:
        resolved.close()

    return app
