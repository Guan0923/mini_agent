"""Application orchestrator whose execution entry accepts only AgentRuntime."""

from __future__ import annotations

from collections.abc import Iterable

from mini_agent.domain import (
    AssistantMessage,
    RunState,
    ToolSpec,
    UserMessage,
    message_from_dict,
    new_run_id,
    new_session_id,
)
from mini_agent.tools import ToolError

from ..conversation.steering import consume_steering
from ..core.config import RunnerSettings
from ..core.context import AgentRuntime, RunSummary, RuntimeServices, RuntimeState
from ..core.contracts import InterruptDecision, InterruptRequest
from ..core.events import RuntimeEvent
from ..core.hooks import (
    AgentHook,
    HookCancellation,
    HookExecutionError,
    HookManager,
    HookOutcome,
    RunHookContext,
    RunHookInfo,
    RunHookResult,
)
from ..persistence.checkpointing import CheckpointStore
from ..planning.mode import PlanModeWorkflow
from .cancellation import cancel_if_requested
from .outcomes import cancel_run, fail_run
from .publisher import RunEventPublisher
from .routing import StrategyRouter
from .workflows import DynamicReplanWorkflow, ReactiveWorkflow


class AgentRunner:
    def __init__(
        self,
        planner: object,
        tools: object,
        max_retries: int = 1,
        max_tool_recoveries: int = 2,
        max_actions: int | None = None,
        max_replans: int = 2,
        strategy: str = "auto",
        log_full_messages: bool = True,
        checkpoints: CheckpointStore | None = None,
        hooks: Iterable[AgentHook] = (),
        max_model_repairs: int = 1,
        max_transport_retries: int = 2,
        max_model_turns: int = 8,
        max_tool_calls: int | None = None,
    ) -> None:
        self.planner = planner
        self.tools = tools
        if max_actions is not None and max_tool_calls is not None:
            raise ValueError("max_actions and max_tool_calls cannot be used together.")
        resolved_tool_calls = max_actions if max_actions is not None else max_tool_calls

        self.settings = RunnerSettings(
            max_retries=max_retries,
            max_model_repairs=max_model_repairs,
            max_transport_retries=max_transport_retries,
            max_tool_recoveries=max_tool_recoveries,
            max_model_turns=max_model_turns,
            max_tool_calls=32 if resolved_tool_calls is None else resolved_tool_calls,
            max_replans=max_replans,
            strategy=strategy,  # type: ignore[arg-type]
            log_full_messages=log_full_messages,
        )
        self.checkpoints = checkpoints
        self.hooks = HookManager(hooks)
        self._router = StrategyRouter()
        self._reactive = ReactiveWorkflow()
        self._plan_mode = PlanModeWorkflow()
        self._dynamic_replan = DynamicReplanWorkflow()

    def new_runtime(
        self,
        *,
        task: str,
        mode: str = "agent",
        session_id: str | None = None,
        messages: list | None = None,
        run_id: str | None = None,
        runtime_store: object | None = None,
        on_event=None,
        interrupt=None,
        confirm=None,
    ) -> AgentRuntime:
        runtime = self.empty_runtime(
            session_id=session_id or new_session_id(),
            messages=list(messages or []),
            runtime_store=runtime_store,
        )
        history = runtime.state.messages
        turn_start_index = len(history)
        history.append(UserMessage(content=task))
        runtime.state.current_run = RunState(
            task=task,
            mode=mode,  # type: ignore[arg-type]
            run_id=run_id or new_run_id(),
            turn_start_index=turn_start_index,
            history=history,
        )
        runtime.state.status = "running"
        runtime.services.on_event = on_event
        runtime.services.interrupt = interrupt
        runtime.services.confirm = confirm
        return runtime

    def empty_runtime(
        self,
        *,
        session_id: str,
        messages: list | None = None,
        runtime_store: object | None = None,
    ) -> AgentRuntime:
        specs: list[ToolSpec] = self.tools.specs() if hasattr(self.tools, "specs") else []
        state = RuntimeState(
            session_id=session_id,
            messages=list(messages or []),
            runner_settings=self.settings,
            tool_specs=specs,
            current_run=None,
            status="idle",
        )
        services = RuntimeServices(
            planner=self.planner,
            tools=self.tools,
            checkpoint_store=self.checkpoints,
            runtime_store=runtime_store,  # type: ignore[arg-type]
            hooks=self.hooks,
        )
        return AgentRuntime(state=state, services=services)

    def bind(self, runtime: AgentRuntime) -> AgentRuntime:
        """Rebind non-serializable services after loading a persisted RuntimeState."""

        runtime.services.planner = self.planner
        runtime.services.tools = self.tools
        runtime.services.checkpoint_store = self.checkpoints
        runtime.services.hooks = self.hooks
        runtime.state.runner_settings = self.settings
        return runtime

    def run(self, runtime: AgentRuntime) -> RunState:
        """Execute one turn using the single runtime parameter."""

        self.bind(runtime)
        runtime.state.status = "running"
        runtime.services.publish = RunEventPublisher(runtime)
        runtime.services.interrupt = runtime.services.interrupt or self._default_interrupt(runtime)
        run = runtime.run
        run.history = runtime.state.messages
        publish = runtime.services.publish
        context = RunHookContext(RunHookInfo(runtime.state.session_id, run.run_id, run.task, run.mode))
        try:
            self.hooks.run_run(
                context,
                lambda _context: self._dispatch(runtime),
                lambda _result: HookOutcome(
                    status=(
                        "succeeded"
                        if run.status == "completed"
                        else run.status
                        if run.status in {"failed", "cancelled"}
                        else "failed"
                    ),
                    result=RunHookResult(run.status, run.strategy, run.final_answer),
                ),
                publish,
            )
        except HookCancellation:
            cancel_run(runtime)
        except HookExecutionError as exc:
            fail_run(
                runtime,
                str(exc),
                hook=exc.hook,
                lifecycle=exc.lifecycle,
                phase=exc.phase,
                error_type=exc.error_type,
            )
        return self._finish(runtime)

    def _dispatch(self, runtime: AgentRuntime) -> None:
        runtime.run.add_event("run_started", "Run started")
        assert runtime.services.publish is not None
        runtime.services.publish(RuntimeEvent("run_started", "started"))
        if cancel_if_requested(runtime):
            return
        if runtime.run.mode == "plan":
            self._plan_mode.run(runtime)
        else:
            self._run_from_router(runtime)

    def _run_from_router(self, runtime: AgentRuntime) -> None:
        while True:
            if self._router.resolve(runtime) is None:
                return
            if cancel_if_requested(runtime):
                return
            if consume_steering(runtime, phase="after_strategy_selection") is None:
                break
            runtime.run.strategy = None
        self._execute(runtime)

    def _execute(self, runtime: AgentRuntime) -> None:
        if runtime.run.strategy == "dynamic_replan":
            self._dynamic_replan.run(runtime)
        else:
            self._reactive.run(runtime)

    def _default_interrupt(self, runtime: AgentRuntime):
        def decide(request: InterruptRequest) -> InterruptDecision:
            if request.kind == "plan":
                return InterruptDecision("cancel")
            tool = request.data.get("tool")
            if not isinstance(tool, str):
                return InterruptDecision("cancel")
            try:
                requires_confirmation = runtime.services.tools.requires_confirmation(tool)
            except ToolError:
                return InterruptDecision("cancel")
            if not requires_confirmation:
                return InterruptDecision("continue")
            message = f"{tool} requires confirmation before an external or destructive operation."
            confirm = runtime.services.confirm
            return InterruptDecision("continue" if confirm is not None and confirm(message) else "cancel")

        return decide

    def _finish(self, runtime: AgentRuntime) -> RunState:
        run = runtime.run
        runtime.state.usage = runtime.state.turn_usage
        runtime.state.turn_usage = None
        runtime.state.status = "idle"
        if not any(summary.run_id == run.run_id for summary in runtime.state.run_history):
            runtime.state.run_history.append(RunSummary(run.run_id, run.task, run.status, run.mode, run.final_answer))
        run.add_event("run_finished", "Run finished", status=run.status)
        if runtime.services.publish is not None:
            runtime.services.publish(RuntimeEvent("run_finished", run.status, {"final_answer": run.final_answer or ""}))
        runtime.save()
        return run


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
            if handoff.new_session:
                proposal = (result.final_answer or "").strip()
                if not proposal:
                    raise RuntimeError("Cannot start an isolated handoff without a completed plan proposal.")
                runtime = self.new_runtime(
                    task=handoff.task,
                    mode=handoff.mode,
                    session_id=new_session_id(),
                    messages=[AssistantMessage(content=proposal)],
                    on_event=runtime.services.on_event,
                    interrupt=runtime.services.interrupt,
                    confirm=runtime.services.confirm,
                )
            else:
                turn_start_index = len(runtime.state.messages)
                runtime.state.messages.append(UserMessage(content=handoff.task))
                runtime.state.current_run = RunState(
                    task=handoff.task,
                    mode=handoff.mode,
                    run_id=new_run_id(),
                    turn_start_index=turn_start_index,
                    history=runtime.state.messages,
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
