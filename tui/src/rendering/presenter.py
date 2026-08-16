"""Terminal rendering for structured runtime events."""

from __future__ import annotations

from collections.abc import Callable

from backend.runtime import RuntimeEvent

_PLAN_RESULT_MAX_CHARS = 240
_PLAN_STATUS_DISPLAY = {
    "completed": ("✓", "COMPLETED"),
    "running": ("→", "RUNNING"),
    "pending": ("•", "PENDING"),
    "failed": ("✗", "FAILED"),
    "superseded": ("↺", "SUPERSEDED"),
}


def _console_write(text: str, end: str = "\n") -> None:
    print(text, end=end, flush=end == "")


class TerminalPresenter:
    """Renders runtime events without leaking console concerns into the runner."""

    def __init__(self, write: Callable[[str, str], None] | None = None) -> None:
        self._thinking_open = False
        self._response_open = False
        self._write = write or _console_write

    def on_event(self, event: RuntimeEvent) -> None:
        if event.kind == "run_started":
            self._write(f"RUN {event.data['run_id']} started")
        elif event.kind == "skills_selected":
            self._write(f"SKILLS {event.message}")
        elif event.kind == "thinking_start":
            self._write("THINKING")
            self._thinking_open = True
        elif event.kind == "thinking_delta":
            self._write(event.message, "")
        elif event.kind == "thinking_end":
            if self._thinking_open:
                self._write("")
            self._thinking_open = False
        elif event.kind == "response_start":
            self._write("RESPONSE")
            self._response_open = True
        elif event.kind == "response_delta":
            self._write(event.message, "")
        elif event.kind == "response_end":
            if self._response_open:
                self._write("")
            self._response_open = False
        elif event.kind == "strategy":
            self._write(f"STRATEGY {event.message} — {event.data['reason']}")
        elif event.kind == "model_repair":
            self._write(f"MODEL FORMAT RETRY — {event.message}")
        elif event.kind == "model_retry":
            self._write(f"MODEL RETRY {event.data['attempt']} — {event.message}")
        elif event.kind == "tool_call":
            self._write(f"CALL  {event.message} {event.data['arguments']}")
        elif event.kind == "tool_result":
            self._write(f"RESULT\n{event.message}")
        elif event.kind == "tool_failed":
            self._write(f"TOOL FAILED {event.data['tool']}: {event.message}")
        elif event.kind == "retry":
            self._write(f"RETRY {event.data['tool']}: {event.message}")
        elif event.kind == "tool_recovery":
            self._write(f"TOOL RECOVERY {event.data['attempt']} — {event.data['tool']}: {event.message}")
        elif event.kind == "replan_requested":
            self._write(f"REPLAN REQUESTED\n{event.message}")
        elif event.kind == "replan_applied":
            if self._has_step_snapshot(event.data, "previous_steps") and self._has_step_snapshot(event.data, "steps"):
                self._render_replan(event)
            else:
                self._write(f"REPLAN APPLIED\n{event.message}")
        elif event.kind == "response":
            if not event.data.get("streamed"):
                self._write(f"RESPONSE\n{event.message}")
        elif event.kind == "plan":
            if self._has_step_snapshot(event.data, "steps"):
                self._render_plan(event.data)
            elif not event.data.get("streamed"):
                self._write(f"PLAN\n{event.message}")
        elif event.kind == "plan_progress":
            if self._has_step_snapshot(event.data, "steps"):
                self._render_plan(event.data)
        elif event.kind == "error":
            self._write(f"ERROR {event.message}")
            diagnostics = event.data.get("provider_diagnostics")
            if isinstance(diagnostics, dict):
                values = [
                    f"{name}={diagnostics[name]}"
                    for name in ("finish_reason", "content_chars", "reasoning_chars")
                    if name in diagnostics
                ]
                if values:
                    self._write(f"MODEL DIAGNOSTICS {' '.join(values)}")
        elif event.kind == "approval_requested":
            self._write(f"APPROVAL REQUIRED — {event.message}")
        elif event.kind == "approval_granted":
            self._write(f"APPROVED — {event.message}")
        elif event.kind == "feedback_received":
            self._write(f"SUPPLEMENT — {event.message}")
        elif event.kind == "steering_received":
            self._write(f"STEERING RECEIVED — {event.data['message_count']} message(s)")
        elif event.kind == "steering_applied":
            self._write(f"STEERING APPLIED — {event.data['phase']}")
        elif event.kind == "handoff_created":
            self._write(f"HANDOFF — {event.message}")
        elif event.kind == "subagent_queued":
            self._write(f"SUBAGENTS QUEUED — {event.data['count']}")
        elif event.kind == "subagent_started":
            self._write(f"SUBAGENT {event.data['task_id']} STARTED")
        elif event.kind == "subagent_write_requested":
            self._write(f"SUBAGENT {event.data['task_id']} REQUESTED {event.data['tool']}")
        elif event.kind in {"subagent_completed", "subagent_failed", "subagent_indeterminate"}:
            self._write(f"SUBAGENT {event.data.get('task_id', '')} — {event.message}")
        elif event.kind == "cancelled":
            self._write("CANCELLED")
        elif event.kind == "run_finished":
            self._write(f"RUN {event.data['run_id']} {event.message}")

    @staticmethod
    def _has_step_snapshot(data: dict[str, object], key: str) -> bool:
        return isinstance(data.get(key), list)

    @staticmethod
    def _truncate_result(value: str) -> str:
        if len(value) <= _PLAN_RESULT_MAX_CHARS:
            return value
        return f"{value[: _PLAN_RESULT_MAX_CHARS - 3]}..."

    @classmethod
    def _step_lines(
        cls,
        steps: list[object],
        *,
        statuses: frozenset[str] | None = None,
    ) -> list[str]:
        lines: list[str] = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            status = str(step.get("status") or "pending")
            if statuses is not None and status not in statuses:
                continue
            symbol, label = _PLAN_STATUS_DISPLAY.get(status, ("•", status.upper()))
            index = step.get("index", "?")
            description = str(step.get("description") or step.get("id") or "Unnamed step")
            lines.append(f"{symbol} {index}. {description} — {label}")
            result = step.get("result")
            if isinstance(result, str) and result:
                lines.append(f"  result: {cls._truncate_result(result)}")
        return lines

    def _render_plan(self, data: dict[str, object]) -> None:
        self._write(f"PLAN REVISION {data.get('revision', '?')}")
        steps = data.get("steps")
        for line in self._step_lines(steps if isinstance(steps, list) else []):
            self._write(line)

    def _render_replan(self, event: RuntimeEvent) -> None:
        previous_steps = event.data.get("previous_steps")
        previous = previous_steps if isinstance(previous_steps, list) else []
        self._write(f"REPLAN APPLIED — revision {event.data.get('revision', '?')}")
        self._write(f"REASON: {event.data.get('reason') or event.message}")
        self._write("COMPLETED STEPS")
        completed = self._step_lines(previous, statuses=frozenset({"completed"}))
        for line in completed or ["(none)"]:
            self._write(line)
        self._write("REPLACED STEPS")
        replaced = self._step_lines(previous, statuses=frozenset({"superseded"}))
        for line in replaced or ["(none)"]:
            self._write(line)
        self._write("NEW PLAN")
        steps = event.data.get("steps")
        for line in self._step_lines(steps if isinstance(steps, list) else []):
            self._write(line)
