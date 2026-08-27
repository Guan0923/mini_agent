"""Shared single-user API runtime state for the local backend."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from threading import RLock

from backend.configuration import ClientPaths
from backend.domain.terminal import TERMINAL_LABELS
from backend.jobs import JobRegistry
from backend.sandbox import WindowsBrokerClient
from backend.storage.local_settings import LocalSettingsStore
from backend.storage.projects import ProjectStore
from backend.tools.terminal import available_terminal_executables, effective_terminal_type

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_DATA_ROOT = Path.home() / ".mini_agent"


class WebAppState:
    """Process-owned state for one local Mini-Agent installation."""

    def __init__(
        self,
        data_root: Path = DEFAULT_DATA_ROOT,
        *,
        project_picker: Callable[[], Path | None] | None = None,
        job_registry: JobRegistry | None = None,
        sandbox_broker: WindowsBrokerClient | None = None,
    ) -> None:
        root = Path(data_root)
        if root.is_symlink():
            raise ValueError("Local data root cannot be a symbolic link.")
        self.data_root = root.resolve()
        self.paths = ClientPaths(self.data_root)
        self.paths.ensure()
        self.settings = LocalSettingsStore(self.paths.state_db, self.paths.config_file)
        self.projects = ProjectStore(self.paths.projects_db)
        self.chat_workspace = self.paths.runtime_dir
        self.benchmark_root = self.data_root.parent / ".mini_agent-cache" / "benchmark"
        self.project_picker = project_picker
        self.job_registry = job_registry or JobRegistry()
        self.sandbox_broker = sandbox_broker or WindowsBrokerClient.from_system()
        self.system_job_scope = self.job_registry.root_scope()

        self.active_runtime_configs: dict[str, dict[str, object]] = {}
        self.active_runtime_bridges: dict[str, object] = {}
        self.active_turn_streams: dict[str, object] = {}
        self.active_turn_steering: dict[str, object] = {}
        self.active_turn_streams_lock = RLock()
        self.active_runtime_config_locks: dict[str, RLock] = {}

    def session_workspace(self, session_id: str) -> Path:
        """Resolve the effective cwd for a session and validate project access."""

        bound = self.projects.session_project(session_id)
        if bound is None:
            self.paths.ensure_session(session_id)
            return self.paths.session_workspace(session_id)
        if bound.removed_at is not None:
            raise RuntimeError("项目已移除，请从回收站恢复后再运行。")
        path = Path(bound.cwd)
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_dir():
                raise RuntimeError("项目 cwd 不可访问，请恢复文件夹后重试。")
            return resolved
        except (OSError, RuntimeError) as exc:
            raise RuntimeError("项目 cwd 不可访问，请恢复文件夹后重试。") from exc

    def copy_session_files(self, source_session_id: str, target_session_id: str) -> None:
        from .user_data import copy_session_files

        copy_session_files(self.paths, source_session_id, target_session_id)

    def copy_session_uploads(self, source_session_id: str, target_session_id: str) -> None:
        from .user_data import copy_session_uploads

        copy_session_uploads(self.paths, source_session_id, target_session_id)

    def benchmark_data_root(self) -> Path:
        cache_root = self.benchmark_root.parent
        if cache_root.is_symlink() or (cache_root.exists() and not cache_root.is_dir()):
            raise ValueError("Benchmark cache root cannot be a symbolic link or regular file.")
        if self.benchmark_root.is_symlink() or (self.benchmark_root.exists() and not self.benchmark_root.is_dir()):
            raise ValueError("Benchmark directory must be a regular directory.")
        self.benchmark_root.mkdir(parents=True, exist_ok=True)
        return self.benchmark_root

    def settings_payload(self) -> dict[str, object]:
        result = self.settings.settings()
        if os.name == "nt":
            available = available_terminal_executables(is_windows=True)
            runtime = result.get("runtime_config")
            current = dict(runtime) if isinstance(runtime, dict) else {}
            requested = current.get("terminal_type", "cmd")
            effective = effective_terminal_type(requested, is_windows=True)
            notice: str | None = None
            if not available:
                notice = "未检测到当前系统可用的终端。"
            elif effective != requested:
                notice = "已保存的终端当前不可用，本次已回退到可用终端。"
            current["terminal_type"] = effective
            result["runtime_config"] = current
            result["terminal_options"] = [{"value": name, "label": TERMINAL_LABELS[name]} for name in available]
            result["terminal_notice"] = notice
        else:
            result["terminal_options"] = []
            result["terminal_notice"] = None
        return result

    def model_config(self, provider_name: str | None = None):
        return self.settings.model_config(provider_name)

    def agent_config(self) -> dict[str, object]:
        return self.settings.agent_config()

    def agent_preferences(self) -> str:
        return self.settings.agent_preferences()

    def runtime_config(self) -> dict[str, object]:
        return {"runtime": self.settings.runtime_config()}

    def close(self) -> None:
        self.job_registry.close_all(reason="web application closed", timeout=5.0)
        closed: set[int] = set()
        for resource in (self.settings, self.projects):
            if id(resource) in closed:
                continue
            closed.add(id(resource))
            close = getattr(resource, "close", None)
            if callable(close):
                close()


__all__ = ["DEFAULT_DATA_ROOT", "WebAppState"]
