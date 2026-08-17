"""Controlled MCP stdio transport with explicit environment ownership.

The upstream MCP helper intentionally merges a platform default environment
and owns process termination internally.  That is convenient for generic SDK
users but incompatible with this application's Job control plane.  This
adapter keeps the SDK's stream protocol while making process creation and
termination explicit.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from contextlib import asynccontextmanager
from typing import TextIO

import anyio
import mcp.types as types
from anyio.streams.text import TextReceiveStream
from mcp.client.stdio import StdioServerParameters
from mcp.shared.message import SessionMessage

logger = logging.getLogger(__name__)


@asynccontextmanager
async def controlled_stdio_client(server: StdioServerParameters, errlog: TextIO = sys.stderr):
    """Yield MCP memory streams backed by one explicitly configured process."""
    read_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_reader = anyio.create_memory_object_stream(0)
    env = dict(server.env or {})
    command = _windows_command(server.command) if os.name == "nt" else server.command
    process = None
    try:
        process = await _open_process(command, list(server.args), env, server.cwd, errlog)

        async def stdout_reader() -> None:
            assert process.stdout is not None
            buffer = ""
            async with read_writer:
                async for chunk in TextReceiveStream(process.stdout, encoding=server.encoding, errors="replace"):
                    lines = (buffer + chunk).split("\n")
                    buffer = lines.pop()
                    for line in lines:
                        if not line.strip():
                            continue
                        try:
                            message = types.JSONRPCMessage.model_validate_json(line)
                            await read_writer.send(SessionMessage(message))
                        except Exception as exc:
                            logger.warning("MCP stdio message rejected: %s", type(exc).__name__)
                            await read_writer.send(exc)

        async def stdin_writer() -> None:
            assert process.stdin is not None
            async with write_reader:
                async for item in write_reader:
                    payload = item.message.model_dump_json(by_alias=True, exclude_none=True) + "\n"
                    await process.stdin.send(payload.encode(server.encoding, errors="replace"))

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(stdout_reader)
            task_group.start_soon(stdin_writer)
            try:
                yield read_stream, write_stream
            finally:
                await _close_process(process, server)
    finally:
        await read_stream.aclose()
        await write_stream.aclose()
        await read_writer.aclose()
        await write_reader.aclose()


async def _open_process(command: str, args: list[str], env: dict[str, str], cwd, errlog: TextIO):
    if os.name == "nt":
        from mcp.os.win32.utilities import create_windows_process

        return await create_windows_process(command, args, env, errlog, cwd)
    return await anyio.open_process(
        [command, *args],
        env=env,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=errlog,
        start_new_session=True,
    )


async def _close_process(process, server: StdioServerParameters) -> None:
    if process.stdin is not None:
        try:
            await process.stdin.aclose()
        except Exception:
            pass
    try:
        with anyio.fail_after(5.0):
            await process.wait()
            return
    except (TimeoutError, ProcessLookupError):
        pass
    await anyio.to_thread.run_sync(_terminate_tree, process.pid)
    try:
        with anyio.fail_after(5.0):
            await process.wait()
    except TimeoutError:
        try:
            process.kill()
        except Exception:
            pass


def _terminate_tree(pid: int) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass


def _windows_command(command: str) -> str:
    from mcp.os.win32.utilities import get_windows_executable_command

    return get_windows_executable_command(command)


__all__ = ["StdioServerParameters", "controlled_stdio_client"]
