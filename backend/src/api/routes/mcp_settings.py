"""Structured user-level MCP settings routes."""

from __future__ import annotations

from typing import Self

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from backend.mcp.client import start_external_tools
from backend.mcp.config import (
    McpServerConfig,
    McpSettings,
    sensitive_environment_name,
    valid_environment_name,
    valid_server_name,
)
from backend.mcp.settings import McpSettingsStore
from backend.tools import ToolError

router = APIRouter(prefix="/api/settings/mcp", tags=["settings"])


class EnabledPayload(BaseModel):
    enabled: StrictBool


class McpServerFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1, max_length=2000)
    args: list[str] = Field(default_factory=list, max_length=128)
    cwd: str | None = Field(default=None, max_length=2000)
    env: dict[str, str] = Field(default_factory=dict, max_length=128)
    secrets: dict[str, str] = Field(default_factory=dict, max_length=128)
    remove_secrets: list[str] = Field(default_factory=list, max_length=128)
    enabled: StrictBool = True

    @field_validator("command")
    @classmethod
    def non_empty_command(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("command must not be empty")
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
    if body.remove_secrets:
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
        server = McpServerConfig(
            saved.name,
            saved.command,
            saved.args,
            saved.cwd,
            saved.env,
            True,
            saved.env_refs,
        )
        resources = start_external_tools((server,), McpSettings.from_config(state.settings.config_store.read()))
        try:
            tools = sorted(item.spec.name for item in resources)
        finally:
            resources.close()
        return {"tools": tools, "count": len(tools)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ToolError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
