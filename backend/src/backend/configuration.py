"""Client-owned paths and TOML configuration."""

from __future__ import annotations

import json
import os
import re
import time
import tomllib
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


class ConfigurationError(ValueError):
    """The local client configuration cannot be used safely."""


_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_CONFIG_SECRET_KEY_RE = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|token|access[_-]?token|refresh[_-]?token|auth(?:orization|entication)?|credential|password|passwd|secret|private[_-]?key|cookie)(?:$|[_-])",
    re.IGNORECASE,
)


def validate_identity_id(value: str, *, require_uuid: bool = False) -> str:
    """Validate an identity directory name without accepting path syntax.

    Web identities always use the lower-case, hyphenated representation
    returned by :func:`uuid.uuid4`.  ``require_uuid=False`` is retained only
    for the standalone/offline TUI's historical device directory; it still
    rejects separators, emails and traversal components.
    """

    from uuid import UUID

    candidate = str(value or "")
    if not _SAFE_ID_RE.fullmatch(candidate) or candidate in {".", ".."}:
        raise ConfigurationError("Unsafe identity id for local data path.")
    parsed = None
    try:
        parsed = UUID(candidate)
    except (ValueError, AttributeError) as exc:
        if require_uuid:
            raise ConfigurationError("Authenticated identity id must be a UUID.") from exc
    else:
        if require_uuid and str(parsed) != candidate:
            raise ConfigurationError("Authenticated identity id must use canonical UUID syntax.")
    if require_uuid and parsed is None:
        raise ConfigurationError("Authenticated identity id must be a UUID.")
    return candidate


