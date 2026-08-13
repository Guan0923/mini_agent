"""Application service for one persistent AgentRuntime conversation."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping
from typing import Any

from backend.domain import (
    FAILED_TERMINAL_MESSAGE,
    AssistantMessage,
    ResumePreview,
    RunHandoff,
    RunMode,
    RunProvenance,
    RunState,
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
    ) -> RunState:
        prepared = self._prepare(task)
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
        )
        handoff = state.handoff
        if handoff is None:
            return state
        source_session_id = (
            self.active_session.session_id if self.active_session is not None else self.runtime.state.session_id
        )
        if handoff.new_session:
            self._start_isolated_handoff_session(handoff)
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

    def _start_isolated_handoff_session(self, handoff: RunHandoff) -> None:
        if self.runtime is None:
            raise RuntimeError("Cannot start a handoff without an active runtime.")
        source_runtime = self.runtime
        proposal = (source_runtime.run.final_answer or "").strip()
        if not proposal:
            raise RuntimeError("Cannot start an isolated handoff without a completed plan proposal.")
        plan_message = AssistantMessage(content=proposal)

        if self.session_store is None:
            self.runtime = self.runner.empty_runtime(
                session_id=new_session_id(),
                messages=[plan_message],
            )
            self.runtime.state.timezone = self.default_timezone
            self.conversation = text_messages(self.runtime.state.messages)
            return

        if self.active_session is None:
            raise RuntimeError("Cannot create an isolated handoff session without an active session.")
        source_session = self.active_session
        provisioned = False
        title = f"Implement: {source_session.title}"
        if self._session_provisioner is not None:
            isolated_session = self._session_provisioner(self.session_store, title, source_session)
            provisioned = isolated_session is not None
        else:
            isolated_session = None
        if isolated_session is None:
            isolated_session = self.session_store.create_session(title)
        paths = getattr(self.session_store, "paths", None)
        try:
            isolated_runtime = self.runner.empty_runtime(
                session_id=isolated_session.session_id,
                messages=[plan_message],
                runtime_store=self.session_store,
            )
            isolated_runtime.state.timezone = self.default_timezone
            isolated_runtime.save()
            if paths is not None and not provisioned:
                paths.ensure_session(isolated_session.session_id)
                _copy_session_tree(
                    paths.session_workspace(source_session.session_id),
                    paths.session_workspace(isolated_session.session_id),
                )
                _copy_session_tree(
                    paths.session_uploads(source_session.session_id),
                    paths.session_uploads(isolated_session.session_id),
                )
        except Exception:
            if provisioned and self._session_provisioner_cleanup is not None:
                try:
                    self._session_provisioner_cleanup(isolated_session.session_id)
                except Exception:
                    pass
            if paths is not None:
                shutil.rmtree(paths.session_root(isolated_session.session_id), ignore_errors=True)
            raise

        self.active_session = isolated_session
        self.runtime = isolated_runtime
        self.conversation = text_messages(isolated_runtime.state.messages)
        self._clear_pending_session()

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
            self.session_store.start_turn(session.session_id, run_id, prepared, provenance)
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
        try:
            state = self.runner.run(runtime)
        except Exception as exc:
            self._record_unexpected_failure(exc)
            raise
        if self.session_store is not None and self.active_session is not None:
            self.session_store.finish_turn(
                self.active_session.session_id,
                state.run_id,
                state.status,
                state.final_answer,
            )
            self._reload_active_session()
        self.conversation = text_messages(runtime.state.messages)
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
        )

    def _prepare(self, task: str) -> str:
        if self._task_preprocessor is None:
            return task
        try:
            return self._task_preprocessor.expand(task)
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


def _copy_session_tree(source, target) -> None:
    """Copy a session payload without following links or special files."""

    if source.is_symlink() or not source.is_dir():
        raise ValueError("Session payload source must be a real directory.")
    if target.is_symlink():
        raise ValueError("Session payload target cannot be a symbolic link.")
    target.mkdir(parents=True, exist_ok=True)
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        destination = target / relative
        if item.is_symlink() or destination.is_symlink():
            raise ValueError("Session payload cannot contain symbolic links.")
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, destination)
        else:
            raise ValueError("Session payload cannot contain special files.")
