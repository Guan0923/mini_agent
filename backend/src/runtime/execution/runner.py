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

from ..core.config import RunnerSettings
from ..core.context import AgentRuntime, RuntimeServices, RuntimeState
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
from .lifecycle.finalization import finish_run
from .lifecycle.interrupts import default_interrupt
from .lifecycle.outcomes import cancel_run, fail_run
from .lifecycle.publisher import RunEventPublisher
from .skills import SkillActivator
from .workflows import ExecutionWorkflow


class AgentRunner:
    def __init__(
        self,
        planner: object,
        tools: object,
        log_full_messages: bool = True,
        checkpoints: CheckpointStore | None = None,
        hooks: Iterable[AgentHook] = (),
        max_tool_calls: int | None = None,
        max_transport_retries: int = 5,
        skill_catalog: object | None = None,
        skill_auto_select: bool = False,
        workspace_root: str | None = None,
        subagents: object | None = None,
        resources: tuple[object, ...] = (),
        provider_config_resolver=None,
    ) -> None:
        self.planner = planner
        self.tools = tools
        self.skill_catalog = skill_catalog
        self.skill_auto_select = skill_auto_select
        self.workspace_root = workspace_root
        self.subagents = subagents
        self._resources = resources
        self.provider_config_resolver = provider_config_resolver
        self._closed = False
        self.settings = RunnerSettings(
            max_transport_retries=max_transport_retries,
            max_tool_calls=32 if max_tool_calls is None else max_tool_calls,
            log_full_messages=log_full_messages,
        )
        self.checkpoints = checkpoints
        self.hooks = HookManager(hooks)
        self._skills = SkillActivator()
        self._execution = ExecutionWorkflow()
        self._plan_mode = PlanModeWorkflow()

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
        # ``mode`` is the initial runtime configuration for a new turn.  Keep
        # it in the state snapshot as well as on ``RunState`` so the dynamic
        # message-tree configuration and the legacy runner start from the same
        # value.  Without this assignment the state default (``agent``) would
        # overwrite an explicitly requested Plan run at dispatch time.
        if mode in {"agent", "plan"}:
            runtime.state.running_mode = mode
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
            skill_auto_select=self.skill_auto_select,
            checkpoint_store=self.checkpoints,
            runtime_store=runtime_store,  # type: ignore[arg-type]
            hooks=self.hooks,
            subagents=self.subagents,
            provider_config_resolver=self.provider_config_resolver,
        )
        return AgentRuntime(state=state, services=services)

    def bind(self, runtime: AgentRuntime) -> AgentRuntime:
        """Rebind non-serializable services after loading a persisted RuntimeState."""

        runtime.services.planner = self.planner
        runtime.services.tools = self.tools
        runtime.services.checkpoint_store = self.checkpoints
        runtime.services.skill_catalog = self.skill_catalog
        runtime.services.skill_auto_select = self.skill_auto_select
        runtime.services.hooks = self.hooks
        runtime.services.subagents = self.subagents
        runtime.services.provider_config_resolver = self.provider_config_resolver
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
        # The active dynamic node is the authoritative workflow mode.  A
        # running PATCH may change it between model/tool decision boundaries;
        # refresh the RunState mode before routing so prompt/tool selection
        # observes the same value that is exposed at the node top level.
        if runtime.state.running_mode in {"agent", "plan"}:
            run.mode = runtime.state.running_mode  # type: ignore[assignment]
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
                    result=RunHookResult(run.status, run.final_answer),
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
        else:
            self._execution.run(runtime)

    def _dispatch(self, runtime: AgentRuntime) -> None:
        runtime.apply_pending_runtime_config()
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
                        "max_transport_retries": settings.max_transport_retries,
                        "max_tool_calls": settings.max_tool_calls,
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
            self._execution.run(runtime)

    def _default_interrupt(self, runtime: AgentRuntime):
        return default_interrupt(runtime)

    def _finish(self, runtime: AgentRuntime, *, started_at: float) -> RunState:
        return finish_run(runtime, started_at=started_at)

    def close(self) -> None:
        """Close only resources created for this runner."""

        if self._closed:
            return
        self._closed = True
        for resource in reversed(self._resources):
            close = getattr(resource, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> AgentRunner:
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()
