"""FastAPI application: chat with the agent, plus an optional benchmark sub-app.

The main chat backend never imports the benchmark harness; benchmark routes
live in a separately mounted sub-application under /benchmark.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from .state import DEFAULT_DATA_ROOT, WebAppState  # noqa: E402


def create_app(state: WebAppState | None = None) -> FastAPI:
    app = FastAPI(title="Mini-Agent Web", version="0.2.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    resolved = state or WebAppState(DEFAULT_DATA_ROOT)
    app.state.web = resolved

    from .benchmark_app import create_benchmark_app
    from .chat import router as chat_router
    from .info import router as info_router
    from .sessions import router as sessions_router

    app.include_router(chat_router)
    app.include_router(info_router)
    app.include_router(sessions_router)
    app.mount("/benchmark", create_benchmark_app(resolved))

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "service": "mini-agent-backend"}

    return app
