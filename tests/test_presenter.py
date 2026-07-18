from mini_agent.runtime import RuntimeEvent
from mini_agent.tui.presenter import TerminalPresenter


def test_presenter_renders_stream_and_response(capsys) -> None:
    presenter = TerminalPresenter()

    presenter.on_event(RuntimeEvent("strategy", "reactive", {"reason": "Simple task."}))
    presenter.on_event(RuntimeEvent("thinking_start"))
    presenter.on_event(RuntimeEvent("thinking_delta", "Thinking."))
    presenter.on_event(RuntimeEvent("thinking_end"))
    presenter.on_event(RuntimeEvent("model_repair", "Malformed model action was repaired automatically."))
    presenter.on_event(RuntimeEvent("tool_recovery", "missing.txt", {"tool": "run_command", "attempt": 1}))
    presenter.on_event(RuntimeEvent("model_retry", "HTTP 503", {"attempt": 1}))
    presenter.on_event(RuntimeEvent("response", "Hello."))

    assert capsys.readouterr().out == (
        "STRATEGY reactive — Simple task.\n"
        "THINKING\n"
        "Thinking.\n"
        "MODEL FORMAT RETRY — Malformed model action was repaired automatically.\n"
        "TOOL RECOVERY 1 — run_command: missing.txt\n"
        "MODEL RETRY 1 — HTTP 503\n"
        "RESPONSE\n"
        "Hello.\n"
    )


def test_presenter_renders_model_diagnostics_for_errors(capsys) -> None:
    presenter = TerminalPresenter()

    presenter.on_event(
        RuntimeEvent(
            "error",
            "Planning failed: Model response did not contain JSON content.",
            {
                "provider_diagnostics": {
                    "finish_reason": "stop",
                    "content_chars": 217,
                    "reasoning_chars": 241,
                }
            },
        )
    )

    assert capsys.readouterr().out == (
        "ERROR Planning failed: Model response did not contain JSON content.\n"
        "MODEL DIAGNOSTICS finish_reason=stop content_chars=217 reasoning_chars=241\n"
    )


def test_presenter_streams_response_once_and_suppresses_final_duplicate(capsys) -> None:
    presenter = TerminalPresenter()

    presenter.on_event(RuntimeEvent("thinking_start"))
    presenter.on_event(RuntimeEvent("thinking_delta", "Reasoning"))
    presenter.on_event(RuntimeEvent("thinking_end"))
    presenter.on_event(RuntimeEvent("response_start"))
    presenter.on_event(RuntimeEvent("response_delta", "Hel"))
    presenter.on_event(RuntimeEvent("response_delta", "lo"))
    presenter.on_event(RuntimeEvent("response_end"))
    presenter.on_event(RuntimeEvent("response", "Hello", {"streamed": True}))

    presenter.on_event(RuntimeEvent("plan", "Hello", {"streamed": True}))
    assert capsys.readouterr().out == "THINKING\nReasoning\nRESPONSE\nHello\n"


def test_presenter_renders_plan_progress_statuses_and_truncated_results(capsys) -> None:
    presenter = TerminalPresenter()
    presenter.on_event(
        RuntimeEvent(
            "plan_progress",
            data={
                "revision": 1,
                "trigger": "step_completed",
                "changed_step_id": "inspect",
                "steps": [
                    {"index": 1, "id": "inspect", "description": "Inspect the project", "status": "completed", "result": "x" * 250},
                    {"index": 2, "id": "implement", "description": "Update the implementation", "status": "running", "result": None},
                    {"index": 3, "id": "test", "description": "Run tests", "status": "pending", "result": None},
                    {"index": 4, "id": "failed", "description": "Failed step", "status": "failed", "result": "permission denied"},
                    {"index": 5, "id": "old", "description": "Old step", "status": "superseded", "result": None},
                ],
            },
        )
    )

    output = capsys.readouterr().out
    assert output.startswith("PLAN REVISION 1\n✓ 1. Inspect the project — COMPLETED\n  result: ")
    assert "..." in output
    assert "→ 2. Update the implementation — RUNNING\n" in output
    assert "• 3. Run tests — PENDING\n" in output
    assert "✗ 4. Failed step — FAILED\n  result: permission denied\n" in output
    assert "↺ 5. Old step — SUPERSEDED\n" in output


def test_presenter_renders_replan_replacements_completed_work_and_new_plan(capsys) -> None:
    presenter = TerminalPresenter()
    presenter.on_event(
        RuntimeEvent(
            "replan_applied",
            data={
                "revision": 2,
                "reason": "The result requires a different output file.",
                "previous_steps": [
                    {"index": 1, "id": "done", "description": "Inspect the project", "status": "completed", "result": "inspected"},
                    {"index": 2, "id": "old", "description": "Write the old result", "status": "superseded", "result": None},
                ],
                "steps": [
                    {"index": 1, "id": "new", "description": "Write the revised result", "status": "pending", "result": None},
                ],
            },
        )
    )

    assert capsys.readouterr().out == (
        "REPLAN APPLIED — revision 2\n"
        "REASON: The result requires a different output file.\n"
        "COMPLETED STEPS\n"
        "✓ 1. Inspect the project — COMPLETED\n"
        "  result: inspected\n"
        "REPLACED STEPS\n"
        "↺ 2. Write the old result — SUPERSEDED\n"
        "NEW PLAN\n"
        "• 1. Write the revised result — PENDING\n"
    )


def test_presenter_keeps_legacy_plan_events_compatible(capsys) -> None:
    presenter = TerminalPresenter()
    presenter.on_event(RuntimeEvent("plan", "1. Inspect the project.", {"revision": 1}))
    presenter.on_event(RuntimeEvent("replan_applied", "1. Write the revised result.", {"reason": "changed"}))

    assert capsys.readouterr().out == (
        "PLAN\n1. Inspect the project.\n"
        "REPLAN APPLIED\n1. Write the revised result.\n"
    )
