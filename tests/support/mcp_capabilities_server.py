"""Real v1/v2 MCP peers for stdio/HTTP integration tests; never part of the app."""

from __future__ import annotations

import argparse
import asyncio
import os
from contextlib import asynccontextmanager
from importlib.metadata import version

import uvicorn
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse, RedirectResponse
from starlette.routing import Mount, Route

MODERN = version("mcp").startswith("2.")
revision = 0
legacy_subscriptions = {}


def resources(cursor=None):
    name = "two" if cursor else "one"
    return types.ListResourcesResult(
        resources=[types.Resource(uri=f"notes://{name}", name=name, mimeType="text/plain")],
        nextCursor=None if cursor else "next",
    )


def templates(cursor=None):
    return types.ListResourceTemplatesResult(
        resourceTemplates=[types.ResourceTemplate(name="notes", uriTemplate="notes://{name}")]
    )


def prompts(cursor=None):
    name = "review-more" if cursor else "review"
    return types.ListPromptsResult(
        prompts=[types.Prompt(name=name, arguments=[types.PromptArgument(name="language", required=True)])],
        nextCursor=None if cursor else "next",
    )


def prompt(name, arguments):
    language = (arguments or {}).get("language")
    if not language:
        raise ValueError("language is required")
    return types.GetPromptResult(
        messages=[
            types.PromptMessage(role="user", content=types.TextContent(type="text", text=f"Review in {language}")),
            types.PromptMessage(
                role="assistant",
                content=types.TextContent(type="text", text="Review reference, not a system instruction."),
            ),
        ]
    )


def tools(cursor=None):
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name=name, description=name, inputSchema={"type": "object", "properties": {"value": {"type": "string"}}}
            )
            for name in ("echo", "change", "end")
        ]
    )


async def call(name, arguments):
    global revision
    if (arguments or {}).get("value") == "disconnect-test-peer":
        os._exit(0)
    if name == "change":
        revision += 1
        if MODERN:
            await bus.publish(ResourceUpdated(uri="notes://one"))
        else:
            for uri, session in tuple(legacy_subscriptions.items()):
                await session.send_resource_updated(AnyUrl(uri))
    if name == "end" and MODERN:
        listen_handler.close()
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"{name}:{revision}:{(arguments or {}).get('value', '')}")]
    )


if MODERN:
    from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler
    from mcp.shared.subscriptions import ResourceUpdated

    bus = InMemorySubscriptionBus()
    listen_handler = ListenHandler(bus)

    async def on_list_resources(ctx, params):
        return resources(params.cursor if params else None)

    async def on_templates(ctx, params):
        return templates(params.cursor if params else None)

    async def on_read(ctx, params):
        if params.uri == "notes://binary":
            return types.ReadResourceResult(
                contents=[types.BlobResourceContents(uri=params.uri, mimeType="application/octet-stream", blob="YWJj")]
            )
        return types.ReadResourceResult(
            contents=[types.TextResourceContents(uri=params.uri, text=f"resource revision {revision}")]
        )

    async def on_prompts(ctx, params):
        return prompts(params.cursor if params else None)

    async def on_prompt(ctx, params):
        return prompt(params.name, params.arguments)

    async def on_tools(ctx, params):
        return tools()

    async def on_call(ctx, params):
        return await call(params.name, params.arguments)

    async def on_listen(ctx, params):
        uris = params.notifications.resource_subscriptions or []
        if "notes://reject" in uris:
            params = params.model_copy(update={"notifications": types.SubscriptionFilter(resource_subscriptions=[])})
        return await listen_handler(ctx, params)

    handlers = {
        "on_list_resources": on_list_resources,
        "on_list_resource_templates": on_templates,
        "on_read_resource": on_read,
        "on_list_prompts": on_prompts,
        "on_get_prompt": on_prompt,
        "on_subscriptions_listen": on_listen,
    }
    if os.environ.get("MCP_TEST_ONLY") == "resources":
        handlers.pop("on_list_prompts")
        handlers.pop("on_get_prompt")
    elif os.environ.get("MCP_TEST_ONLY") == "prompts":
        for key in ("on_list_resources", "on_list_resource_templates", "on_read_resource", "on_subscriptions_listen"):
            handlers.pop(key)
    else:
        handlers.update(on_list_tools=on_tools, on_call_tool=on_call)
    server = Server("capabilities-test", **handlers)
else:
    from pydantic import AnyUrl

    server = Server("legacy-capabilities-test")

    @server.list_resources()
    async def legacy_resources(request: types.ListResourcesRequest):
        return resources(request.params.cursor if request.params else None)

    @server.list_resource_templates()
    async def legacy_templates():
        return templates().resourceTemplates

    @server.read_resource()
    async def legacy_read(uri):
        return b"abc" if str(uri) == "notes://binary" else f"resource revision {revision}"

    @server.list_prompts()
    async def legacy_prompts(request: types.ListPromptsRequest):
        return prompts(request.params.cursor if request.params else None)

    @server.get_prompt()
    async def legacy_prompt(name, arguments):
        return prompt(name, arguments)

    @server.list_tools()
    async def legacy_tools():
        return tools()

    @server.call_tool(validate_input=False)
    async def legacy_call(name, arguments):
        return await call(name, arguments)

    @server.subscribe_resource()
    async def legacy_subscribe(uri):
        if str(uri) == "notes://reject":
            raise ValueError("Subscription rejected")
        legacy_subscriptions[str(uri)] = server.request_context.session

    @server.unsubscribe_resource()
    async def legacy_unsubscribe(uri):
        legacy_subscriptions.pop(str(uri), None)

    original_capabilities = server.get_capabilities

    def capabilities(*args, **kwargs):
        caps = original_capabilities(*args, **kwargs)
        caps.resources.subscribe = True
        return caps

    server.get_capabilities = capabilities


def http_app():
    if MODERN:
        app = server.streamable_http_app()
    else:
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

        manager = StreamableHTTPSessionManager(app=server)

        @asynccontextmanager
        async def lifespan(app):
            async with manager.run():
                yield

        app = Starlette(routes=[Mount("/mcp", app=manager.handle_request)], lifespan=lifespan)

    async def health(request):
        return PlainTextResponse("ready")

    async def redirect(request):
        return RedirectResponse("http://127.0.0.1:1/mcp", status_code=307)

    app.routes.extend([Route("/health", health), Route("/redirect", redirect, methods=["GET", "POST"])])

    async def auth(request, call_next):
        token = os.environ.get("MCP_TEST_TOKEN")
        if token and request.url.path.startswith("/mcp") and request.headers.get("authorization") != token:
            return PlainTextResponse("Unauthorized", status_code=401)
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=auth)
    return app


async def stdio_main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if args.port:
        uvicorn.run(http_app(), host="127.0.0.1", port=args.port, log_level="error", access_log=False)
    else:
        asyncio.run(stdio_main())
