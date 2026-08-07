"""FastAPI application: chat with the agent, plus an optional benchmark sub-app.

The main chat backend never imports the benchmark harness; benchmark routes
live in a separately mounted sub-application under /benchmark.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from .auth.dependencies import require_user  # noqa: E402
from .state import DEFAULT_DATA_ROOT, WebAppState  # noqa: E402


def create_app(state: WebAppState | None = None) -> FastAPI:
    app = FastAPI(title="Mini-Agent Web", version="0.2.0")
    resolved = state or WebAppState(DEFAULT_DATA_ROOT)
    app.state.web = resolved
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved.auth_service.settings.allowed_origins),
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )

    from .auth import router as auth_router
    from .chat import router as chat_router
    from .sessions import router as sessions_router
    from .shared.benchmark import create_benchmark_app
    from .shared.info import router as info_router

    app.include_router(auth_router)
    app.include_router(chat_router, dependencies=[Depends(require_user)])
    app.include_router(info_router, dependencies=[Depends(require_user)])
    app.include_router(sessions_router, dependencies=[Depends(require_user)])
    app.mount("/benchmark", create_benchmark_app(resolved))

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "service": "mini-agent-backend"}

    @app.on_event("shutdown")
    def close_state() -> None:
        resolved.close()

    return app
