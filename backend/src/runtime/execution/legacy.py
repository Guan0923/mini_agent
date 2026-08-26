"""Compatibility facade for pre-Runtime embedding callers."""

from __future__ import annotations

from backend.domain import (
    RunState,
    UserMessage,
    message_from_dict,
    new_run_id,
)

from ..core.context import AgentRuntime
from .runner import AgentRunner


class LegacyAgentRunner(AgentRunner):
    """Deprecated facade for pre-Runtime embedding callers."""

    def run(self, task, *args, **kwargs):  # type: ignore[override]
        if isinstance(task, AgentRuntime):
            return super().run(task)
        confirm = args[0] if args else kwargs.pop("confirm", None)
        conversation = kwargs.pop("conversation", None)
        messages = [message_from_dict(item) for item in (conversation or [])]
        runtime = self.new_runtime(
            task=task,
            mode=kwargs.pop("mode", "agent"),
            messages=messages,
            run_id=kwargs.pop("run_id", None),
            on_event=kwargs.pop("on_event", None),
            interrupt=kwargs.pop("interrupt", None),
            confirm=confirm,
        )
        if kwargs:
            unknown = ", ".join(sorted(kwargs))
            raise TypeError(f"Unknown LegacyAgentRunner.run arguments: {unknown}")
        result = super().run(runtime)
        if result.handoff is not None:
            handoff = result.handoff
            if handoff.compact_before:
                super().compact_context(runtime)
            turn_start_index = len(runtime.state.messages)
            runtime.state.messages.append(UserMessage(content=handoff.task))
            runtime.state.running_mode = handoff.mode
            runtime.state.current_run = RunState(
                task=handoff.task,
                mode=handoff.mode,
                run_id=new_run_id(),
                turn_start_index=turn_start_index,
                history=runtime.state.messages,
                active_skills=list(handoff.active_skills),
            )
            runtime.state.active_message = None
            runtime.state.active_tool_index = None
            runtime.state.turn_usage = None
            runtime.state.status = "running"
            result = super().run(runtime)
        if conversation is not None and result.mode == "agent":
            conversation.extend(
                [
                    {"role": "user", "content": result.task},
                    {"role": "assistant", "content": result.final_answer or ""},
                ]
            )
        return result