@dataclass(frozen=True)
class ClientPaths:
    """All mutable client data, outside the project workspace."""

    root: Path

    @classmethod
    def from_home(cls, home: Path | None = None) -> ClientPaths:
        return cls((home or Path.home()) / ".mini_agent")

    @property
    def config_file(self) -> Path:
        return self.root / "config.toml"

    @property
    def user_db(self) -> Path:
        """Canonical per-user settings database for authenticated clients."""

        return self.root / "user.db"

    @property
    def runtime_dir(self) -> Path:
        """Runtime shared by Web and TUI; no client-type directory is used."""

        return self.root / "runtime"

    @property
    def sync_dir(self) -> Path:
        return self.root / "sync"

    @property
    def sync_staging_dir(self) -> Path:
        return self.sync_dir / "staging"

    @property
    def sync_recovery_dir(self) -> Path:
        return self.sync_dir / "recovery"

    @property
    def skills_dir(self) -> Path:
        return self.root / "skills"

    @property
    def logs_dir(self) -> Path:
        """Optional TUI-only diagnostics kept outside the user snapshot tree."""

        return self.root.parent / ".mini_agent-cache" / "logs" / self.root.name

    @property
    def rag_dir(self) -> Path:
        return self.root / "rag"

    @property
    def plugins_dir(self) -> Path:
        return self.root / "plugins"

    @property
    def mcp_dir(self) -> Path:
        return self.root / "mcp"

    @property
    def mcp_file(self) -> Path:
        return self.mcp_dir / "servers.toml"

    @property
    def mcp_trust_file(self) -> Path:
        return self.mcp_dir / "trust.toml"

    @property
    def mcp_resources_dir(self) -> Path:
        return self.mcp_dir / "resources"

    def session_db(self, session_id: str) -> Path:
        path = self.session_root(session_id) / "state.db"
        if path.is_symlink():
            raise ConfigurationError("Session database cannot be a symbolic link.")
        if path.exists() and not path.is_file():
            raise ConfigurationError("Session database path must be a file.")
        return path

    def session_root(self, session_id: str) -> Path:
        if (
            not session_id
            or Path(session_id).name != session_id
            or session_id in {".", ".."}
            or not _SAFE_ID_RE.fullmatch(session_id)
        ):
            raise ConfigurationError("Unsafe session id for local data path.")
        if self.runtime_dir.is_symlink():
            raise ConfigurationError("Runtime directory cannot be a symbolic link.")
        candidate = self.runtime_dir / session_id
        if candidate.is_symlink():
            raise ConfigurationError("Session data path cannot be a symbolic link.")
        root = candidate.resolve()
        if root.parent != self.runtime_dir.resolve():
            raise ConfigurationError("Session data path must remain inside the runtime directory.")
        return root

    def session_workspace(self, session_id: str) -> Path:
        return self.session_root(session_id) / "workspace"

    def session_uploads(self, session_id: str) -> Path:
        return self.session_root(session_id) / "uploads"

    def ensure_session(self, session_id: str) -> Path:
        if self.runtime_dir.is_symlink():
            raise ConfigurationError("Runtime directory cannot be a symbolic link.")
        if self.runtime_dir.exists() and not self.runtime_dir.is_dir():
            raise ConfigurationError("Runtime path must be a directory.")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        root = self.session_root(session_id)
        if root.exists() and root.is_symlink():
            raise ConfigurationError("Session data directory cannot be a symbolic link.")
        for child in (self.session_workspace(session_id), self.session_uploads(session_id)):
            if child.is_symlink():
                raise ConfigurationError("Session payload directories cannot be symbolic links.")
            if child.exists() and not child.is_dir():
                raise ConfigurationError("Session payload path must be a directory.")
            child.mkdir(parents=True, exist_ok=True)
        # A session directory is discoverable only when its durable state file
        # exists.  SQLite initializes the schema on first open, but the path
        # contract must be complete even for callers that prepare a workspace
        # before opening the session store.
        self.session_db(session_id).touch(exist_ok=True)
        return root

    def ensure(self) -> None:
        if self.root.exists() and self.root.is_symlink():
            raise ConfigurationError("User data root cannot be a symbolic link.")
        if self.root.exists() and not self.root.is_dir():
            raise ConfigurationError("User data root must be a directory.")
        self.root.mkdir(parents=True, exist_ok=True)
        for directory in (
            self.skills_dir,
            self.rag_dir,
            self.plugins_dir,
            self.mcp_dir,
            self.mcp_resources_dir,
            self.runtime_dir,
            self.sync_dir,
            self.sync_staging_dir,
            self.sync_recovery_dir,
        ):
            if directory.is_symlink():
                raise ConfigurationError(f"User data directory cannot be a symbolic link: {directory}")
            if directory.exists() and not directory.is_dir():
                raise ConfigurationError(f"User data path must be a directory: {directory}")
            directory.mkdir(parents=True, exist_ok=True)
        for file in (self.config_file, self.user_db, self.mcp_file, self.mcp_trust_file):
            if file.is_symlink():
                raise ConfigurationError(f"User data file cannot be a symbolic link: {file}")
            if file.exists() and not file.is_file():
                raise ConfigurationError(f"User data path must be a file: {file}")
            file.touch(exist_ok=True)
        # TUI diagnostics deliberately live outside the snapshot-owned user
        # root.  Creating the directory here keeps the path usable for the
        # standalone TUI without reintroducing runtime/web or runtime/tui.
        cache_root = self.root.parent / ".mini_agent-cache"
        logs_root = cache_root / "logs"
        if cache_root.is_symlink() or (cache_root.exists() and not cache_root.is_dir()):
            raise ConfigurationError("Diagnostics cache root cannot be a symbolic link or regular file.")
        if logs_root.is_symlink() or (logs_root.exists() and not logs_root.is_dir()):
            raise ConfigurationError("Diagnostics logs path cannot be a symbolic link or regular file.")
        cache_root.mkdir(parents=True, exist_ok=True)
        logs_root.mkdir(parents=True, exist_ok=True)
        if self.logs_dir.is_symlink():
            raise ConfigurationError("Diagnostics directory cannot be a symbolic link.")
        self.logs_dir.mkdir(parents=True, exist_ok=True)


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
    """Create a safe config.toml without reading legacy files or secrets."""

    del workspace
    paths.ensure()
    # ``ClientPaths.ensure`` creates the contract files up front so callers
    # can rely on their presence.  An empty TOML file is still an
    # uninitialized store and must receive the safe defaults below; otherwise
    # a first standalone-TUI launch would end up with no model/runtime tables.
    if paths.config_file.exists() and paths.config_file.stat().st_size > 0:
        values = load_config(paths.config_file)
        # Web identities use user.db for provider credentials.  Sanitize an
        # existing account TOML even when a caller reaches the composition
        # root without first going through ``user_paths``.  The root-level
        # standalone TUI keeps its historical model.toml compatibility.
        if _is_authenticated_user_root(paths.root):
            values = UserConfigStore(paths.config_file).ensure_defaults({})
        return _ensure_device_id(paths, values)
    config: dict[str, dict[str, object]] = {
        "model": {
            "provider": "deepseek",
            "protocol": "chat_completions",
            "base_url": "",
            "model": "",
            "max_tokens": 8192,
            "context_size": 1_024_000,
            "tokenizer_model": "deepseek-ai/DeepSeek-V3",
        },
        "runtime": {"log_full_messages": True},
        "capabilities": {"skills": True, "rag": False, "plugins": False, "mcp": False},
        "sync": {"auto_save_enabled": False, "auto_save_rule": "idle_5m"},
    }
    _atomic_write(paths.config_file, _to_toml(config))
    return _ensure_device_id(paths, load_config(paths.config_file))


def section(values: Mapping[str, object], name: str) -> Mapping[str, object]:
    item = values.get(name, {})
    if not isinstance(item, dict):
        raise ConfigurationError(f"[{name}] must be a table.")
    return item


