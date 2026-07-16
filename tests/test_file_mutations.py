from pathlib import Path

import pytest

from mini_agent.domain import AgentAction, StrategySelection
from mini_agent.planning import RuleBasedPlanner
from mini_agent.runtime import LegacyAgentRunner as AgentRunner
from mini_agent.runtime.contracts import InterruptDecision
from mini_agent.tools import ConfirmationRequired, ToolError, ToolRegistry


def test_file_mutation_tools_move_and_delete_workspace_entries(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)
    (tmp_path / "archive").mkdir()
    (tmp_path / "source.txt").write_text("source", encoding="utf-8")
    folder = tmp_path / "folder"
    (folder / "nested").mkdir(parents=True)
    (folder / "nested" / "note.txt").write_text("note", encoding="utf-8")
    (tmp_path / "empty").mkdir()
    tree = tmp_path / "tree"
    (tree / "nested").mkdir(parents=True)
    (tree / "nested" / "note.txt").write_text("note", encoding="utf-8")

    assert (
        tools.invoke("move_file", {"source": "source.txt", "destination": "archive/moved.txt"}, confirmed=True)
        == "Moved file source.txt to archive/moved.txt."
    )
    assert (tmp_path / "archive" / "moved.txt").read_text(encoding="utf-8") == "source"
    assert not (tmp_path / "source.txt").exists()

    assert (
        tools.invoke("move_folder", {"source": "folder", "destination": "archive/moved-folder"}, confirmed=True)
        == "Moved folder folder to archive/moved-folder."
    )
    assert (tmp_path / "archive" / "moved-folder" / "nested" / "note.txt").read_text(encoding="utf-8") == "note"

    assert (
        tools.invoke("delete_file", {"path": "archive/moved.txt"}, confirmed=True) == "Deleted file archive/moved.txt."
    )
    assert not (tmp_path / "archive" / "moved.txt").exists()
    assert tools.invoke("delete_folder", {"path": "empty"}, confirmed=True) == "Deleted empty folder empty."
    assert not (tmp_path / "empty").exists()

    with pytest.raises(ToolError, match="Directory is not empty"):
        tools.invoke("delete_folder", {"path": "tree"}, confirmed=True)
    assert tools.invoke("delete_folder", {"path": "tree", "recursive": True}, confirmed=True) == (
        "Deleted folder tree and its contents."
    )
    assert not tree.exists()


def test_file_mutation_tools_require_confirmation_and_are_not_retryable(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)
    (tmp_path / "file.txt").write_text("file", encoding="utf-8")
    (tmp_path / "folder").mkdir()
    (tmp_path / "move-file.txt").write_text("move", encoding="utf-8")
    (tmp_path / "move-folder").mkdir()
    (tmp_path / "target").mkdir()

    calls = [
        ("delete_file", {"path": "file.txt"}),
        ("delete_folder", {"path": "folder"}),
        ("move_file", {"source": "move-file.txt", "destination": "target/move-file.txt"}),
        ("move_folder", {"source": "move-folder", "destination": "target/move-folder"}),
    ]
    for name, arguments in calls:
        with pytest.raises(ConfirmationRequired):
            tools.invoke(name, arguments)
        assert tools.is_read_only(name) is False
        assert tools.is_retryable(name) is False

    assert tools.is_retryable("calculator") is True
    assert tools.is_retryable("write_file") is False
    assert tools.is_retryable("run_command") is False
    assert (tmp_path / "file.txt").exists()
    assert (tmp_path / "folder").exists()
    assert (tmp_path / "move-file.txt").exists()
    assert (tmp_path / "move-folder").exists()


def test_file_mutation_tools_reject_unsafe_paths_conflicts_and_invalid_types(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)
    (tmp_path / "file.txt").write_text("file", encoding="utf-8")
    (tmp_path / "existing.txt").write_text("existing", encoding="utf-8")
    (tmp_path / "folder" / "child").mkdir(parents=True)

    with pytest.raises(ToolError, match="Not a file"):
        tools.invoke("delete_file", {"path": "folder"}, confirmed=True)
    with pytest.raises(ToolError, match="Not a directory"):
        tools.invoke("delete_folder", {"path": "file.txt"}, confirmed=True)
    with pytest.raises(ToolError, match="workspace root"):
        tools.invoke("delete_folder", {"path": "."}, confirmed=True)
    with pytest.raises(ToolError, match="workspace root"):
        tools.invoke("move_folder", {"source": ".", "destination": "moved-workspace"}, confirmed=True)
    with pytest.raises(ToolError, match="not of type 'boolean'"):
        tools.invoke("delete_folder", {"path": "folder", "recursive": "true"}, confirmed=True)
    with pytest.raises(ToolError, match="Destination already exists"):
        tools.invoke("move_file", {"source": "file.txt", "destination": "existing.txt"}, confirmed=True)
    with pytest.raises(ToolError, match="Destination parent directory does not exist"):
        tools.invoke("move_file", {"source": "file.txt", "destination": "missing/file.txt"}, confirmed=True)
    with pytest.raises(ToolError, match="Cannot move a folder into itself"):
        tools.invoke("move_folder", {"source": "folder", "destination": "folder/child/moved"}, confirmed=True)
    with pytest.raises(ToolError, match="Path must stay inside the workspace"):
        tools.invoke("delete_file", {"path": "../outside.txt"}, confirmed=True)

    assert (tmp_path / "file.txt").read_text(encoding="utf-8") == "file"
    assert (tmp_path / "existing.txt").read_text(encoding="utf-8") == "existing"
    assert (tmp_path / "folder").exists()


