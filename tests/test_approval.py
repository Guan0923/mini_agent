from mini_agent.runtime.contracts import InterruptRequest
from mini_agent.tui.approval import TerminalApproval


def test_terminal_approval_collects_english_supplement(monkeypatch, capsys) -> None:
    answers = iter(["3", "Use a smaller change."])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    decision = TerminalApproval()(
        InterruptRequest("plan", "Execute this plan in Agent mode?", {"goal": "Update docs", "steps": ["Edit README"]})
    )

    assert decision.choice == "supplement"
    assert decision.supplement == "Use a smaller change."
    assert "PLAN REVIEW\nGoal: Update docs\n1. Edit README\n" in capsys.readouterr().out
