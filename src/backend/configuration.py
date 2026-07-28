"""Client-owned paths and TOML configuration."""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


class ConfigurationError(ValueError):
    """The local client configuration cannot be used safely."""


@dataclass(frozen=True)
class ClientPaths:
    """All mutable client data, outside the project workspace."""

    root: Path

    @classmethod
    def from_home(cls, home: Path | None = None) -> ClientPaths:
        return cls((home or Path.home()) / "mini_agent")

    @property
    def config_file(self) -> Path:
        return self.root / "config.toml"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def skills_dir(self) -> Path:
        return self.root / "skills"

    @property
    def mcp_file(self) -> Path:
        return self.root / "mcp.toml"

    @property
    def mcp_trust_file(self) -> Path:
        return self.root / "mcp-trust.toml"

    def session_db(self, session_id: str) -> Path:
        if not session_id or Path(session_id).name != session_id:
            raise ConfigurationError("Unsafe session id for local data path.")
        return self.root / session_id / "state.db"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(exist_ok=True)
        self.skills_dir.mkdir(exist_ok=True)


def load_config(path: Path) -> dict[str, object]:
    """Load TOML without reading `.env` or process environment values."""

    try:
        with path.open("rb") as handle:
            values = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Configuration file not found: {path}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"Invalid configuration {path}: {exc}") from exc
    if not isinstance(values, dict):
        raise ConfigurationError("TOML root must be a table.")
    return values


def initialize_config(paths: ClientPaths, workspace: Path) -> dict[str, object]:
    """Create config.toml once, atomically migrating the legacy workspace .env."""

    paths.ensure()
    if paths.config_file.exists():
        return _ensure_device_id(paths, load_config(paths.config_file))
    legacy = workspace / ".env"
    config = _convert_legacy_env(_read_env(legacy) if legacy.exists() else {})
    _atomic_write(paths.config_file, _to_toml(config))
    parsed = _ensure_device_id(paths, load_config(paths.config_file))
    if legacy.exists():
        legacy.unlink()
    return parsed


def section(values: Mapping[str, object], name: str) -> Mapping[str, object]:
    item = values.get(name, {})
    if not isinstance(item, dict):
        raise ConfigurationError(f"[{name}] must be a table.")
    return item


def _read_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip():
            result[key.strip()] = value.strip().strip("\"'")
    return result


def _convert_legacy_env(values: Mapping[str, str]) -> dict[str, dict[str, object]]:
    def number(name: str, default: int) -> int:
        try:
            return int(values.get(name, str(default)))
        except ValueError:
            return default

    result: dict[str, dict[str, object]] = {
        "model": {
            "api_key": values.get("API_KEY", ""),
            "base_url": values.get("BASE_URL", ""),
            "model": values.get("MODEL", ""),
            "provider": values.get("PROVIDER", "deepseek"),
            "max_tokens": number("MAX_TOKENS", 8192),
            "context_size": number("CONTEXT_SIZE", 1_024_000),
            "tokenizer_model": values.get("TOKENIZER_MODEL", "deepseek-ai/DeepSeek-V3"),
        },
        "runtime": {"log_full_messages": values.get("LOG_FULL_MESSAGES", "true").lower() == "true"},
    }
    sync = {"url": values[name] for name in ("SYNC_URL",) if values.get(name)}
    if values.get("SYNC_TOKEN"):
        sync["token"] = values["SYNC_TOKEN"]
    if sync:
        result["sync"] = sync
    return result


def _to_toml(values: Mapping[str, Mapping[str, object]]) -> str:
    lines: list[str] = []
    for table, entries in values.items():
        lines.append(f"[{table}]")
        for key, value in entries.items():
            rendered = (
                "true"
                if value is True
                else "false"
                if value is False
                else str(value)
                if isinstance(value, int)
                else json.dumps(str(value), ensure_ascii=False)
            )
            lines.append(f"{key} = {rendered}")
        lines.append("")
    return "\n".join(lines)


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(path: Path, content: str) -> None:
    """Atomically replace one client-owned UTF-8 text file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, content)


def _ensure_device_id(paths: ClientPaths, values: dict[str, object]) -> dict[str, object]:
    sync = dict(section(values, "sync"))
    if isinstance(sync.get("device_id"), str) and sync["device_id"]:
        return values
    sync["device_id"] = f"device_{uuid4().hex}"
    normalized = {name: dict(value) for name, value in values.items() if isinstance(value, dict)}
    normalized["sync"] = sync
    _atomic_write(paths.config_file, _to_toml(normalized))
    return load_config(paths.config_file)
