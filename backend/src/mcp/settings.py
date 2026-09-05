"""Atomic user-facing MCP server and credential settings."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace

from backend.configuration import ClientPaths, ConfigurationError, atomic_write_text

from .config import McpServerConfig, read_server_configs

KEYRING_SERVICE = "mini-agent-mcp"
_MANAGED_REFERENCE_PREFIX = f"keyring://{KEYRING_SERVICE}/"


def _account(server_name: str, environment_name: str) -> str:
    return f"{server_name}.{environment_name}"


def _reference(server_name: str, environment_name: str) -> str:
    return f"{_MANAGED_REFERENCE_PREFIX}{_account(server_name, environment_name)}"


def _managed_account(reference: str) -> str | None:
    return reference[len(_MANAGED_REFERENCE_PREFIX) :] if reference.startswith(_MANAGED_REFERENCE_PREFIX) else None


def _header_key(name: str) -> str:
    return "http." + name.lower().encode("ascii").hex()


def _references(server: McpServerConfig) -> dict[str, str]:
    return {**(server.env_refs or {}), **(server.header_refs or {})}


def _keyring_module():
    import keyring

    return keyring


def _render_servers(configs: tuple[McpServerConfig, ...]) -> str:
    lines: list[str] = []
    for server in sorted(configs, key=lambda item: item.name):
        lines.extend(
            (
                f"[servers.{server.name}]",
                f"transport = {json.dumps(server.transport)}",
                f"enabled = {'true' if server.enabled else 'false'}",
            )
        )
        if server.transport == "stdio":
            lines.append(f"command = {json.dumps(server.command, ensure_ascii=False)}")
            lines.append("args = [" + ", ".join(json.dumps(item, ensure_ascii=False) for item in server.args) + "]")
        else:
            lines.append(f"url = {json.dumps(server.url, ensure_ascii=False)}")
        if server.cwd is not None:
            lines.append(f"cwd = {json.dumps(server.cwd, ensure_ascii=False)}")
        if server.env:
            lines.append("")
            lines.append(f"[servers.{server.name}.env]")
            lines.extend(
                f"{name} = {json.dumps(value, ensure_ascii=False)}" for name, value in sorted(server.env.items())
            )
        if server.env_refs:
            lines.append("")
            lines.append(f"[servers.{server.name}.env_refs]")
            lines.extend(
                f"{name} = {json.dumps(value, ensure_ascii=False)}" for name, value in sorted(server.env_refs.items())
            )
        for field, values in (("headers", server.headers), ("header_refs", server.header_refs)):
            if values:
                lines.extend(("", f"[servers.{server.name}.{field}]"))
                lines.extend(
                    f"{json.dumps(name)} = {json.dumps(value, ensure_ascii=False)}"
                    for name, value in sorted(values.items())
                )
        lines.append("")
    return "\n".join(lines).rstrip() + ("\n" if lines else "")


class McpSettingsStore:
    """Own structured servers.toml mutations and managed keyring entries."""

    def __init__(self, paths: ClientPaths) -> None:
        self.paths = paths
        self.lock_path = paths.mcp_file.with_name(".servers.toml.lock")

    def servers(self) -> tuple[McpServerConfig, ...]:
        return read_server_configs(self.paths.mcp_file, reject_plaintext_secrets=True)

    def server(self, name: str) -> McpServerConfig:
        found = next((item for item in self.servers() if item.name == name), None)
        if found is None:
            raise ValueError("MCP server not found")
        return found

    @staticmethod
    def public_server(server: McpServerConfig) -> dict[str, object]:
        return {
            "name": server.name,
            "command": server.command,
            "args": list(server.args),
            "cwd": server.cwd,
            "env": dict(server.env or {}),
            "secret_env": [{"name": name, "configured": True} for name in sorted((server.env_refs or {}).keys())],
            "enabled": server.enabled,
            "transport": server.transport,
            "url": server.url,
            "headers": dict(server.headers or {}),
            "secret_headers": [{"name": name, "configured": True} for name in sorted(server.header_refs or {})],
        }

    def create(
        self,
        *,
        name: str,
        command: str,
        args: tuple[str, ...],
        cwd: str | None,
        env: Mapping[str, str],
        secrets: Mapping[str, str],
        enabled: bool,
        transport: str = "stdio",
        url: str | None = None,
        headers: Mapping[str, str] | None = None,
        header_secrets: Mapping[str, str] | None = None,
    ) -> McpServerConfig:
        with self._lock():
            current = list(self.servers())
            if any(item.name == name for item in current):
                raise ValueError("MCP server name already exists")
            references = {key: _reference(name, key) for key in secrets}
            overlap = set(env) & set(references)
            if overlap:
                raise ValueError("MCP environment names cannot be both plain and secret")
            header_refs = {key.lower(): _reference(name, _header_key(key)) for key in (header_secrets or {})}
            server = McpServerConfig(
                name,
                command,
                args,
                cwd,
                dict(env) or None,
                enabled,
                references or None,
                transport,
                url,
                dict(headers or {}) or None,
                header_refs or None,
            )
            sets = {
                **self._credential_sets(name, secrets),
                **self._credential_sets(
                    name, {_header_key(key): value for key, value in (header_secrets or {}).items()}
                ),
            }
            self._commit(tuple((*current, server)), credential_sets=sets)
            return server

    def update(
        self,
        name: str,
        *,
        command: str,
        args: tuple[str, ...],
        cwd: str | None,
        env: Mapping[str, str],
        secrets: Mapping[str, str],
        remove_secrets: set[str],
        enabled: bool,
        transport: str = "stdio",
        url: str | None = None,
        headers: Mapping[str, str] | None = None,
        header_secrets: Mapping[str, str] | None = None,
        remove_header_secrets: set[str] | None = None,
    ) -> McpServerConfig:
        with self._lock():
            current = list(self.servers())
            previous = next((item for item in current if item.name == name), None)
            if previous is None:
                raise ValueError("MCP server not found")
            references = dict(previous.env_refs or {}) if transport == "stdio" else {}
            for environment_name in remove_secrets:
                references.pop(environment_name, None)
            references.update({key: _reference(name, key) for key in secrets})
            overlap = set(env) & set(references)
            if overlap:
                raise ValueError("MCP environment names cannot be both plain and secret")
            header_refs = dict(previous.header_refs or {}) if transport == "streamable_http" else {}
            for key in remove_header_secrets or ():
                header_refs.pop(key.lower(), None)
            header_refs.update({key.lower(): _reference(name, _header_key(key)) for key in (header_secrets or {})})
            updated = McpServerConfig(
                name,
                command,
                args,
                cwd,
                dict(env) or None,
                enabled,
                references or None,
                transport,
                url,
                dict(headers or {}) or None,
                header_refs or None,
            )
            removed_accounts = self._removed_accounts(previous, updated)
            configs = tuple(updated if item.name == name else item for item in current)
            self._commit(
                configs,
                credential_sets={
                    **self._credential_sets(name, secrets),
                    **self._credential_sets(
                        name, {_header_key(key): value for key, value in (header_secrets or {}).items()}
                    ),
                },
                credential_deletes=removed_accounts,
            )
            return updated

    def set_enabled(self, name: str, enabled: bool) -> McpServerConfig:
        with self._lock():
            current = list(self.servers())
            previous = next((item for item in current if item.name == name), None)
            if previous is None:
                raise ValueError("MCP server not found")
            updated = replace(previous, enabled=enabled)
            self._commit(tuple(updated if item.name == name else item for item in current))
            return updated

    def delete(self, name: str) -> None:
        with self._lock():
            current = list(self.servers())
            previous = next((item for item in current if item.name == name), None)
            if previous is None:
                raise ValueError("MCP server not found")
            accounts = tuple(
                account
                for reference in _references(previous).values()
                if (account := _managed_account(reference)) is not None
            )
            self._commit(
                tuple(item for item in current if item.name != name),
                credential_deletes=accounts,
            )

    @staticmethod
    def _credential_sets(server_name: str, secrets: Mapping[str, str]) -> dict[str, str]:
        return {_account(server_name, name): value for name, value in secrets.items()}

    @staticmethod
    def _removed_accounts(previous: McpServerConfig, updated: McpServerConfig) -> tuple[str, ...]:
        retained = set(_references(updated).values())
        return tuple(
            account
            for reference in _references(previous).values()
            if reference not in retained and (account := _managed_account(reference)) is not None
        )

    def _commit(
        self,
        configs: tuple[McpServerConfig, ...],
        *,
        credential_sets: Mapping[str, str] | None = None,
        credential_deletes: tuple[str, ...] = (),
    ) -> None:
        sets = dict(credential_sets or {})
        touched = tuple(dict.fromkeys((*sets.keys(), *credential_deletes)))
        if not touched:
            atomic_write_text(self.paths.mcp_file, _render_servers(configs))
            return
        keyring = _keyring_module()
        previous: dict[str, str | None] = {}
        try:
            for account in touched:
                previous[account] = keyring.get_password(KEYRING_SERVICE, account)
            for account, value in sets.items():
                keyring.set_password(KEYRING_SERVICE, account, value)
            for account in credential_deletes:
                if previous.get(account) is not None:
                    keyring.delete_password(KEYRING_SERVICE, account)
            atomic_write_text(self.paths.mcp_file, _render_servers(configs))
        except Exception:
            for account, value in previous.items():
                try:
                    if value is None:
                        if keyring.get_password(KEYRING_SERVICE, account) is not None:
                            keyring.delete_password(KEYRING_SERVICE, account)
                    else:
                        keyring.set_password(KEYRING_SERVICE, account, value)
                except Exception:
                    pass
            raise ValueError("Unable to save MCP credentials or configuration.") from None

    @contextmanager
    def _lock(self) -> Iterator[None]:
        deadline = time.monotonic() + 10
        handle = None
        while handle is None:
            try:
                handle = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise ConfigurationError("Timed out waiting for MCP settings lock")
                time.sleep(0.02)
        try:
            yield
        finally:
            os.close(handle)
            self.lock_path.unlink(missing_ok=True)


__all__ = ["KEYRING_SERVICE", "McpSettingsStore"]
