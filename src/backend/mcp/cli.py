"""Command-line entry point for Mini-Agent's stdio MCP server."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from mcp.server.stdio import stdio_server

from .server import create_server


def _existing_directory(value: str) -> Path:
    workspace = Path(value).expanduser()
    if not workspace.exists():
        raise argparse.ArgumentTypeError(f"workspace does not exist: {workspace}")
    if not workspace.is_dir():
        raise argparse.ArgumentTypeError(f"workspace is not a directory: {workspace}")
    return workspace.resolve()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mini-Agent safe read-only MCP server")
    parser.add_argument(
        "--workspace",
        type=_existing_directory,
        default=Path.cwd(),
        help="Existing workspace directory available to MCP tools (default: current directory).",
    )
    return parser.parse_args(argv)


async def run_stdio_server(workspace: Path) -> None:
    """Run the MCP protocol over standard input and output."""

    server = create_server(workspace)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    asyncio.run(run_stdio_server(args.workspace))
    return 0
