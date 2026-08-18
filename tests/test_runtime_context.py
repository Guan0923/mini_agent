from pathlib import Path

from backend.domain import AssistantMessage, RunState, ToolMessage, ToolSpec, UserMessage
from backend.planning import RuleBasedPlanner
from backend.runtime import AgentRunner, ConversationService, RuntimeState
from backend.tools import Tool, ToolRegistry
from tests.local_store import session_store


def test_messages_expose_required_fields_and_nest_tools() -> None:
    tool = ToolMessage(
        name="run_command",
        call_id="call_1",
        arguments={"expression": "2 + 2"},
        content="4",
        status="succeeded",
    )
    assistant = AssistantMessage(
        name="mini-agent",
        content=None,
        reasoning="Use arithmetic.",
        logprobs={"content": []},
        tool_messages=[tool],
    )

    assert (assistant.name, assistant.role, assistant.content) == ("mini-agent", "assistant", None)
    assert (tool.name, tool.role, tool.content) == ("run_command", "tool", "4")
    assert assistant.tool_messages == [tool]


def test_runtime_state_round_trips_complete_session_state() -> None:
    tool = ToolMessage(
        name="run_command",
        call_id="call_1",
        arguments={"expression": "2 + 2"},
        content="4",
        status="succeeded",
        retryable=True,
    )
    state = RuntimeState(
        session_id="session_1",
        messages=[
            UserMessage(name="alice", content="Calculate.", provider_options={"chat_completions": {"extra_body": {}}}),
            AssistantMessage(
                logprobs={"content": []},
                tool_messages=[tool],
                provider_options={"chat_completions": {"response": {"created": 1}}},
            ),
        ],
        provider="chat_completions",
        model="chat_completions-test",
        request_parameters={"max_tokens": 512},
        tool_specs=[
            ToolSpec(
                "run_command",
                "Calculate",
                {"type": "object"},
                provider_options={"chat_completions": {"strict": True}},
            )
        ],
        current_run=RunState(task="Calculate.", mode="agent"),
        usage={"total_tokens": 12},
        status="running",
    )

    restored = RuntimeState.from_dict(state.to_dict())

    assert restored.session_id == state.session_id
    assert restored.provider == "chat_completions"
    assert restored.request_parameters == {"max_tokens": 512}
    assert restored.usage == {"total_tokens": 12}
    assert restored.current_run is not None and restored.current_run.task == "Calculate."
    restored_assistant = restored.messages[1]
    assert isinstance(restored_assistant, AssistantMessage)
    assert restored_assistant.logprobs == {"content": []}
    assert restored_assistant.provider_options == {"chat_completions": {"response": {"created": 1}}}
    assert restored_assistant.tool_messages[0] == tool
    assert restored.tool_specs[0].provider_options == {"chat_completions": {"strict": True}}


def test_postgres_persists_and_reloads_runtime_snapshot(tmp_path: Path) -> None:
    store = session_store(tmp_path / "store")
    session = store.create_session("Runtime")
    tools = ToolRegistry(
        [
            Tool(
                "run_command",
                "Calculate",
                lambda expression: expression,
                parameters={"type": "object"},
            )
        ]
    )
    runtime = AgentRunner(RuleBasedPlanner(), tools).empty_runtime(
        session_id=session.session_id,
        runtime_store=store,
    )
    runtime.state.messages.append(AssistantMessage(content="done", logprobs={"content": []}))
    runtime.state.usage = {"prompt_tokens": 5, "total_tokens": 8}

    runtime.save()
    restored = store.load_runtime(session.session_id)

    assert restored is not None
    assert restored.usage == {"prompt_tokens": 5, "total_tokens": 8}
    assert restored.tool_specs[0].name == "run_command"
    assert isinstance(restored.messages[0], AssistantMessage)
    assert restored.messages[0].logprobs == {"content": []}


class UsagePlanner:
    name = "usage"

    def __init__(self) -> None:
        self.turn = 0

    def decide(self, runtime):
        self.turn += 1
        runtime.state.turn_usage = {"total_tokens": self.turn}
        return AssistantMessage(content=f"turn {self.turn}")


def test_completed_turn_overwrites_session_usage(tmp_path: Path) -> None:
    store = session_store(tmp_path / "store")
    service = ConversationService(AgentRunner(UsagePlanner(), ToolRegistry()), store)

    service.run_task("first", mode="agent")
    service.run_task("second", mode="agent")

    assert service.runtime is not None
    assert service.runtime.state.usage == {"total_tokens": 2}
    restored = store.load_runtime(service.runtime.state.session_id)
    assert restored is not None and restored.usage == {"total_tokens": 2}
