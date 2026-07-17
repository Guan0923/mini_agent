from pathlib import Path

from mini_agent.domain import AgentAction
from mini_agent.runtime import LegacyAgentRunner as AgentRunner
from mini_agent.tools import ToolRegistry
from mini_agent.tui.cli import TerminalApp


class FullAccessWritePlanner:
    name = "full-access-write"

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        if history[-1]["content"].startswith("[Tool result]"):
            return AgentAction(type="final_answer", answer="written")
        return AgentAction(
            type="tool_call",
            tool="run_command",
            arguments={"command": "python -c \"open('full-access.txt','w').write('ok')\""},
        )


class FullAccessPlanPlanner:
    name = "full-access-plan"

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        assert mode == "plan"
        return AgentAction(type="final_answer", answer="1. Inspect the project.\n2. Apply the approved change.")


def test_permission_command_full_access_auto_approves_confirmed_tools(tmp_path: Path, monkeypatch) -> None:
    app = TerminalApp(AgentRunner(FullAccessWritePlanner(), ToolRegistry(tmp_path)))
    monkeypatch.setattr("builtins.input", lambda _prompt: "2")

    assert app._handle("/permission") is True
    app.run_task("write a file")

    assert app._approval.permission_mode == "full_access"
    assert app.last_state is not None and app.last_state.status == "completed"
    assert (tmp_path / "full-access.txt").read_text(encoding="utf-8") == "ok"


def test_full_access_still_requires_explicit_plan_review(tmp_path: Path, monkeypatch, capsys) -> None:
    answers = iter(["2", "3"])
    app = TerminalApp(AgentRunner(FullAccessPlanPlanner(), ToolRegistry(tmp_path)))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert app._handle("/permission") is True
    assert app._handle("/plan") is True
    app.run_task("prepare a change")

    assert app.last_state is not None and app.last_state.status == "cancelled"
    assert "PLAN REVIEW" in capsys.readouterr().out


def test_legacy_slash_argument_is_a_task_and_help_lists_permission(tmp_path: Path, capsys, monkeypatch) -> None:
    app = TerminalApp(AgentRunner(FullAccessWritePlanner(), ToolRegistry(tmp_path)))
    tasks: list[str] = []
    monkeypatch.setattr(app, "run_task", tasks.append)

    assert app._handle("/permission/full access") is True
    assert app._approval.permission_mode == "approval_for_me"
    assert tasks == ["/permission/full access"]
    assert app._handle("/help") is True

    output = capsys.readouterr().out
    assert "/permission" in output
    assert "/clear <title>" in output
    assert "/new/<title>" not in output
