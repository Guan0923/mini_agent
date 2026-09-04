from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.runtime.conversation.user_input import REQUEST_USER_INPUT_SPEC
from backend.runtime.planning.review import REQUEST_PLAN_REVIEW_SPEC
from backend.tools import WorkspaceFiles, build_tool_registry, delegation_tools

EXPECTED_LOCAL_TOOLS = [
    "get_current_time",
    "update_todo_list",
    "read_file",
    "glob",
    "grep",
    "web_search",
    "web_fetch",
    "create_directory",
    "write_file",
    "edit_file",
    "run_command",
    "delegate_tasks",
    "send_agent_message",
    "set_thread_node_status",
    "get_thread_node",
    "pause_current_turn",
    "request_user_input",
    "request_plan_review",
]


def _missing_property_descriptions(schema: dict[str, Any], prefix: str = "") -> list[str]:
    missing: list[str] = []
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return missing
    for name, raw_property in properties.items():
        path = f"{prefix}.{name}" if prefix else name
        if not isinstance(raw_property, dict):
            missing.append(path)
            continue
        description = raw_property.get("description")
        if not isinstance(description, str) or not description.strip():
            missing.append(path)
        missing.extend(_missing_property_descriptions(raw_property, path))
        items = raw_property.get("items")
        if isinstance(items, dict):
            missing.extend(_missing_property_descriptions(items, f"{path}[]"))
    return missing


def test_all_local_tools_and_named_parameters_have_descriptions(tmp_path: Path) -> None:
    registry = build_tool_registry(
        tmp_path,
        workspace_files=WorkspaceFiles(tmp_path),
        extra_tools=delegation_tools(),
    )
    specs = [*registry.specs(), REQUEST_USER_INPUT_SPEC, REQUEST_PLAN_REVIEW_SPEC]

    assert [spec.name for spec in specs] == EXPECTED_LOCAL_TOOLS
    assert all(isinstance(spec.description, str) and spec.description.strip() for spec in specs)
    assert {spec.name: missing for spec in specs if (missing := _missing_property_descriptions(spec.parameters))} == {}
