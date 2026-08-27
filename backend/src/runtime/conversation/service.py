"""Application service for one persistent AgentRuntime conversation."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from backend.domain import (
    FAILED_TERMINAL_MESSAGE,
    AssistantMessage,
    ResumePreview,
    RunMode,
    RunProvenance,
    RunState,
    RuntimeStateNode,
    RunTrigger,
    Session,
    SkillSnapshot,
    UserMessage,
    new_run_id,
    new_session_id,
)
from backend.tools import ToolError

from ..core.context import text_messages
from ..core.contracts import CancellationHandler, EventHandler, InterruptHandler, SteeringHandler
from ..core.events import RuntimeEvent
from ..execution import RuntimeRunner
from ..node_bridge import RuntimeEventNodeBridge
from ..persistence.recording import persistent_event
from .ports import SessionStore, TaskPreprocessor
from .recovery.resuming import prepare_resume
from .recovery.resuming import resume_session as resume_conversation
from .session_control import ConversationSessionController


class TaskPreparationError(ValueError):
    pass


class ConversationService(ConversationSessionController):
    def __init__(
        self,
        runner: RuntimeRunner,
        session_store: SessionStore | None = None,
        task_preprocessor: TaskPreprocessor | None = None,
        session_id: str | None = None,
        default_timezone: str = "Asia/Shanghai",
        session_provisioner: Callable[[SessionStore, str, Session], Session | None] | None = None,
        session_provisioner_cleanup: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(runner, session_store, session_id, default_timezone=default_timezone)
        self._task_preprocessor = task_preprocessor
        self._session_provisioner = session_provisioner
        self._session_provisioner_cleanup = session_provisioner_cleanup
        # Web streaming installs its bridge before invoking this service so it
        # can expose the active leaf to PATCH /runtime-config.  Local TUI and
        # embedding callers leave this unset; ``_run_single_turn`` then owns a
        # bridge and projects the same canonical node lifecycle internally.
        self.runtime_node_bridge: RuntimeEventNodeBridge | None = None
        self._node_bridge_events_external = False

    def attach_runtime_node_bridge(
        self,
        bridge: RuntimeEventNodeBridge,
        *,
        events_external: bool = True,
    ) -> None:
        """Attach a caller-owned bridge for the next execution.

        The Web SSE layer needs to register the bridge before the worker
        starts, while the local service creates one lazily.  Marking event
        ownership prevents the Web sink from receiving the same event twice.
        """

        self.runtime_node_bridge = bridge
        self._node_bridge_events_external = events_external

    def compact_context(
        self,
        *,
        source_node_id: str | None = None,
        compact_turn_id: str | None = None,
    ):
        """Compact an idle conversation through the canonical message-tree bridge."""

        if self.runtime is None:
            return super().compact_context()
        bridge = self.runtime_node_bridge
        if bridge is None or bridge.closed:
            bridge = self._node_bridge_for_runtime(
                "",
                source_node_id=source_node_id,
                compaction_turn_id=compact_turn_id,
            )
            self.runtime_node_bridge = bridge
            self._node_bridge_events_external = False
        previous_on_event = self.runtime.services.on_event
        if bridge is not None:
            bridge.bind_runtime(self.runtime)
            bridge.start_for_compaction()

            def sink(event):
                bridge.handle(event)
                if previous_on_event is not None:
                    previous_on_event(event)

            self.runtime.services.on_event = sink
        canonical_context = bool(self.runtime.model_nodes())
        try:
            result = super().compact_context()
            if result.compacted and bridge is not None and bridge.finish("success") is None:
                raise RuntimeError("Compaction Turn could not be finalized.")
        finally:
            self.runtime.services.on_event = previous_on_event
            if bridge is not None:
                bridge.closed = True
            self.runtime_node_bridge = None
            self._node_bridge_events_external = False
        if canonical_context and result.compacted:
            # Refresh the in-process planner state from the durable Turn tree.
            projected = self.runtime.model_messages()
            self.runtime.state.messages = projected
            if self.runtime.state.current_run is not None:
                self.runtime.state.current_run.history = self.runtime.state.messages
                self.runtime.state.current_run.turn_start_index = min(1, len(projected))
            self.runtime.save()
        self.conversation = text_messages(self.runtime.state.messages)
        return result

    def compact_turn(self, source_node_id: str, compact_turn_id: str) -> RuntimeStateNode:
        """Create one finalized Compaction Turn from an exact source Turn."""

        result = self.compact_context(
            source_node_id=source_node_id,
            compact_turn_id=compact_turn_id,
        )
        if not result.compacted or self.session_store is None or self.active_session is None:
            raise RuntimeError("Conversation context did not produce a Compaction Turn.")
        getter = getattr(self.session_store, "get_node", None)
        if not callable(getter):
            raise RuntimeError("The Turn store cannot load the completed Compaction Turn.")
        compacted = getter(self.active_session.session_id, compact_turn_id)
        if not isinstance(compacted, RuntimeStateNode) or compacted.status != "success":
            raise RuntimeError("The completed Compaction Turn is unavailable.")
        return compacted

    def generate_title(self, first_user_text: str) -> str:
        """Generate a title with the active conversation's completed runtime."""

        if self.runtime is None:
            raise RuntimeError("Conversation runtime is unavailable for title generation.")
        return self.runner.generate_title(self.runtime, first_user_text)

    def _node_bridge_for_runtime(
        self,
        prompt: str,
        references: list[Mapping[str, str]] | None = None,
        *,
        source_node_id: str | None = None,
        compaction_turn_id: str | None = None,
    ) -> RuntimeEventNodeBridge | None:
        """Create a local bridge from the latest durable node configuration."""

        if self.session_store is None or not callable(getattr(self.session_store, "create_node", None)):
            return None
        session = self.active_session
        if session is None or self.runtime is None:
            return None
        store = self.session_store
        # Prefer the latest durable leaf's top-level runtime settings.  This
        # preserves a provider/model/permission change across turns even when
        # the legacy RuntimeState checkpoint still has older compatibility
        # fields.  A provider client supplies defaults for an empty session.
        latest = None
        if source_node_id:
            getter = getattr(store, "get_node", None)
            latest = getter(session.session_id, source_node_id) if callable(getter) else None
            if latest is None:
                raise ValueError("Unknown source Turn.")
            if not isinstance(latest, RuntimeStateNode):
                raise ValueError("A root Turn is only an ancestry anchor.")
        loader = getattr(store, "load_nodes", None)
        if latest is None and callable(loader):
            nodes = [node for node in loader(session.session_id) if isinstance(node, RuntimeStateNode)]
            if nodes:
                parent_keys = {(node.parent_session_id, node.parent_id) for node in nodes if node.parent_id}
                leaves = [node for node in nodes if (node.session_id, node.id) not in parent_keys]
                if leaves:
                    latest = max(leaves, key=lambda node: (node.timestamp, node.id))

        client = getattr(getattr(self.runtime.services, "planner", None), "client", None)
        config = getattr(client, "config", None)
        provider_name = str(
            (latest.provider_name if latest is not None else "")
            or getattr(self.runtime.state, "provider_name", "")
            or getattr(config, "provider_name", None)
            or getattr(config, "provider", None)
            or "unknown"
        )
        model_config = dict(latest.model) if latest is not None else dict(self.runtime.state.model_snapshot or {})
        model_config.setdefault(
            "current_model", getattr(config, "model", None) or self.runtime.state.model or "unknown"
        )
        model_config.setdefault("context_length", getattr(config, "context_size", 128000))
        model_config.setdefault("output_length", getattr(config, "max_tokens", 8192))
        model_config.setdefault("reasoning_effort", "medium")
        model_config.setdefault("thinking", "enable")
        model_config.setdefault("temperature", 1.0)
        permission_mode = latest.permission_mode if latest is not None else self.runtime.state.permission_mode
        running_mode = latest.running_mode if latest is not None else self.runtime.state.running_mode
        return RuntimeEventNodeBridge(
            store,
            session_id=session.session_id,
            thread_id=latest.thread_id if latest is not None else session.session_id,
            source_node_id=latest.id if source_node_id and latest is not None else None,
            compaction_turn_id=compaction_turn_id,
            prompt=prompt,
            user=str(getattr(self.runtime.state, "user", "") or ""),
            provider_name=provider_name,
            model=str(model_config.get("current_model") or "unknown"),
            model_config=model_config,
            permission_mode=permission_mode,
            running_mode=running_mode,
            cwd=str(getattr(self.runtime.state, "workspace_root", "") or ""),
            references=references,
            emit=lambda _frame: None,
        )

    def _bind_node_bridge(
        self,
        prompt: str,
        on_event: EventHandler | None,
        references: list[Mapping[str, str]] | None = None,
        *,
        running_mode: RunMode | None = None,
    ) -> None:
        """Bind a bridge to the runtime and compose its local event sink."""

        bridge = self.runtime_node_bridge
        if bridge is None or bridge.closed:
            bridge = self._node_bridge_for_runtime(prompt, references)
            self.runtime_node_bridge = bridge
            self._node_bridge_events_external = False
        if bridge is None or self.runtime is None:
            return
        if running_mode in {"agent", "plan"}:
            bridge.running_mode = running_mode
        bridge.bind_runtime(self.runtime)
        if not bridge.started:
            bridge.start()
        if self._node_bridge_events_external:
            # The caller (Web SSE) already invokes bridge.handle from its
            # transport sink and owns frame publication/active registration.
            return
        previous = on_event

        def sink(event: RuntimeEvent) -> None:
            bridge.handle(event)
            if previous is not None:
                previous(event)

        self.runtime.services.on_event = sink

    def run_task(
        self,
        task: str,
        *,
        mode: RunMode,
        on_event: EventHandler | None = None,
        interrupt: InterruptHandler | None = None,
        steering: SteeringHandler | None = None,
        cancel_requested: CancellationHandler | None = None,
        suspend_requested: CancellationHandler | None = None,
        trigger: RunTrigger = "embedding",
        request_parameters: Mapping[str, Any] | None = None,
        references: Sequence[Mapping[str, str]] = (),
    ) -> RunState:
        prepared = self._prepare(task, structured=bool(references))
        state = self._run_single_turn(
            prepared,
            mode=mode,
            on_event=on_event,
            interrupt=interrupt,
            steering=steering,
            cancel_requested=cancel_requested,
            suspend_requested=suspend_requested,
            trigger=trigger,
            request_parameters=request_parameters,
            references=list(references),
        )
        handoff = state.handoff
        if handoff is None:
            return state
        source_session_id = (
            self.active_session.session_id if self.active_session is not None else self.runtime.state.session_id
        )
        bridge = self.runtime_node_bridge
        if handoff.compact_before:
            try:
                self.runner.compact_context(self.runtime)
                if bridge is not None:
                    bridge.finalize_current("success")
                if self.runtime.model_nodes():
                    self.runtime.state.messages = self.runtime.model_messages()
                    self.runtime.save()
            except Exception as exc:
                safe_detail, _ = persistent_event(RuntimeEvent("error", str(exc)), True)
                safe_message = f"Context compaction failed: {safe_detail or exc.__class__.__name__}"
                self.runtime.state.running_mode = "plan"
                state.mode = "plan"
                if bridge is not None and not bridge.closed:
                    bridge.record_compaction_failure(safe_message)
                    bridge.closed = True
                return state
        if bridge is not None and not bridge.closed:
            bridge.start_child(handoff.task, running_mode=handoff.mode)
        follow_up = self._run_single_turn(
            handoff.task,
            mode=handoff.mode,
            on_event=on_event,
            interrupt=interrupt,
            steering=steering,
            cancel_requested=cancel_requested,
            suspend_requested=suspend_requested,
            active_skills=handoff.active_skills,
            trigger="handoff",
            source_session_id=source_session_id,
            source_run_id=state.run_id,
            request_parameters=request_parameters,
        )
        if follow_up.handoff is not None:
            raise RuntimeError("Nested run handoffs are not supported.")
        return follow_up

    def _run_single_turn(
        self,
        prepared: str,
        *,
        mode: RunMode,
        on_event: EventHandler | None,
        interrupt: InterruptHandler | None,
        steering: SteeringHandler | None,
        cancel_requested: CancellationHandler | None,
        suspend_requested: CancellationHandler | None,
        active_skills: tuple[SkillSnapshot, ...] = (),
        trigger: RunTrigger = "embedding",
        source_session_id: str | None = None,
        source_run_id: str | None = None,
        request_parameters: Mapping[str, Any] | None = None,
        references: list[Mapping[str, str]] | None = None,
    ) -> RunState:
        provenance = RunProvenance(
            trigger=trigger,
            workspace_root=getattr(self.runner, "workspace_root", None),
            source_session_id=source_session_id,
            source_run_id=source_run_id,
        )
        if self.session_store is not None:
            session = self.ensure_session(prepared)
            self._ensure_runtime(session.session_id)
            assert self.runtime is not None
            if self.runtime.state.status == "running":
                raise RuntimeError("The active session already has a running turn; resume or terminate it first.")
            run_id = new_run_id()
            self.session_store.start_turn(
                session.session_id,
                run_id,
                prepared,
                provenance,
            )
        else:
            if self.runtime is None:
                self.runtime = self.runner.empty_runtime(session_id=new_session_id())
                self.runtime.state.timezone = self.default_timezone
            if self.runtime.state.status == "running":
                raise RuntimeError("The active session already has a running turn; resume or terminate it first.")
            run_id = new_run_id()
        assert self.runtime is not None
        turn_start_index = len(self.runtime.state.messages)
        self.runtime.state.messages.append(UserMessage(content=prepared))
        self.runtime.state.current_run = RunState(
            task=prepared,
            mode=mode,
            run_id=run_id,
            turn_start_index=turn_start_index,
            history=self.runtime.state.messages,
            active_skills=list(active_skills),
            provenance=provenance,
        )
        self.runtime.state.active_message = None
        self.runtime.state.active_tool_index = None
        self.runtime.state.turn_usage = None
        self.runtime.state.status = "running"
        self.runtime.services.runtime_store = self.session_store
        self.runtime.services.on_event = on_event
        self.runtime.services.interrupt = interrupt
        self.runtime.services.steering = steering
        self.runtime.services.cancel_requested = cancel_requested
        self.runtime.services.suspend_requested = suspend_requested
        if request_parameters:
            self.runtime.state.request_parameters.update(dict(request_parameters))
        runtime = self.runner.bind(self.runtime)
        # The canonical message-tree bridge is installed for local TUI and
        # embedding executions as well as Web SSE.  Web attaches a bridge
        # ahead of time so it can expose the active dynamic leaf to PATCH;
        # local callers get an equivalent bridge here.
        self._bind_node_bridge(prepared, on_event, references, running_mode=mode)
        # ``mode`` is the initial runtime configuration for this turn.  The
        # runner refreshes ``RunState.mode`` from ``state.running_mode`` at
        # dispatch time and the bridge's ``bind_runtime`` derives it from the
        # latest durable node, so an explicitly requested mode must be
        # re-applied after both: otherwise a Plan review handoff to an agent
        # run would silently inherit the previous node's ``plan`` mode.
        if mode in {"agent", "plan"}:
            self.runtime.state.running_mode = mode
        try:
            state = self.runner.run(runtime)
        except Exception as exc:
            bridge = self.runtime_node_bridge
            if bridge is not None and not self._node_bridge_events_external:
                bridge.finish_exception(exc)
            self._record_unexpected_failure(exc)
            raise
        bridge = self.runtime_node_bridge
        if bridge is not None and not self._node_bridge_events_external and state.handoff is None:
            if state.status in {"completed", "success"}:
                bridge.finish("success", state.final_answer or "")
            elif state.status == "cancelled":
                bridge.finish("paused", state.final_answer or "", category="user")
            elif bridge.abort_category is not None:
                bridge.finish(
                    "paused" if bridge.abort_category == "network" else "failed",
                    state.final_answer or "",
                    category=bridge.abort_category,
                    code=bridge.abort_code,
                )
            else:
                bridge.finish("failed", state.final_answer or "", category="agent", code="runtime_failed")
        if self.session_store is not None and self.active_session is not None:
            self.session_store.finish_turn(
                self.active_session.session_id,
                state.run_id,
                state.status,
                state.final_answer,
            )
            self._reload_active_session()
        self.conversation = text_messages(runtime.state.messages)
        # A bridge is scoped to one turn.  Keep the final durable tree in the
        # store, but do not let a closed dynamic sidecar receive a later turn's
        # configuration or events.
        if bridge is not None and bridge.closed:
            self.runtime_node_bridge = None
            self._node_bridge_events_external = False
        return state

    def prepare_resume(self, session_id: str | None = None) -> ResumePreview:
        return prepare_resume(self, session_id)

    def resume_session(
        self,
        session_id: str | None = None,
        *,
        on_event: EventHandler | None = None,
        interrupt: InterruptHandler | None = None,
        steering: SteeringHandler | None = None,
        cancel_requested: CancellationHandler | None = None,
        suspend_requested: CancellationHandler | None = None,
        request_parameters: Mapping[str, Any] | None = None,
        resume_confirmed: bool = False,
    ) -> RunState | None:
        return resume_conversation(
            self,
            session_id,
            on_event=on_event,
            interrupt=interrupt,
            steering=steering,
            cancel_requested=cancel_requested,
            suspend_requested=suspend_requested,
            request_parameters=request_parameters,
            resume_confirmed=resume_confirmed,
        )

    def _prepare(self, task: str, *, structured: bool = False) -> str:
        if self._task_preprocessor is None:
            return task
        try:
            return self._task_preprocessor.expand(task, structured=structured)
        except ToolError as exc:
            raise TaskPreparationError(str(exc)) from exc

    def _record_unexpected_failure(self, error: Exception) -> None:
        if self.runtime is None or self.runtime.state.current_run is None:
            return
        run = self.runtime.state.current_run
        run.status = "failed"
        boundary = min(max(run.turn_start_index, 0), len(self.runtime.state.messages))
        assistant = next(
            (item for item in reversed(self.runtime.state.messages[boundary:]) if isinstance(item, AssistantMessage)),
            None,
        )
        if assistant is None:
            self.runtime.state.messages.append(AssistantMessage(content=FAILED_TERMINAL_MESSAGE))
        elif FAILED_TERMINAL_MESSAGE not in (assistant.content or ""):
            assistant.content = f"{assistant.content}\n\n{FAILED_TERMINAL_MESSAGE}".strip()
        run.history = self.runtime.state.messages
        run.final_answer = FAILED_TERMINAL_MESSAGE
        run.add_event("error", FAILED_TERMINAL_MESSAGE, error_type=error.__class__.__name__)
        self.runtime.state.status = "idle"
        self.runtime.state.usage = self.runtime.state.turn_usage
        self.runtime.state.turn_usage = None
        publish = self.runtime.services.publish
        if publish is not None:
            publish(RuntimeEvent("thinking_end", data={"interrupted": True}))
            publish(
                RuntimeEvent(
                    "error",
                    FAILED_TERMINAL_MESSAGE,
                    {"error_type": error.__class__.__name__, "unexpected": True},
                )
            )
            run.add_event("run_finished", "Run finished", status=run.status)
            publish(RuntimeEvent("run_finished", run.status, {"final_answer": run.final_answer}))
        self.runtime.save()
        if self.session_store is not None and self.active_session is not None:
            self.session_store.finish_turn(
                self.active_session.session_id,
                run.run_id,
                run.status,
                run.final_answer,
            )
