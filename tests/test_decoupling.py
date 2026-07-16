from pathlib import Path

from mini_agent.domain import AgentAction, StrategySelection
from mini_agent.planning import PlannerCapabilities
from mini_agent.runtime import AgentRunner, ConversationService, SQLiteSessionStore
from mini_agent.tools import Tool, ToolRegistry


def test_tool_registry_accepts_constructor_injected_tools() -> None:
    registry = ToolRegistry([Tool("echo", "Returns its value.", lambda value: value)])

    assert registry.names() == ["echo"]
    assert registry.invoke("echo", {"value": "hello"}) == "hello"


class FeedbackOnlyPlanner:
    name = "feedback-only"

    def create_plan(self, history, mode, on_reasoning=None):
        raise AssertionError("Capability discovery must not invoke create_plan.")

    def replan(self, history, plan, reason, on_reasoning=None):
        raise AssertionError("Capability discovery must not invoke replan.")


def test_planner_capabilities_keep_plan_feedback_separate_from_dynamic_replanning() -> None:
    capabilities = PlannerCapabilities.from_planner(FeedbackOnlyPlanner())

    assert capabilities.plan_creator is not None
    assert capabilities.plan_replanner is not None
    assert capabilities.dynamic_replanner is None


class RememberingPlanner:
    name = "remembering"

    def __init__(self) -> None:
        self.histories: list[list[dict[str, str]]] = []

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        self.histories.append(list(history))
        return AgentAction(type="final_answer", answer=f"remembered {history[-1]['content']}")

    def select_strategy(self, history: list[dict[str, str]], mode: str) -> StrategySelection:
        return StrategySelection("reactive", "The answer does not need a tool.")


class PrefixTask:
    def expand(self, task: str) -> str:
        return f"prepared: {task}"


def test_conversation_service_owns_preprocessing_session_lifecycle_and_history(tmp_path: Path) -> None:
    planner = RememberingPlanner()
    service = ConversationService(
        AgentRunner(planner, ToolRegistry(tmp_path)),
        SQLiteSessionStore(tmp_path / ".mini_agent" / "checkpoints.db"),
        PrefixTask(),
    )

    first = service.run_task("first", mode="agent")
    assert first.status == "completed"
    assert service.active_session is not None
    session_id = service.active_session.session_id

    second = service.run_task("second", mode="agent")

    assert second.status == "completed"
    assert planner.histories[-1][-3:] == [
        {"role": "user", "content": "prepared: first"},
        {"role": "assistant", "content": "remembered prepared: first"},
        {"role": "user", "content": "prepared: second"},
    ]
    assert service.history()[-2:] == [
        {"role": "user", "content": "prepared: second"},
        {"role": "assistant", "content": "remembered prepared: second"},
    ]
    assert service.current_summary() is not None
    assert service.current_summary().session_id == session_id
