"""Structured user-level MCP settings routes."""

from __future__ import annotations

from dataclasses import replace
from typing import Self

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from backend.mcp.client import start_external_tools
from backend.mcp.config import (
    McpServerConfig,
    McpSettings,
    sensitive_environment_name,
    valid_environment_name,
    valid_header_name,
    valid_server_name,
    validate_headers,
)
from backend.mcp.settings import McpSettingsStore
from backend.tools import ToolError


class SecretSafeMcpRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def route(request: Request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": [
                            {"loc": error["loc"], "msg": error["msg"], "type": error["type"]} for error in exc.errors()
                        ]
                    },
                )

        return route


router = APIRouter(prefix="/api/settings/mcp", tags=["settings"], route_class=SecretSafeMcpRoute)


class EnabledPayload(BaseModel):
    enabled: StrictBool


class McpServerFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(default="", max_length=2000)
    args: list[str] = Field(default_factory=list, max_length=128)
    cwd: str | None = Field(default=None, max_length=2000)
    env: dict[str, str] = Field(default_factory=dict, max_length=128)
    secrets: dict[str, str] = Field(default_factory=dict, max_length=128)
    remove_secrets: list[str] = Field(default_factory=list, max_length=128)
    enabled: StrictBool = True
    transport: str = "stdio"
    url: str | None = Field(default=None, max_length=2000)
    headers: dict[str, str] = Field(default_factory=dict, max_length=128)
    header_secrets: dict[str, str] = Field(default_factory=dict, max_length=128)
    remove_header_secrets: list[str] = Field(default_factory=list, max_length=128)

    @field_validator("command")
    @classmethod
    def non_empty_command(cls, value: str) -> str:
        return value.strip()

    @field_validator("args")
    @classmethod
    def valid_args(cls, value: list[str]) -> list[str]:
        if any(len(item) > 2000 for item in value):
            raise ValueError("MCP arguments must not exceed 2000 characters")
        return value

    @field_validator("cwd")
    @classmethod
    def normalized_cwd(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @field_validator("env", "secrets")
    @classmethod
    def valid_environment(cls, value: dict[str, str]) -> dict[str, str]:
        for name, item in value.items():
            if not valid_environment_name(name):
                raise ValueError("MCP environment names are invalid")
            if len(item) > 4096:
                raise ValueError("MCP environment values must not exceed 4096 characters")
        return value

    @field_validator("remove_secrets")
    @classmethod
    def valid_removed_environment(cls, value: list[str]) -> list[str]:
        if any(not valid_environment_name(name) for name in value):
            raise ValueError("MCP environment names are invalid")
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def separated_environment(self) -> Self:
        try:
            validate_headers(self.headers, {name: "env://MCP_HEADER" for name in self.header_secrets})
            for name, value in self.header_secrets.items():
                if len(value) > 4096 or "\r" in value or "\n" in value:
                    raise ToolError("Invalid MCP header secret value.")
            if any(not valid_header_name(name) for name in self.remove_header_secrets):
                raise ToolError("Invalid removed MCP header name.")
            McpServerConfig(
                "validation",
                self.command,
                tuple(self.args),
                self.cwd,
                self.env,
                self.enabled,
                {name: "env://MCP_SECRET" for name in self.secrets},
                self.transport,
                self.url,
                self.headers,
                {name: "env://MCP_HEADER" for name in self.header_secrets},
            )
            if self.transport == "stdio" and self.remove_header_secrets:
                raise ToolError("stdio cannot include HTTP fields.")
            if self.transport == "streamable_http" and self.remove_secrets:
                raise ToolError("HTTP cannot include environment fields.")
        except ToolError as exc:
            raise ValueError(str(exc)) from exc
        overlap = set(self.env) & (set(self.secrets) | set(self.remove_secrets))
        if overlap:
            raise ValueError("MCP environment names cannot be both plain and secret")
        if any(sensitive_environment_name(name) for name in self.env):
            raise ValueError("Sensitive MCP environment values must use the credential vault")
        return self


class McpServerCreatePayload(McpServerFields):
    name: str = Field(min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not valid_server_name(value):
            raise ValueError("MCP server name must use letters, digits, '_' or '-'")
        return value


def _state(request: Request):
    return request.app.state.web


def _store(request: Request) -> McpSettingsStore:
    return McpSettingsStore(_state(request).paths)


def _payload(request: Request) -> dict[str, object]:
    state = _state(request)
    try:
        servers = _store(request).servers()
    except ToolError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "enabled": bool(state.settings.capability_config()["mcp"]),
        "servers": [McpSettingsStore.public_server(item) for item in servers],
    }


def _server_values(body: McpServerFields) -> dict[str, object]:
    return {
        "command": body.command,
        "args": tuple(body.args),
        "cwd": body.cwd,
        "env": body.env,
        # Password inputs are intentionally blank when editing an existing
        # credential.  Only non-empty values replace the keyring entry.
        "secrets": {name: value for name, value in body.secrets.items() if value},
        "enabled": body.enabled,
        "transport": body.transport,
        "url": body.url,
        "headers": {name.lower(): value for name, value in body.headers.items()},
        "header_secrets": {name.lower(): value for name, value in body.header_secrets.items() if value},
    }


@router.get("")
def get_mcp_settings(request: Request) -> dict[str, object]:
    return _payload(request)


@router.put("/enabled")
def update_mcp_enabled(body: EnabledPayload, request: Request) -> dict[str, object]:
    try:
        _state(request).settings.update_capability_config({"mcp": body.enabled})
        return _payload(request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/servers", status_code=201)
def create_mcp_server(body: McpServerCreatePayload, request: Request) -> dict[str, object]:
    if body.remove_secrets or body.remove_header_secrets:
        raise HTTPException(status_code=422, detail="New MCP servers cannot remove credentials")
    try:
        created = _store(request).create(name=body.name, **_server_values(body))
        return McpSettingsStore.public_server(created)
    except (ToolError, ValueError) as exc:
        raise HTTPException(status_code=409 if "already exists" in str(exc) else 422, detail=str(exc)) from exc


@router.put("/servers/{name}")
def update_mcp_server(name: str, body: McpServerFields, request: Request) -> dict[str, object]:
    if not valid_server_name(name):
        raise HTTPException(status_code=404, detail="MCP server not found")
    try:
        updated = _store(request).update(
            name,
            remove_secrets=set(body.remove_secrets),
            remove_header_secrets=set(body.remove_header_secrets),
            **_server_values(body),
        )
        return McpSettingsStore.public_server(updated)
    except (ToolError, ValueError) as exc:
        raise HTTPException(status_code=404 if "not found" in str(exc) else 422, detail=str(exc)) from exc


@router.put("/servers/{name}/enabled")
def update_mcp_server_enabled(name: str, body: EnabledPayload, request: Request) -> dict[str, object]:
    try:
        updated = _store(request).set_enabled(name, body.enabled)
        return McpSettingsStore.public_server(updated)
    except (ToolError, ValueError) as exc:
        raise HTTPException(status_code=404 if "not found" in str(exc) else 422, detail=str(exc)) from exc


@router.delete("/servers/{name}", status_code=204)
def delete_mcp_server(name: str, request: Request) -> Response:
    try:
        _store(request).delete(name)
    except (ToolError, ValueError) as exc:
        raise HTTPException(status_code=404 if "not found" in str(exc) else 422, detail=str(exc)) from exc
    return Response(status_code=204)


@router.post("/servers/{name}/test")
def test_mcp_server(name: str, request: Request) -> dict[str, object]:
    state = _state(request)
    try:
        saved = _store(request).server(name)
        server = replace(saved, enabled=True)
        resources = start_external_tools((server,), McpSettings.from_config(state.settings.config_store.read()))
        try:
            tools = sorted(f"mcp_{name}_{item.name}" for item in resources.manager.definitions[name])
            details = resources.manager.describe(name)
        finally:
            resources.close()
        return {"tools": tools, "count": details["counts"]["tools"], **details}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ToolError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
