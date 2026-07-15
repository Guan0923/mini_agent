"""Terminal rendering for structured runtime events."""

from __future__ import annotations

from mini_agent.runtime import RuntimeEvent


class TerminalPresenter:
    """Renders runtime events without leaking console concerns into the runner."""

    def __init__(self) -> None:
        self._thinking_open = False

    def on_event(self, event: RuntimeEvent) -> None:
        if event.kind == "run_started":
            print(f"RUN {event.data['run_id']} started")
        elif event.kind == "thinking_start":
            print("THINKING")
            self._thinking_open = True
        elif event.kind == "thinking_delta":
            print(event.message, end="", flush=True)
        elif event.kind == "thinking_end":
            if self._thinking_open:
                print()
            self._thinking_open = False
        elif event.kind == "strategy":
            print(f"STRATEGY {event.message} — {event.data['reason']}")
        elif event.kind == "tool_call":
            print(f"CALL  {event.message} {event.data['arguments']}")
        elif event.kind == "tool_result":
            print(f"RESULT\n{event.message}")
        elif event.kind == "tool_failed":
            print(f"TOOL FAILED {event.data['tool']}: {event.message}")
        elif event.kind == "retry":
            print(f"RETRY {event.data['tool']}: {event.message}")
        elif event.kind == "replan_requested":
            print(f"REPLAN REQUESTED\n{event.message}")
        elif event.kind == "replan_applied":
            print(f"REPLAN APPLIED\n{event.message}")
        elif event.kind == "response":
            print(f"RESPONSE\n{event.message}")
        elif event.kind == "plan":
            print(f"PLAN\n{event.message}")
        elif event.kind == "error":
            print(f"ERROR {event.message}")
        elif event.kind == "approval_requested":
            print(f"APPROVAL REQUIRED — {event.message}")
        elif event.kind == "approval_granted":
            print(f"APPROVED — {event.message}")
        elif event.kind == "feedback_received":
            print(f"SUPPLEMENT — {event.message}")
        elif event.kind == "cancelled":
            print("CANCELLED")
        elif event.kind == "run_finished":
            print(f"RUN {event.data['run_id']} {event.message}")