def _to_toml(values: Mapping[str, Mapping[str, object]]) -> str:
    lines: list[str] = []

    def render(value: object) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        if isinstance(value, float):
            return repr(value)
        if isinstance(value, list):
            return "[" + ", ".join(render(item) for item in value) + "]"
        return json.dumps(str(value), ensure_ascii=False)

    def emit_table(name: str, entries: Mapping[str, object]) -> None:
        scalars = [(key, value) for key, value in entries.items() if not isinstance(value, Mapping)]
        nested = [(key, value) for key, value in entries.items() if isinstance(value, Mapping)]
        if name:
            lines.append(f"[{name}]")
        for key, value in scalars:
            if isinstance(key, str):
                lines.append(f"{key} = {render(value)}")
        if name or scalars:
            lines.append("")
        for key, value in nested:
            if isinstance(key, str) and isinstance(value, Mapping):
                emit_table(f"{name}.{key}" if name else key, value)

    for table, entries in values.items():
        if isinstance(table, str) and isinstance(entries, Mapping):
            emit_table(table, entries)
    return "\n".join(lines).rstrip() + "\n"


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(path: Path, content: str) -> None:
    """Atomically replace one client-owned UTF-8 text file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, content)


class UserConfigStore:
    """Small, dependency-free, cross-process TOML store for one user root."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_name(f".{path.name}.lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def read(self) -> dict[str, object]:
        if not self.path.exists():
            return {}
        return _strip_config_secrets(load_config(self.path))

    def update(self, patch: Mapping[str, Mapping[str, object]]) -> dict[str, object]:
        with self._lock():
            raw_current = load_config(self.path) if self.path.exists() else {}
            current = _strip_config_secrets(raw_current)
            merged = _strip_config_secrets(_merge_tables(current, patch))
            if merged != raw_current:
                atomic_write_text(self.path, _to_toml(merged))
            return merged

    def ensure_defaults(self, defaults: Mapping[str, Mapping[str, object]]) -> dict[str, object]:
        with self._lock():
            raw_current = load_config(self.path) if self.path.exists() else {}
            current = _strip_config_secrets(raw_current)
            merged = _strip_config_secrets(_merge_tables(defaults, current))
            if merged != raw_current:
                atomic_write_text(self.path, _to_toml(merged))
            return merged

    @contextmanager
    def _lock(self):
        deadline = time.monotonic() + 10
        handle = None
        while handle is None:
            try:
                handle = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise ConfigurationError(f"Timed out waiting for config lock: {self.path}")
                time.sleep(0.02)
        try:
            yield
        finally:
            os.close(handle)
            self.lock_path.unlink(missing_ok=True)


def _merge_tables(base: Mapping[str, object], overlay: Mapping[str, object]) -> dict[str, object]:
    merged: dict[str, object] = {key: value for key, value in base.items()}
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _merge_tables(merged[key], value)  # type: ignore[arg-type]
        else:
            merged[key] = value
    return merged


def _strip_config_secrets(value: Mapping[str, object]) -> dict[str, object]:
    """Remove values that must never be persisted in a user TOML file.

    TOML remains the source of truth for preferences, not credentials.  The
    recursive filter also protects forward-compatible nested tables added by
    extensions; explicit ``*_ref`` fields are retained because they are
    references to an external secret store rather than secret material.
    """

    def clean(item: object) -> object:
        if isinstance(item, Mapping):
            return {str(key): clean(entry) for key, entry in item.items() if not _is_config_secret_key(str(key))}
        if isinstance(item, list):
            return [clean(entry) for entry in item]
        return item

    cleaned = clean(value)
    return cleaned if isinstance(cleaned, dict) else {}


def _is_config_secret_key(key: str) -> bool:
    """Identify secret-bearing fields while preserving external references."""

    lowered = key.casefold()
    if lowered.endswith(("_ref", "_reference")):
        return False
    return _CONFIG_SECRET_KEY_RE.search(key) is not None


def _is_authenticated_user_root(path: Path) -> bool:
    """Return whether a config belongs to the canonical UUID user tree."""

    try:
        validate_identity_id(Path(path).name, require_uuid=True)
    except ConfigurationError:
        return False
    return True


def _ensure_device_id(paths: ClientPaths, values: dict[str, object]) -> dict[str, object]:
    sync = dict(section(values, "sync"))
    if isinstance(sync.get("device_id"), str) and sync["device_id"]:
        return values
    if _is_authenticated_user_root(paths.root):
        return UserConfigStore(paths.config_file).ensure_defaults({"sync": {"device_id": f"web_{paths.root.name}"}})
    sync["device_id"] = f"device_{uuid4().hex}"
    normalized = {name: dict(value) for name, value in values.items() if isinstance(value, dict)}
    normalized["sync"] = sync
    _atomic_write(paths.config_file, _to_toml(normalized))
    return load_config(paths.config_file)
