"""Run-local subscriptions; notifications never execute work or modify chat."""

from __future__ import annotations

import asyncio
from typing import Any

from mcp import Client, types
from mcp.client.subscriptions import ResourceUpdated

from backend.tools import ToolError

MAX_RESOURCE_UPDATES = 1024


class ResourceSubscriptions:
    def __init__(self) -> None:
        self.states: dict[str, str] = {}
        self.changed: dict[str, int] = {}
        self.revision = 0
        self.overflow = False
        self.tasks: dict[str, asyncio.Task] = {}

    def record(self, uri: str) -> None:
        self.revision += 1
        if uri in self.changed or len(self.changed) < MAX_RESOURCE_UPDATES:
            self.changed[uri] = self.revision
        else:
            self.overflow = True

    async def notification(self, message: Any) -> None:
        if isinstance(message, types.ResourceUpdatedNotification):
            if self.states.get(message.params.uri) in {"pending", "active"}:
                self.record(message.params.uri)
        elif isinstance(message, Exception):
            self.lost()

    def lost(self) -> None:
        for uri, status in self.states.items():
            if status == "active":
                self.states[uri] = "lost"

    def snapshot(self) -> dict[str, Any]:
        return {
            "subscriptions": [{"uri": uri, "status": status} for uri, status in self.states.items()],
            "changed_uris": list(self.changed),
            "overflow": self.overflow,
            "resync_required": self.overflow or any(value in {"lost", "ended"} for value in self.states.values()),
        }

    async def subscribe(self, client: Client, uri: str) -> dict[str, Any]:
        if self.states.get(uri) == "active":
            return {"uri": uri, "status": "active"}
        if uri not in self.states and len(self.states) >= MAX_RESOURCE_UPDATES:
            raise ToolError("MCP subscription limit reached; unsubscribe an existing resource first.")
        if client.protocol_version != "2026-07-28":
            self.states[uri] = "pending"
            try:
                await client.subscribe_resource(uri)
            except BaseException:
                self.states[uri] = "rejected"
                raise
            self.states[uri] = "active"
        else:
            previous = self.tasks.pop(uri, None)
            if previous is not None:
                previous.cancel()
                await asyncio.gather(previous, return_exceptions=True)
            ready = asyncio.get_running_loop().create_future()
            task = asyncio.create_task(self._watch(client, uri, ready))
            self.tasks[uri] = task
            try:
                await ready
            except BaseException:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.tasks.pop(uri, None)
                raise
        return {"uri": uri, "status": self.states[uri]}

    async def _watch(self, client: Client, uri: str, ready: asyncio.Future) -> None:
        try:
            async with client.listen(resource_subscriptions=[uri]) as subscription:
                if uri not in (subscription.honored.resource_subscriptions or ()):
                    self.states[uri] = "rejected"
                    ready.set_result(None)
                    return
                self.states[uri] = "active"
                ready.set_result(None)
                async for event in subscription:
                    if isinstance(event, ResourceUpdated):
                        self.record(event.uri)
                self.states[uri] = "ended"
        except asyncio.CancelledError:
            if not ready.done():
                ready.cancel()
            raise
        except Exception:
            self.states[uri] = "lost" if ready.done() else "rejected"
            if not ready.done():
                ready.set_exception(ToolError("MCP resource subscription was rejected or disconnected."))

    async def unsubscribe(self, client: Client, uri: str) -> dict[str, Any]:
        task = self.tasks.pop(uri, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        elif self.states.get(uri) == "active":
            await client.unsubscribe_resource(uri)
        self.states.pop(uri, None)
        self.changed.pop(uri, None)
        return {"uri": uri, "status": "unsubscribed"}

    async def close(self, *, preserve_updates: bool = False) -> None:
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()
        if preserve_updates:
            self.lost()
        else:
            self.states.clear()
            self.changed.clear()
