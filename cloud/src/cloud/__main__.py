"""Run the cloud API with Uvicorn."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "cloud.api.app:create_app",
        factory=True,
        host=os.environ.get("MINI_AGENT_CLOUD_HOST", "127.0.0.1"),
        port=int(os.environ.get("MINI_AGENT_CLOUD_PORT", "8100")),
    )


if __name__ == "__main__":
    main()
