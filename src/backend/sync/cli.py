"""Start the optional PostgreSQL synchronization service."""

from __future__ import annotations

import argparse
import os

from .server import PostgresSyncRepository, create_sync_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mini-Agent PostgreSQL sync service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    database_url = os.environ.get("MINI_AGENT_DATABASE_URL", "").strip()
    token = os.environ.get("MINI_AGENT_SYNC_TOKEN", "").strip()
    if not database_url or not token:
        parser.error("MINI_AGENT_DATABASE_URL and MINI_AGENT_SYNC_TOKEN are required.")
    try:
        import uvicorn
    except ImportError as exc:
        parser.error('Install server dependencies with: pip install "mini-agent[server]"')
        raise AssertionError from exc
    app = create_sync_app(PostgresSyncRepository(database_url), token)
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)
    return 0
