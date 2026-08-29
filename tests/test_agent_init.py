from __future__ import annotations

from pathlib import Path

import pytest

from backend.runtime.agent_init import AgentInitError, initialize_project_agents, render_project_agents_template


def test_initialize_project_agents_creates_detected_starter_without_overwrite(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    frontend = project / "frontend"
    tests = project / "tests"
    frontend.mkdir(parents=True)
    tests.mkdir()
    (project / "uv.lock").write_text("", encoding="utf-8")
    (project / "pyproject.toml").write_text("[tool.ruff]\n[tool.pytest.ini_options]\n", encoding="utf-8")
    (frontend / "package-lock.json").write_text("{}", encoding="utf-8")
    (frontend / "package.json").write_text(
        '{"scripts":{"typecheck":"tsc --noEmit","test":"vitest run","build":"vite build"}}',
        encoding="utf-8",
    )

    result = initialize_project_agents(project)

    target = project / "AGENTS.md"
    assert result.path == "AGENTS.md"
    assert target.read_text(encoding="utf-8") == result.content
    assert result.byte_count == len(result.content.encode("utf-8"))
    assert "Mini-Agent 只发现项目根目录" in result.content
    assert "uv run python -m ruff check ." in result.content
    assert "uv run python -m pytest -q" in result.content
    assert "cd frontend; npm run typecheck" in result.content
    assert "cd frontend; npm run test" in result.content
    assert "cd frontend; npm run build" in result.content

    original = target.read_bytes()
    with pytest.raises(AgentInitError, match="已存在 AGENTS.md"):
        initialize_project_agents(project)
    assert target.read_bytes() == original


def test_render_project_agents_template_falls_back_for_unknown_project(tmp_path: Path) -> None:
    project = tmp_path / "unknown"
    project.mkdir()

    content = render_project_agents_template(project)

    assert content.startswith("# unknown 仓库协作规范")
    assert "未识别到标准构建清单" in content
    assert "请补充项目的格式检查、测试和构建命令" in content
    assert str(tmp_path) not in content


def test_initialize_project_agents_treats_any_existing_target_as_conflict(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    (project / "AGENTS.md").mkdir()

    with pytest.raises(AgentInitError, match="已存在 AGENTS.md"):
        initialize_project_agents(project)


def test_initialize_project_agents_rejects_missing_project_root(tmp_path: Path) -> None:
    with pytest.raises(AgentInitError, match="项目根目录不可访问"):
        initialize_project_agents(tmp_path / "missing")


def test_initialize_project_agents_rejects_symlink_root(tmp_path: Path) -> None:
    project = tmp_path / "project"
    link = tmp_path / "project-link"
    project.mkdir()
    try:
        link.symlink_to(project, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks is unavailable on this platform.")

    with pytest.raises(AgentInitError, match="不能是符号链接"):
        initialize_project_agents(link)
