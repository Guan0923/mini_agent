"""Isolated client state, config seeding, workspace materialization, and MCP pre-trust.

The benchmark never touches the developer's real ``~/mini_agent`` directory. All
client-owned data (config, logs, skills, session stores, MCP trust) lives under
one sandbox root, seeded from a copy of the user's ``config.toml`` so the real
model credentials are reused without mutating anything outside the sandbox.
"""

from __future__ import annotations

import json
import ntpath
import shutil
import sys
import time
import tomllib
from pathlib import Path

from backend.configuration import ClientPaths, atomic_write_text
from backend.providers import ModelConfig

from .model import BenchmarkTask, SeedMcp

FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures"


def _read_toml(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _to_toml(values: dict[str, dict]) -> str:
    """Serialize flat tables of scalars to TOML (same shape as client config)."""
    lines: list[str] = []
    for table, entries in values.items():
        lines.append(f"[{table}]")
        for key, value in entries.items():
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, int):
                rendered = str(value)
            else:
                rendered = json.dumps(str(value), ensure_ascii=False)
            lines.append(f"{key} = {rendered}")
        lines.append("")
    return "\n".join(lines)


class Sandbox:
    """One isolated client-data root shared by all runs in a benchmark session."""

    def __init__(
        self,
        root: Path,
        source_config: Path | None = None,
        *,
        model_config: ModelConfig | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.paths = ClientPaths(self.root / "mini_agent")
        self.source_config = source_config
        self.model_config = model_config
        self.workspaces_dir = self.root / "workspaces"

    def prepare(self) -> ClientPaths:
        """Create the client directories and seed config.toml once."""
        self.paths.ensure()
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)
        self._seed_config()
        return self.paths

    def _seed_config(self) -> None:
        values = (
            _read_toml(self.source_config) if self.source_config is not None and self.source_config.exists() else {}
        )
        # Benchmarks need model/runtime/tool settings, but must never copy
        # browser/email configuration or synchronization credentials into a
        # per-user sandbox.
        allowed = {"model", "runtime", "mcp", "skills", "subagents"}
        normalized = {
            name: {
                key: item for key, item in value.items() if isinstance(key, str) and isinstance(item, (str, int, bool))
            }
            for name, value in values.items()
            if name in allowed and isinstance(value, dict)
        }
        # Pre-write an independent device id so initialize_config does no
        # mid-run writes and no sync coordinator can start in the sandbox.
        normalized["sync"] = {"device_id": f"bench_{int(time.time())}_{id(self)}"}
        runtime = dict(normalized.get("runtime", {}))
        runtime["log_full_messages"] = True
        normalized["runtime"] = runtime
        atomic_write_text(self.paths.config_file, _to_toml(normalized))

    def materialize_workspace(self, task: BenchmarkTask) -> Path:
        """Copy a validated fixture and seed task-owned client files into a fresh directory."""
        workspace = self.workspaces_dir / f"{task.name}-{time.time_ns()}"
        workspace.mkdir(parents=True, exist_ok=False)
        if task.seed.fixture is not None:
            fixture = self._fixture_path(task.seed.fixture)
            shutil.copytree(fixture, workspace, dirs_exist_ok=True)
        for seed_file in task.seed.files:
            path = self._workspace_path(workspace, seed_file.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(seed_file.content, encoding="utf-8")
        for skill in task.seed.skills:
            self._write_skill(workspace, skill.name, skill.description, skill.instructions)
        if task.seed.mcp is not None:
            self._write_mcp_file(workspace, task.seed.mcp)
        return workspace

    @staticmethod
    def _fixture_path(name: str) -> Path:
        """Resolve a fixture name without allowing an absolute/path traversal escape."""
        raw = Path(name)
        if not name or raw.is_absolute() or ntpath.isabs(name):
            raise ValueError(f"fixture path must be a non-empty relative directory: {name!r}")
        candidate = (FIXTURES_ROOT / name).resolve()
        if candidate == FIXTURES_ROOT or FIXTURES_ROOT not in candidate.parents:
            raise ValueError(f"fixture path escapes benchmark fixtures: {name!r}")
        if not candidate.is_dir():
            raise FileNotFoundError(f"benchmark fixture not found: {name}")
        return candidate

    @staticmethod
    def _workspace_path(workspace: Path, relative: str) -> Path:
        if not relative or Path(relative).is_absolute() or ntpath.isabs(relative):
            raise ValueError(f"seed path must be a non-empty relative path: {relative!r}")
        candidate = (workspace / relative).resolve()
        if candidate != workspace and workspace not in candidate.parents:
            raise ValueError(f"seed path escapes task workspace: {relative!r}")
        return candidate

    @staticmethod
    def _write_skill(workspace: Path, name: str, description: str, instructions: str) -> None:
        manifest = workspace / ".mini_agent" / "skills" / name / "SKILL.md"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n{instructions}\n",
            encoding="utf-8",
        )

    def _write_mcp_file(self, workspace: Path, seed: SeedMcp) -> None:
        if seed.profile not in {"retail", "airline"}:
            raise ValueError(f"unsupported benchmark MCP profile: {seed.profile!r}")
        server_path = Path(__file__).resolve().parent / "mcp" / "mock_server.py"
        mcp_dir = workspace / ".mini_agent"
        mcp_dir.mkdir(parents=True, exist_ok=True)
        server_args = [str(server_path), "--profile", seed.profile]
        if seed.tools:
            server_args.extend(("--tools", *seed.tools))
        (mcp_dir / "mcp.toml").write_text(
            f"[servers.{seed.server_name}]\n"
            f"command = {json.dumps(sys.executable)}\n"
            f"args = {json.dumps(server_args)}\n"
            f"cwd = {json.dumps(str(workspace))}\n",
            encoding="utf-8",
        )


def activate_client_paths(paths: ClientPaths) -> None:
    """Redirect the application factory's client paths into the sandbox."""
    import backend.runtime.application.factory as factory

    factory.client_paths = lambda: paths


def trust_project_mcp(paths: ClientPaths, workspace: Path) -> None:
    """Pre-trust a project MCP file so build_application does not refuse it."""
    from backend.mcp.config import McpTrustStore, prepare_mcp_plan

    plan = prepare_mcp_plan(paths, workspace)
    store = McpTrustStore(paths.mcp_trust_file)
    if plan.has_project_config and not store.is_trusted(plan):
        store.trust(plan)
