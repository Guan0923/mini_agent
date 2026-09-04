"""Application orchestrator whose execution entry accepts only AgentRuntime."""

from __future__ import annotations

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
    safe_error_message,
)
from backend.jobs import JobRegistry, JobScope, JobScopeKind
from backend.planning.base import ContextCompactor, TitleGenerator
from backend.planning.context_management import ContextCompactionResult

from ..core.config import RunnerSettings
from ..core.context import AgentRuntime, RuntimeServices, RuntimeState
from ..core.contracts import WorkflowModeChanged
from ..core.events import RuntimeEvent
from ..core.hooks import (
    HookErrorInfo,
    HookExecutionError,
    HookOutcome,
    HookRejected,
    RunHookContext,
    RunHookInfo,
    RunHookResult,
    after_run_hook_manager,
    before_run_hook_manager,
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
        max_tool_calls: int | None = None,
        max_transport_retries: int = 5,
        skill_catalog: object | None = None,
        skills_enabled: bool = True,
        skill_auto_select: bool = False,
        project_skill_gate: object | None = None,
        workspace_root: str | None = None,
        project_cwd: str | None = None,
        subagents: object | None = None,
        resources: tuple[object, ...] = (),
        provider_config_resolver=None,
        job_registry: JobRegistry | None = None,
        job_scope: JobScope | None = None,
        parent_job_id: str | None = None,
        sandbox_launcher: object | None = None,
        sandbox_config: dict[str, object] | None = None,
        sandbox_user_id: str | None = None,
        todo_store=None,
    ) -> None:
        self.planner = planner
        self.tools = tools
        self.todo_store = todo_store
        self.skill_catalog = skill_catalog
        self.skills_enabled = skills_enabled
        self.skill_auto_select = skill_auto_select
        self.project_skill_gate = project_skill_gate
        self.workspace_root = workspace_root
        self.project_cwd = project_cwd
        self.subagents = subagents
        self._resources = resources
        self.provider_config_resolver = provider_config_resolver
        self.job_registry = job_registry or JobRegistry()
        self._owns_job_registry = job_registry is None
        self.job_scope = job_scope or self.job_registry.root_scope().child(JobScopeKind.THREAD)
        self._parent_job_id = parent_job_id
        self._closed = False
        self.settings = RunnerSettings(
            max_transport_retries=max_transport_retries,
            max_tool_calls=32 if max_tool_calls is None else max_tool_calls,
            log_full_messages=log_full_messages,
        )
        self.checkpoints = checkpoints
        self.sandbox_launcher = sandbox_launcher
        self.sandbox_config = dict(sandbox_config or {})
        self.sandbox_user_id = sandbox_user_id
        self._skills = SkillActivator(self.project_skill_gate)
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
        parent_job_id: str | None = None,
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
            provenance=RunProvenance(
                trigger="embedding",
                workspace_root=self.workspace_root,
                project_cwd=self.project_cwd,
            ),
        )
        runtime.state.status = "running"
        runtime.services.job_scope = self.job_scope.child(
            JobScopeKind.RUN,
            session_id=runtime.state.session_id,
            run_id=runtime.state.current_run.run_id,
            parent_job_id=parent_job_id or self._parent_job_id,
        )
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
            project_cwd=self.project_cwd,
            messages=list(messages or []),
            runner_settings=self.settings,
            tool_specs=specs,
            current_run=None,
            status="idle",
        )
        services = RuntimeServices(
            planner=self.planner,
            tools=self.tools,
            todo_store=self.todo_store,
            skill_catalog=self.skill_catalog,
            skills_enabled=self.skills_enabled,
            skill_auto_select=self.skill_auto_select,
            project_skill_gate=self.project_skill_gate,
            checkpoint_store=self.checkpoints,
            runtime_store=runtime_store,  # type: ignore[arg-type]
            subagents=self.subagents,
            sandbox_launcher=self.sandbox_launcher,
            sandbox_config=self.sandbox_config,
            sandbox_user_id=self.sandbox_user_id,
            provider_config_resolver=self.provider_config_resolver,
            job_scope=self.job_scope,
        )
        return AgentRuntime(state=state, services=services)

    def bind(self, runtime: AgentRuntime) -> AgentRuntime:
        """Rebind non-serializable services after loading a persisted RuntimeState."""

        runtime.services.planner = self.planner
        runtime.services.tools = self.tools
        runtime.services.todo_store = self.todo_store
        runtime.services.checkpoint_store = self.checkpoints
        runtime.services.skill_catalog = self.skill_catalog
        runtime.services.skills_enabled = self.skills_enabled
        runtime.services.skill_auto_select = self.skill_auto_select
        runtime.services.project_skill_gate = self.project_skill_gate
        runtime.services.subagents = self.subagents
        runtime.services.sandbox_launcher = self.sandbox_launcher
        runtime.services.sandbox_config = self.sandbox_config
        runtime.services.sandbox_user_id = self.sandbox_user_id
        runtime.services.provider_config_resolver = self.provider_config_resolver
        if runtime.services.job_scope is None and runtime.state.current_run is not None:
            runtime.services.job_scope = self.job_scope.child(
                JobScopeKind.RUN,
                session_id=runtime.state.session_id,
                run_id=runtime.state.current_run.run_id,
                parent_job_id=self._parent_job_id,
            )
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

    def generate_title(self, runtime: AgentRuntime, first_user_text: str) -> str:
        """Generate one isolated title after the conversation Turn has finished."""

        self.bind(runtime)
        if runtime.state.status == "running":
            raise RuntimeError("Current turn is still running; its title cannot be generated.")
        if runtime.state.current_run is None:
            raise RuntimeError("Conversation title generation requires a completed run.")
        if not isinstance(self.planner, TitleGenerator):
            raise PlanningError("Conversation title generation requires the LLM planner.")
        runtime.services.publish = RunEventPublisher(runtime)
        title = self.planner.generate_title(runtime, first_user_text)
        runtime.save()
        return title

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
        if runtime.services.todo_store is not None and run.turn_id:
            runtime.services.todo_store.persist_turn(runtime.state.session_id, run.turn_id)
        # The active dynamic node is the authoritative workflow mode.  A
        # running PATCH may change it between model/tool decision boundaries;
        # refresh the RunState mode before routing so prompt/tool selection
        # observes the same value that is exposed at the node top level.
        if runtime.state.running_mode in {"agent", "plan"}:
            run.mode = runtime.state.running_mode  # type: ignore[assignment]
        run.history = runtime.state.messages
        publish = runtime.services.publish
        info = RunHookInfo(runtime.state.session_id, run.run_id, run.task, run.mode)
        context = RunHookContext(info)
        try:
            before = before_run_hook_manager.execute(context, publish)
            if before.decision == "reject":
                raise HookRejected("run", before.reason or "Run rejected by hook.", before.data)
            try:
                if resumed:
                    self._resume_dispatch(runtime)
                else:
                    self._dispatch(runtime)
            except Exception as error:
                after_run_hook_manager.execute(
                    RunHookContext(
                        info,
                        HookOutcome(status="failed", error=HookErrorInfo.from_exception(error)),
                    ),
                    publish,
                )
                raise
            outcome = HookOutcome(
                status=(
                    "succeeded"
                    if run.status == "completed"
                    else run.status
                    if run.status in {"failed", "cancelled"}
                    else "failed"
                ),
                result=RunHookResult(run.status, run.final_answer),
            )
            after_run_hook_manager.execute(RunHookContext(info, outcome), publish)
        except HookRejected as exc:
            cancel_run(runtime, message=exc.reason)
        except HookExecutionError as exc:
            fail_run(
                runtime,
                safe_error_message(exc),
                hook=exc.hook,
                lifecycle=exc.lifecycle,
                phase=exc.phase,
                error_type=exc.error_type,
            )
        return self._finish(runtime, started_at=started_at)

    def _resume_dispatch(self, runtime: AgentRuntime) -> None:
        run = runtime.run
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
        self._run_selected_workflow(runtime)

    def _dispatch(self, runtime: AgentRuntime) -> None:
        runtime.apply_pending_runtime_config()
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
        self._run_selected_workflow(runtime)

    def _run_selected_workflow(self, runtime: AgentRuntime) -> None:
        """Re-dispatch a running Turn whenever its mode changes at a boundary."""

        while runtime.run.status == "running":
            runtime.apply_pending_runtime_config()
            try:
                if runtime.run.mode == "plan":
                    self._plan_mode.run(runtime)
                else:
                    self._execution.run(runtime)
            except WorkflowModeChanged:
                continue
            return

    def _default_interrupt(self, runtime: AgentRuntime):
        return default_interrupt(runtime)

    def _finish(self, runtime: AgentRuntime, *, started_at: float) -> RunState:
        return finish_run(runtime, started_at=started_at)

    def close(self) -> None:
        """Close only resources created for this runner."""

        if self._closed:
            return
        self._closed = True
        try:
            self.job_scope.close(timeout=5.0)
        finally:
            if self._owns_job_registry:
                self.job_registry.close_all(reason="runner closed", timeout=5.0)
        for resource in reversed(self._resources):
            close = getattr(resource, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> AgentRunner:
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()
