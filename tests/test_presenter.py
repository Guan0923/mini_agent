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
    presenter.on_event(RuntimeEvent("response", "Hello."))

    assert capsys.readouterr().out == (
        "STRATEGY reactive — Simple task.\n"
        "THINKING\n"
        "Thinking.\n"
        "MODEL FORMAT RETRY — Malformed model action was repaired automatically.\n"
        "TOOL RECOVERY 1 — run_command: missing.txt\n"
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
