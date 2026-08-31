from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from time import sleep

import pytest

from backend.runtime.subagents import LockedToolExecutor, WorkspaceWriteLock
from backend.tools import ToolError, ToolRegistry, build_tool_registry, delegation_tools


class _IdleTools:
    def names(self) -> list[str]:
        return []

    def read_only_names(self) -> list[str]:
        return []

    def specs(self) -> list[object]:
        return []

    def read_only_specs(self) -> list[object]:
        return []

    def is_read_only(self, _name: str) -> bool:
        return False

    def requires_confirmation(self, _name: str) -> bool:
        return False

    def is_workspace_confined(self, _name: str) -> bool:
        return False

    def is_retryable(self, _name: str) -> bool:
        return False

    def validate_arguments(self, _name: str, _arguments: dict[str, object]) -> None:
        return None

    def invoke(self, _name: str, _arguments: dict[str, object], confirmed: bool = False) -> str:
        return "ok"


def test_persistent_subagent_tool_contract_exposes_only_the_read_query_in_plan_mode() -> None:
    tools = delegation_tools()
    assert [tool.name for tool in tools] == [
        "delegate_tasks",
        "send_agent_message",
        "set_thread_node_status",
        "get_thread_node",
        "pause_current_turn",
    ]
    assert [tool.name for tool in tools if tool.read_only] == ["get_thread_node"]
    delegate = tools[0].spec.parameters
    assert set(delegate["properties"]) == {
        "source_thread_id",
        "subagent_path",
        "subagent_task",
        "context_transfer_strategy",
    }
    assert delegate["properties"]["context_transfer_strategy"]["enum"] == [
        "share",
        "compaction_share",
        "independent",
    ]
    assert delegate["required"] == ["subagent_path", "subagent_task", "context_transfer_strategy"]
    send = tools[1].spec.parameters
    assert set(send["properties"]) == {
        "source_thread_id",
        "target_thread_path",
        "subagent_task",
        "references",
        "need_reply",
    }
    assert send["required"] == ["target_thread_path", "subagent_task"]
    assert send["properties"]["references"]["items"]["required"] == ["path"]
    status = tools[2].spec.parameters
    assert set(status["properties"]) == {"source_thread_id", "target_thread_path", "thread_status"}
    assert status["required"] == ["target_thread_path", "thread_status"]
    assert status["properties"]["thread_status"]["enum"] == ["running", "paused", "success"]
    query = tools[3].spec.parameters
    assert set(query["properties"]) == {"source_thread_id", "target_thread_path"}
    assert query["required"] == []
    pause = tools[4].spec.parameters
    assert pause == {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    registry = ToolRegistry(tools)
    registry.validate_arguments(
        "send_agent_message",
        {"target_thread_path": "/root/target", "subagent_task": "follow up"},
    )
    with pytest.raises(ToolError, match="non-empty"):
        registry.validate_arguments(
            "send_agent_message",
            {"source_thread_id": "", "target_thread_path": "/root/target", "subagent_task": "follow up"},
        )


def test_locked_executor_serializes_same_path_writes(tmp_path: Path) -> None:
    active = 0
    maximum = 0
    guard = Lock()

    class Tools(_IdleTools):
        def invoke(self, _name: str, _arguments: dict[str, object], confirmed: bool = False) -> str:
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            sleep(0.02)
            with guard:
                active -= 1
            return "written"

    executor = LockedToolExecutor(Tools(), WorkspaceWriteLock(), tmp_path)
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [
            workers.submit(executor.invoke, "write_file", {"path": str(tmp_path / "same.txt")}, True) for _ in range(2)
        ]
        assert [future.result() for future in futures] == ["written", "written"]
    assert maximum == 1


def test_locked_executor_allows_writes_with_a_shared_missing_parent(tmp_path: Path) -> None:
    executor = LockedToolExecutor(build_tool_registry(tmp_path), WorkspaceWriteLock(), tmp_path)

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [
            workers.submit(
                executor.invoke,
                "write_file",
                {"path": str(tmp_path / "shared" / f"{name}.txt"), "content": name},
                True,
            )
            for name in ("one", "two")
        ]
        assert all("Created " in future.result() for future in futures)

    assert (tmp_path / "shared" / "one.txt").read_text(encoding="utf-8") == "one"
    assert (tmp_path / "shared" / "two.txt").read_text(encoding="utf-8") == "two"


def test_explicit_create_directory_excludes_other_workspace_writes(tmp_path: Path) -> None:
    active = 0
    maximum = 0
    guard = Lock()

    class Tools(_IdleTools):
        def invoke(self, _name: str, _arguments: dict[str, object], confirmed: bool = False) -> str:
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            sleep(0.02)
            with guard:
                active -= 1
            return "done"

    executor = LockedToolExecutor(Tools(), WorkspaceWriteLock(), tmp_path)
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [
            workers.submit(executor.invoke, "create_directory", {"path": str(tmp_path / "shared")}, True),
            workers.submit(executor.invoke, "write_file", {"path": str(tmp_path / "other.txt")}, True),
        ]
        assert [future.result() for future in futures] == ["done", "done"]
    assert maximum == 1


def test_locked_executor_rejects_missing_workspace_path() -> None:
    executor = LockedToolExecutor(_IdleTools(), WorkspaceWriteLock())
    try:
        executor.invoke("write_file", {}, True)
    except ToolError as exc:
        assert "requires a path" in str(exc)
    else:
        raise AssertionError("missing path was accepted")
