"""Application orchestrator whose execution entry accepts only AgentRuntime."""

from __future__ import annotations

from collections.abc import Iterable
from time import perf_counter

from backend.domain import (
    PlanningError,
    RunProvenance,
    RunState,
    SkillSnapshot,
    ToolSpec,
    UserMessage,
    new_run_id,
    new_session_id,
)
from backend.planning.base import ContextCompactor
from backend.planning.context_management import ContextCompactionResult
from backend.tools import ToolError

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
from .lifecycle.cancellation import cancel_if_requested
from .lifecycle.outcomes import cancel_run, fail_run
from .lifecycle.publisher import RunEventPublisher
from .routing import StrategyRouter
from .skills import SkillActivator
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
        max_model_repairs: int = 2,
        max_transport_retries: int = 2,
        max_model_turns: int = 8,
        max_tool_calls: int | None = None,
        skill_catalog: object | None = None,
        workspace_root: str | None = None,
        subagents: object | None = None,
    ) -> None:
        self.planner = planner
        self.tools = tools
        self.skill_catalog = skill_catalog
        self.workspace_root = workspace_root
        self.subagents = subagents
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
        self._skills = SkillActivator()
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
        active_skills: list[SkillSnapshot] | tuple[SkillSnapshot, ...] | None = None,
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
            active_skills=list(active_skills or ()),
            provenance=RunProvenance(trigger="embedding", workspace_root=self.workspace_root),
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
            workspace_root=self.workspace_root,
            messages=list(messages or []),
            runner_settings=self.settings,
            tool_specs=specs,
            current_run=None,
            status="idle",
        )
        services = RuntimeServices(
            planner=self.planner,
            tools=self.tools,
            skill_catalog=self.skill_catalog,
            checkpoint_store=self.checkpoints,
            runtime_store=runtime_store,  # type: ignore[arg-type]
            hooks=self.hooks,
            subagents=self.subagents,
        )
        return AgentRuntime(state=state, services=services)

    def bind(self, runtime: AgentRuntime) -> AgentRuntime:
        """Rebind non-serializable services after loading a persisted RuntimeState."""

        runtime.services.planner = self.planner
        runtime.services.tools = self.tools
        runtime.services.checkpoint_store = self.checkpoints
        runtime.services.skill_catalog = self.skill_catalog
        runtime.services.hooks = self.hooks
        runtime.services.subagents = self.subagents
        runtime.state.runner_settings = self.settings
        return runtime

    def compact_context(self, runtime: AgentRuntime) -> ContextCompactionResult:
        """Compact an idle runtime through the configured planner capability."""

        self.bind(runtime)
        if runtime.state.status == "running":
            raise RuntimeError("Current turn is still running; context cannot be compacted.")
        runtime.services.publish = RunEventPublisher(runtime)
        if not isinstance(self.planner, ContextCompactor):
            raise PlanningError("Context compaction requires the LLM planner.")
        if runtime.state.current_run is None:
            message_count = len(runtime.state.messages)
            return ContextCompactionResult(False, message_count, message_count)
        return self.planner.compact_context(runtime)

    def run(self, runtime: AgentRuntime) -> RunState:
        """Execute one turn using the single runtime parameter."""

        return self._run_attempt(runtime, resumed=False)

    def resume(self, runtime: AgentRuntime) -> RunState:
        """Continue a reconstructed durable attempt without replaying run setup."""

        return self._run_attempt(runtime, resumed=True)

    def _run_attempt(self, runtime: AgentRuntime, *, resumed: bool) -> RunState:

        self.bind(runtime)
        started_at = perf_counter()
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
                lambda _context: self._resume_dispatch(runtime) if resumed else self._dispatch(runtime),
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
        return self._finish(runtime, started_at=started_at)

    def _resume_dispatch(self, runtime: AgentRuntime) -> None:
        run = runtime.run
        run.add_event("run_resumed", "Run resumed", source_run_id=run.provenance.source_run_id)
        assert runtime.services.publish is not None
        runtime.services.publish(
            RuntimeEvent(
                "run_resumed",
                "resumed",
                {
                    "session_id": runtime.state.session_id,
                    "workflow_id": run.provenance.workflow_id,
                    "attempt": run.provenance.attempt,
                    "source_run_id": run.provenance.source_run_id,
                },
            )
        )
        if cancel_if_requested(runtime):
            return
        if run.mode == "plan":
            self._plan_mode.run(runtime)
        elif run.strategy == "dynamic_replan":
            self._dynamic_replan.resume(runtime)
        else:
            self._reactive.run(runtime)

    def _dispatch(self, runtime: AgentRuntime) -> None:
        runtime.run.add_event("run_started", "Run started")
        assert runtime.services.publish is not None
        settings = runtime.state.runner_settings
        runtime.services.publish(
            RuntimeEvent(
                "run_started",
                "started",
                {
                    "schema_version": 2,
                    "session_id": runtime.state.session_id,
                    "provider": runtime.state.provider,
                    "model": runtime.state.model,
                    "runner_settings": {
                        "max_retries": settings.max_retries,
                        "max_model_repairs": settings.max_model_repairs,
                        "max_transport_retries": settings.max_transport_retries,
                        "max_tool_recoveries": settings.max_tool_recoveries,
                        "max_model_turns": settings.max_model_turns,
                        "max_tool_calls": settings.max_tool_calls,
                        "max_replans": settings.max_replans,
                        "strategy": settings.strategy,
                        "log_full_messages": settings.log_full_messages,
                    },
                },
            )
        )
        if cancel_if_requested(runtime):
            return
        if not self._skills.activate(runtime):
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

    def _finish(self, runtime: AgentRuntime, *, started_at: float) -> RunState:
        run = runtime.run
        runtime.state.usage = runtime.state.turn_usage
        runtime.state.turn_usage = None
        runtime.state.status = "idle"
        if not any(summary.run_id == run.run_id for summary in runtime.state.run_history):
            runtime.state.run_history.append(
                RunSummary(
                    run.run_id,
                    run.task,
                    run.status,
                    run.mode,
                    run.final_answer,
                    run.provenance.workflow_id,
                    run.provenance.attempt,
                )
            )
        run.add_event("run_finished", "Run finished", status=run.status)
        if runtime.services.publish is not None:
            counts: dict[str, int] = {}
            for message in run.runtime_messages:
                counts[message.kind] = counts.get(message.kind, 0) + 1
            runtime.services.publish(
                RuntimeEvent(
                    "run_finished",
                    run.status,
                    {
                        "schema_version": 2,
                        "final_answer": run.final_answer or "",
                        "duration_ms": round((perf_counter() - started_at) * 1000, 3),
                        "usage": runtime.state.usage,
                        "event_counts": counts,
                        "model_calls": counts.get("model_request", 0),
                        "tool_calls": counts.get("tool_call", 0),
                        "retries": counts.get("retry", 0) + counts.get("model_retry", 0),
                        "replans": counts.get("replan_requested", 0),
                        "active_skills": [{"name": skill.name, "sha256": skill.sha256} for skill in run.active_skills],
                    },
                )
            )
        runtime.save()
        return run
