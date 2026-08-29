from __future__ import annotations

from pathlib import Path

import pytest

from backend.runtime.instructions import AgentInstructionsError, discover_agent_instructions


def test_project_agents_replaces_global_agents(tmp_path: Path) -> None:
    global_root = tmp_path / "mini-home"
    project_root = tmp_path / "project"
    global_root.mkdir()
    project_root.mkdir()
    (global_root / "AGENTS.md").write_text("global guidance", encoding="utf-8")
    (project_root / "AGENTS.md").write_text("project guidance", encoding="utf-8")

    result = discover_agent_instructions(global_root=global_root, project_root=project_root)

    assert result.source is not None
    assert result.source.scope == "project"
    assert result.source.display_path == "AGENTS.md"
    assert result.source.content == "project guidance"
    assert "global guidance" not in result.render()
    assert str(tmp_path) not in result.render()


def test_missing_project_agents_falls_back_to_global_agents(tmp_path: Path) -> None:
    global_root = tmp_path / "mini-home"
    project_root = tmp_path / "project"
    global_root.mkdir()
    project_root.mkdir()
    (global_root / "AGENTS.md").write_text("global guidance", encoding="utf-8")

    result = discover_agent_instructions(global_root=global_root, project_root=project_root)

    assert result.source is not None
    assert result.source.scope == "global"
    assert result.source.display_path == "~/.mini_agent/AGENTS.md"
    assert result.source.content == "global guidance"


def test_empty_project_agents_falls_back_to_global_agents(tmp_path: Path) -> None:
    global_root = tmp_path / "mini-home"
    project_root = tmp_path / "project"
    global_root.mkdir()
    project_root.mkdir()
    (global_root / "AGENTS.md").write_text("global guidance", encoding="utf-8")
    (project_root / "AGENTS.md").write_text("  \n", encoding="utf-8")

    result = discover_agent_instructions(global_root=global_root, project_root=project_root)

    assert result.source is not None
    assert result.source.scope == "global"


def test_ignores_override_and_nested_agents_files(tmp_path: Path) -> None:
    global_root = tmp_path / "mini-home"
    project_root = tmp_path / "project"
    nested = project_root / "services" / "api"
    global_root.mkdir()
    nested.mkdir(parents=True)
    (global_root / "AGENTS.md").write_text("global guidance", encoding="utf-8")
    (project_root / "AGENTS.override.md").write_text("override guidance", encoding="utf-8")
    (nested / "AGENTS.md").write_text("nested guidance", encoding="utf-8")

    result = discover_agent_instructions(global_root=global_root, project_root=project_root)

    assert result.source is not None
    assert result.source.scope == "global"
    rendered = result.render()
    assert "override guidance" not in rendered
    assert "nested guidance" not in rendered


def test_selected_file_byte_limit_truncates_on_a_utf8_boundary(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "AGENTS.md").write_text("界界", encoding="utf-8")

    result = discover_agent_instructions(global_root=None, project_root=project_root, max_bytes=4)

    assert result.source is not None
    assert result.source.content == "界"
    assert result.total_bytes == 3
    assert result.total_bytes <= result.max_bytes
    assert result.truncated is True


def test_rejects_non_utf8_project_agents_instead_of_falling_back(tmp_path: Path) -> None:
    global_root = tmp_path / "mini-home"
    project_root = tmp_path / "project"
    global_root.mkdir()
    project_root.mkdir()
    (global_root / "AGENTS.md").write_text("global guidance", encoding="utf-8")
    (project_root / "AGENTS.md").write_bytes(b"valid\xffinvalid")

    with pytest.raises(AgentInstructionsError, match="must be UTF-8"):
        discover_agent_instructions(global_root=global_root, project_root=project_root)


def test_project_symlink_falls_back_to_global_agents(tmp_path: Path) -> None:
    global_root = tmp_path / "mini-home"
    project_root = tmp_path / "project"
    global_root.mkdir()
    project_root.mkdir()
    (global_root / "AGENTS.md").write_text("global guidance", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("outside guidance", encoding="utf-8")
    try:
        (project_root / "AGENTS.md").symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks is unavailable on this platform.")

    result = discover_agent_instructions(global_root=global_root, project_root=project_root)

    assert result.source is not None
    assert result.source.scope == "global"
    assert "outside guidance" not in result.render()


def test_missing_global_and_project_agents_returns_empty_result(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()

    result = discover_agent_instructions(global_root=None, project_root=project_root)

    assert result.source is None
    assert result.total_bytes == 0
    assert result.truncated is False
    assert result.render() == ""
