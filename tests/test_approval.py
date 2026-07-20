from mini_agent.runtime.core.contracts import InterruptRequest, QuestionOption, UserQuestion
from mini_agent.tui.approval import TerminalApproval


def test_tool_review_collects_english_supplement(monkeypatch, capsys) -> None:
    answers = iter(["3", "Use a smaller change."])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    decision = TerminalApproval()(
        InterruptRequest("tool", "Call tool run_command?", {"tool": "run_command", "arguments": {}})
    )

    assert decision.choice == "supplement"
    assert decision.supplement == "Use a smaller change."
    assert "TOOL REVIEW\nTool: run_command\nAction: Run command\nCommand: <missing>" in capsys.readouterr().out


def test_plan_review_implements_plan(monkeypatch, capsys) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "implement")

    decision = TerminalApproval()(InterruptRequest("plan", "Implement this plan?", {"plan": "1. Edit README."}))

    assert decision.choice == "implement"
    assert "PLAN REVIEW\n1. Edit README." in capsys.readouterr().out


def test_plan_review_implements_and_clears_session(monkeypatch, capsys) -> None:
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "2")

    decision = TerminalApproval()(InterruptRequest("plan", "Implement this plan?", {"plan": "1. Edit README."}))

    assert decision.choice == "implement_clear_session"
    assert prompts == ["[1] Implement  [2] Implement and Clear Session  [3] Cancel and Stay in plan mode: "]
    assert "PLAN REVIEW\n1. Edit README." in capsys.readouterr().out


def test_plan_review_rejects_tool_continue_choice(monkeypatch, capsys) -> None:
    answers = iter(["continue", "1"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    decision = TerminalApproval()(InterruptRequest("plan", "Implement this plan?", {"plan": "1. Edit README."}))

    assert decision.choice == "implement"
    assert "Choose 1, 2, or 3." in capsys.readouterr().out


def test_plan_review_rejects_tool_supplement_choice(monkeypatch, capsys) -> None:
    answers = iter(["supplement", "3"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    decision = TerminalApproval()(InterruptRequest("plan", "Implement this plan?", {"plan": "1. Edit README."}))

    assert decision.choice == "cancel"
    assert decision.supplement is None
    assert "Choose 1, 2, or 3." in capsys.readouterr().out


def test_tool_review_continues_tool(monkeypatch, capsys) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "continue")

    decision = TerminalApproval()(
        InterruptRequest("tool", "Call tool run_command?", {"tool": "run_command", "arguments": {}})
    )

    assert decision.choice == "continue"
    assert "TOOL REVIEW\nTool: run_command\nAction: Run command\nCommand: <missing>" in capsys.readouterr().out


def test_tool_review_rejects_plan_implement_choice(monkeypatch, capsys) -> None:
    answers = iter(["implement", "1"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    decision = TerminalApproval()(
        InterruptRequest("tool", "Call tool run_command?", {"tool": "run_command", "arguments": {}})
    )

    assert decision.choice == "continue"
    assert "Choose 1, 2, or 3." in capsys.readouterr().out


def test_terminal_approval_switches_permission_modes(monkeypatch, capsys) -> None:
    answers = iter(["not a mode", "full access"])
    approval = TerminalApproval()
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    approval.configure_permission()

    assert approval.permission_mode == "full_access"
    output = capsys.readouterr().out
    assert "Current: Approval for me" in output
    assert "Choose 1 or 2." in output
    assert "PERMISSION SET — Full access" in output


def test_full_access_auto_approves_tools_but_not_plan_review(monkeypatch, capsys) -> None:
    approval = TerminalApproval("full_access")
    monkeypatch.setattr("builtins.input", lambda _prompt: "3")

    tool_decision = approval(InterruptRequest("tool", "Call tool run_command?", {"tool": "run_command", "arguments": {}}))
    plan_decision = approval(InterruptRequest("plan", "Implement this plan?", {"plan": "1. Write the file."}))

    assert tool_decision.choice == "continue"
    assert plan_decision.choice == "cancel"
    output = capsys.readouterr().out
    assert "TOOL REVIEW" not in output
    assert "PLAN REVIEW\n1. Write the file." in output


def test_terminal_questionnaire_uses_numeric_choices_and_custom_input(monkeypatch, capsys) -> None:
    questions = (
        UserQuestion(
            "storage",
            "Storage",
            "Where should the result be stored?",
            (
                QuestionOption("SQLite", "Use the existing database."),
                QuestionOption("JSONL", "Use the audit stream."),
            ),
        ),
        UserQuestion(
            "scope",
            "Scope",
            "How broad should the change be?",
            (
                QuestionOption("Focused", "Change one workflow."),
                QuestionOption("Shared", "Change shared behavior."),
            ),
        ),
    )
    answers = iter(["2", "3", "Only storage code"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    decision = TerminalApproval()(
        InterruptRequest("question", "Answer questions.", {"questions": []}, questions=questions)
    )

    assert decision.choice == "answer"
    assert decision.answers == {"storage": ["JSONL"], "scope": ["Only storage code"]}
    output = capsys.readouterr().out
    assert "PLAN QUESTIONS" in output
    assert "[3] 其他" in output


def test_terminal_questionnaire_with_no_model_options_uses_custom_input(monkeypatch, capsys) -> None:
    question = UserQuestion("details", "Details", "What should be used?", ())
    answers = iter(["1", "Custom value"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    decision = TerminalApproval()(
        InterruptRequest("question", "Answer questions.", {"questions": []}, questions=(question,))
    )

    assert decision.answers == {"details": ["Custom value"]}
    assert "[1] 其他" in capsys.readouterr().out
