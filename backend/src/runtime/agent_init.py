"""Create a bounded, deterministic project-root ``AGENTS.md`` starter file."""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MAX_CONFIG_BYTES = 1024 * 1024
_PACKAGE_SCRIPT_ORDER = ("lint", "typecheck", "test", "build")


class AgentInitError(RuntimeError):
    """Raised when a project ``AGENTS.md`` cannot be created safely."""


@dataclass(frozen=True)
class AgentInitResult:
    """Public result for one successful project initialization."""

    path: str
    content: str
    byte_count: int


def initialize_project_agents(project_root: Path) -> AgentInitResult:
    """Atomically create ``AGENTS.md`` at one validated project root.

    Existing paths, including symbolic links and directories, are never
    overwritten. The generated starter is derived only from bounded project
    metadata and package-script names; it does not execute project code.
    """

    root = _resolve_project_root(project_root)
    target = root / "AGENTS.md"
    if target.is_symlink() or target.exists():
        raise AgentInitError("项目根目录已存在 AGENTS.md，未进行覆盖。")
    content = render_project_agents_template(root)
    encoded = content.encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        descriptor = os.open(target, flags, 0o600)
    except FileExistsError as exc:
        raise AgentInitError("项目根目录已存在 AGENTS.md，未进行覆盖。") from exc
    except OSError as exc:
        raise AgentInitError("无法在当前项目根目录创建 AGENTS.md。") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
    except OSError as exc:
        # The file was exclusively created but could not be completed. Keep
        # the failure explicit and avoid a second, potentially destructive
        # cleanup operation against a path that another actor could replace.
        raise AgentInitError("AGENTS.md 已创建，但写入初始内容失败，请检查文件权限。") from exc
    return AgentInitResult(path="AGENTS.md", content=content, byte_count=len(encoded))


def render_project_agents_template(project_root: Path) -> str:
    """Render a conservative starter from bounded, read-only project signals."""

    root = _resolve_project_root(project_root)
    signals: list[str] = []
    commands: list[str] = []
    _append_python_guidance(root, signals, commands)
    _append_package_guidance(root, signals, commands)

    signal_lines = [f"- {item}" for item in signals] or ["- 未识别到标准构建清单；请根据实际项目补充。"]
    command_lines = [f"- `{command}`" for command in commands] or ["- 请补充项目的格式检查、测试和构建命令。"]
    project_name = _safe_project_name(root.name)
    rendered_signals = "\n".join(signal_lines)
    rendered_commands = "\n".join(command_lines)
    return (
        f"# {project_name} 仓库协作规范\n\n"
        "## 适用范围\n\n"
        "本文件适用于整个项目。Mini-Agent 只发现项目根目录的 `AGENTS.md`，"
        "不会合并 `AGENTS.override.md` 或子目录中的同名文件。\n\n"
        "## 开发原则\n\n"
        "- 修改前检查相关源码、配置和测试，以当前实现为准。\n"
        "- 保留已有修改，只处理当前任务范围内的文件。\n"
        "- 遵循现有模块边界、命名、类型标注和错误处理方式。\n"
        "- 不写入密钥、令牌、Cookie、真实用户数据或完整环境变量。\n"
        "- 未经明确要求，不创建提交、不推送、不启动长期服务。\n\n"
        "## 检测到的项目结构\n\n"
        f"{rendered_signals}\n\n"
        "## 验证\n\n"
        "按变更风险运行最小但有代表性的检查：\n\n"
        f"{rendered_commands}\n\n"
        "测试失败时先定位根因，不要通过删除测试、放宽断言或隐藏错误来绕过失败。\n"
    )


def _resolve_project_root(project_root: Path) -> Path:
    candidate = Path(project_root)
    try:
        if candidate.is_symlink():
            raise AgentInitError("项目根目录不能是符号链接。")
        resolved = candidate.resolve(strict=True)
    except AgentInitError:
        raise
    except OSError as exc:
        raise AgentInitError("当前项目根目录不可访问。") from exc
    if not resolved.is_dir():
        raise AgentInitError("当前项目根路径不是目录。")
    return resolved


def _append_python_guidance(root: Path, signals: list[str], commands: list[str]) -> None:
    pyproject = _load_toml(root / "pyproject.toml")
    if pyproject is None:
        return
    signals.append("Python 项目：`pyproject.toml`")
    prefix = "uv run " if (root / "uv.lock").is_file() else ""
    tool = pyproject.get("tool")
    tool_config = tool if isinstance(tool, dict) else {}
    if isinstance(tool_config.get("ruff"), dict):
        commands.extend((f"{prefix}python -m ruff check .", f"{prefix}python -m ruff format --check ."))
    if (root / "tests").is_dir() or isinstance(tool_config.get("pytest"), dict):
        commands.append(f"{prefix}python -m pytest -q")


def _append_package_guidance(root: Path, signals: list[str], commands: list[str]) -> None:
    candidates = [root]
    children: list[Path] = []
    try:
        for child in root.iterdir():
            if len(children) >= 128:
                break
            if not child.is_symlink() and child.is_dir() and _safe_package_directory_name(child.name):
                children.append(child)
    except OSError:
        pass
    children.sort(key=lambda child: child.name.casefold())
    candidates.extend(child for child in children if (child / "package.json").is_file())
    seen: set[Path] = set()
    for directory in candidates:
        manifest = directory / "package.json"
        if not manifest.is_file() or manifest.is_symlink() or manifest in seen:
            continue
        seen.add(manifest)
        payload = _load_json(manifest)
        if payload is None:
            continue
        relative = "." if directory == root else directory.relative_to(root).as_posix()
        display = "`package.json`" if relative == "." else f"`{relative}/package.json`"
        signals.append(f"JavaScript/TypeScript 包：{display}")
        scripts = payload.get("scripts")
        if not isinstance(scripts, dict):
            continue
        manager = _package_manager(directory)
        prefix = "" if relative == "." else f"cd {relative}; "
        for name in _PACKAGE_SCRIPT_ORDER:
            if isinstance(scripts.get(name), str):
                commands.append(f"{prefix}{manager} run {name}")


def _load_toml(path: Path) -> dict[str, Any] | None:
    raw = _read_bounded(path)
    if raw is None:
        return None
    try:
        value = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _load_json(path: Path) -> dict[str, Any] | None:
    raw = _read_bounded(path)
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _read_bounded(path: Path) -> bytes | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_CONFIG_BYTES + 1)
    except OSError:
        return None
    return raw if len(raw) <= _MAX_CONFIG_BYTES else None


def _package_manager(directory: Path) -> str:
    if (directory / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (directory / "yarn.lock").is_file():
        return "yarn"
    if (directory / "bun.lock").is_file() or (directory / "bun.lockb").is_file():
        return "bun"
    return "npm"


def _safe_project_name(value: str) -> str:
    cleaned = "".join(character if character.isalnum() or character in " ._-" else "-" for character in value)
    return " ".join(cleaned.split()).strip(" .-")[:120] or "当前项目"


def _safe_package_directory_name(value: str) -> bool:
    return bool(value) and len(value) <= 120 and all(character.isalnum() or character in "._-" for character in value)


__all__ = ["AgentInitError", "AgentInitResult", "initialize_project_agents", "render_project_agents_template"]
