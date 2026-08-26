from __future__ import annotations

import threading
from pathlib import Path

import pytest

from backend.domain import AssistantMessage, ToolMessage
from backend.planning.llm import LLMPlanner
from backend.planning.prompts import compose_system_prompt
from backend.runtime import AgentRunner
from backend.runtime.core.context import AgentRuntime, PreparedResponse
from backend.tools import ToolRegistry


class BlockingDecisionClient:
    context_size = 128_000

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.system_prompts: list[str] = []
        self.allowed_tools: list[list[str]] = []

    def estimate_tokens(self, _messages, _tools, _request_parameters) -> int:
        return 100

    def run(self, runtime: AgentRuntime) -> PreparedResponse:
        self.system_prompts.append(str(runtime.exchange.messages[0].content))
        names = [tool.name for tool in runtime.exchange.operation_tools]
        self.allowed_tools.append(names)
        if len(self.system_prompts) == 1:
            self.started.set()
            assert self.release.wait(5)
            ordinary = next(name for name in names if not name.startswith("request_"))
            return PreparedResponse(
                AssistantMessage(tool_messages=[ToolMessage(name=ordinary, call_id="stale_call", arguments={})]),
                {"total_tokens": 1},
            )
        return PreparedResponse(AssistantMessage(content="completed after mode switch"), {"total_tokens": 1})


@pytest.mark.parametrize(("initial", "target"), [("agent", "plan"), ("plan", "agent")])
def test_running_turn_redispatches_both_mode_directions_at_the_next_boundary(
    tmp_path: Path,
    initial: str,
    target: str,
) -> None:
    tools = ToolRegistry(tmp_path)
    client = BlockingDecisionClient()
    runner = AgentRunner(LLMPlanner(client, tools.specs(), tools.read_only_specs()), tools)
    runtime = runner.new_runtime(task="switch modes", mode=initial)  # type: ignore[arg-type]
    result: list[object] = []
    worker = threading.Thread(target=lambda: result.append(runner.run(runtime)), daemon=True)
    worker.start()
    assert client.started.wait(5)

    runtime.services.pending_runtime_config = {"running_mode": target}
    client.release.set()
    worker.join(10)

    assert not worker.is_alive()
    assert result and runtime.run.status == "completed"
    assert runtime.run.mode == target
    assert client.system_prompts == [compose_system_prompt(initial), compose_system_prompt(target)]
    assert ("request_plan_review" in client.allowed_tools[1]) is (target == "plan")
    stale = next(
        message for message in runtime.state.messages if isinstance(message, AssistantMessage) and message.tool_messages
    )
    assert stale.tool_messages[0].status == "failed"
    assert "workflow mode changed" in (stale.tool_messages[0].content or "")