class DeleteFolderPlanner:
    name = "delete-folder"

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        return AgentAction(type="tool_call", tool="delete_folder", arguments={"path": "tree", "recursive": True})

    def select_strategy(self, history: list[dict[str, str]], mode: str) -> StrategySelection:
        return StrategySelection("reactive", "The test calls one destructive tool.")


class MissingDeletePlanner:
    name = "missing-delete"

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        return AgentAction(type="tool_call", tool="delete_file", arguments={"path": "missing.txt"})

    def select_strategy(self, history: list[dict[str, str]], mode: str) -> StrategySelection:
        return StrategySelection("reactive", "The test checks retry policy.")


def test_cancelled_recursive_delete_preserves_tree_and_uses_one_approval(tmp_path: Path) -> None:
    (tmp_path / "tree" / "nested").mkdir(parents=True)
    (tmp_path / "tree" / "nested" / "note.txt").write_text("note", encoding="utf-8")
    requests = []

    state = AgentRunner(DeleteFolderPlanner(), ToolRegistry(tmp_path)).run(
        "delete tree",
        interrupt=lambda request: requests.append(request) or InterruptDecision("cancel"),
    )

    assert state.status == "cancelled"
    assert len(requests) == 1
    assert requests[0].data["arguments"] == {"path": "tree", "recursive": True}
    assert (tmp_path / "tree" / "nested" / "note.txt").exists()


def test_destructive_file_tools_do_not_retry_after_failure(tmp_path: Path) -> None:
    state = AgentRunner(MissingDeletePlanner(), ToolRegistry(tmp_path), max_retries=2).run(
        "delete missing file",
        interrupt=lambda _request: InterruptDecision("continue"),
    )

    assert state.status == "failed"
    assert "retry" not in [event.kind for event in state.events]


class RetryingToolExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def names(self) -> list[str]:
        return ["temporary"]

    def read_only_names(self) -> list[str]:
        return ["temporary"]

    def is_read_only(self, name: str) -> bool:
        return name == "temporary"

    def requires_confirmation(self, name: str) -> bool:
        return False

    def is_retryable(self, name: str) -> bool:
        return name == "temporary"

    def invoke(self, name: str, arguments: dict[str, object], confirmed: bool = False) -> str:
        assert name == "temporary"
        assert arguments == {}
        assert confirmed is True
        self.calls += 1
        if self.calls == 1:
            raise ToolError("temporary failure")
        return "recovered"


class RetryingPlanner:
    name = "retrying"

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        if history[-1]["content"].startswith("[Tool result]"):
            return AgentAction(type="final_answer", answer="recovered")
        return AgentAction(type="tool_call", tool="temporary", arguments={})

    def select_strategy(self, history: list[dict[str, str]], mode: str) -> StrategySelection:
        return StrategySelection("reactive", "The test checks that retryable tools still retry.")


def test_retryable_tools_keep_the_existing_retry_behavior() -> None:
    tools = RetryingToolExecutor()
    state = AgentRunner(RetryingPlanner(), tools, max_retries=1).run(
        "retry once",
        interrupt=lambda _request: InterruptDecision("continue"),
    )

    assert state.status == "completed"
    assert tools.calls == 2
    assert "retry" in [event.kind for event in state.events]


@pytest.mark.parametrize(
    ("task", "tool", "arguments"),
    [
        ("delete file note.txt", "delete_file", {"path": "note.txt"}),
        ("delete folder cache", "delete_folder", {"path": "cache", "recursive": False}),
        ("delete folder recursive build", "delete_folder", {"path": "build", "recursive": True}),
        ("删除文件 note.txt", "delete_file", {"path": "note.txt"}),
        ("递归删除目录 build", "delete_folder", {"path": "build", "recursive": True}),
        ("move file a.txt to archive/a.txt", "move_file", {"source": "a.txt", "destination": "archive/a.txt"}),
        ("移动文件夹 old 到 archive/old", "move_folder", {"source": "old", "destination": "archive/old"}),
    ],
)
def test_rule_planner_parses_file_mutation_commands(task: str, tool: str, arguments: dict[str, object]) -> None:
    planner = RuleBasedPlanner()
    runtime = AgentRunner(planner, ToolRegistry()).new_runtime(task=task)
    response = planner.decide(runtime)

    assert len(response.tool_messages) == 1
    assert response.tool_messages[0].name == tool
    assert response.tool_messages[0].arguments == arguments
