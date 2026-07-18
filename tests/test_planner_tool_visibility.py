from pathlib import Path

from mini_agent.domain import AssistantMessage
from mini_agent.planning import LLMPlanner
from mini_agent.runtime import AgentRunner, PreparedResponse
from mini_agent.tools import ToolRegistry


class RecordingClient:
    def __init__(self) -> None:
        self.requests = []

    def run(self, runtime):
        self.requests.append(runtime.exchange)
        return PreparedResponse(AssistantMessage(content="Inspected the workspace."))


def test_plan_mode_exposes_specialized_read_tools_but_not_command_or_mutations(tmp_path: Path) -> None:
    registry = ToolRegistry(tmp_path)
    client = RecordingClient()
    planner = LLMPlanner(client, registry.specs(), registry.read_only_specs())
    runtime = AgentRunner(planner, registry).new_runtime(task="Inspect the project", mode="plan")

    planner.decide(runtime)

    assert [tool.name for tool in runtime.exchange.allowed_tools] == [
        "read_file",
        "glob",
        "grep",
        "web_search",
        "web_fetch",
        "request_user_input",
        "request_plan_review",
    ]
    system_prompt = runtime.exchange.messages[0].content or ""
    assert "Use read_file" in system_prompt
    assert "only use run_command" not in system_prompt


def test_agent_mode_exposes_complete_tool_catalog(tmp_path: Path) -> None:
    registry = ToolRegistry(tmp_path)
    client = RecordingClient()
    planner = LLMPlanner(client, registry.specs(), registry.read_only_specs())
    runtime = AgentRunner(planner, registry).new_runtime(task="Implement the change")

    planner.decide(runtime)

    assert [tool.name for tool in runtime.exchange.allowed_tools] == registry.names()
    system_prompt = runtime.exchange.messages[0].content or ""
    assert "Need file contents" in system_prompt
    assert "another general operation? → run_command" in system_prompt
