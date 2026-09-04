"""Durable Assistant-report dispatch and runtime report consumption."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from threading import Thread
from time import monotonic
from uuid import uuid4

from backend.domain import (
    AssistantMessage,
    MessageEnvelope,
    ThreadNode,
)
from backend.domain.runtime_state import (
    NodeFrame,
    RuntimeState,
)
from backend.tools import ToolError

from ..core.context import AgentRuntime
from ..core.events import RuntimeEvent


class _SubagentReportDeliveryMixin:
    """Deliver one canonical child report to each registered recipient."""

    def _publish_turn_reports(self, node: ThreadNode, turn: RuntimeState) -> None:
        if turn.status not in {"success", "failed"}:
            return
        reply_content = self._reply_content(node, turn)

        self._reply_context.value = (node.session_id, turn.id, turn.status)
        try:
            self.reply_subagent_message(reply_content)
        finally:
            del self._reply_context.value
        self._dispatch_ready_reports(node.session_id)
        self._report_dispatch_wakeup.set()

    def reply_subagent_message(self, reply_content: str) -> None:
        """Finalize the pre-bound terminal Turn's reports without model-supplied routing."""

        context = getattr(self._reply_context, "value", None)
        if context is None:
            raise RuntimeError("reply_subagent_message has no bound terminal Agent Turn.")
        session_id, turn_id, thread_status = context
        getattr(self._store, "finalize_agent_turn_reports")(
            session_id,
            turn_id,
            thread_status=thread_status,
            reply_content=reply_content,
        )

    def _ensure_report_dispatcher(self) -> None:
        with self._state_lock:
            if self._report_dispatcher is not None and self._report_dispatcher.is_alive():
                return
            self._report_dispatch_stop.clear()
            self._report_dispatcher = Thread(
                target=self._report_dispatch_loop,
                name="agent-report-dispatcher",
                daemon=True,
            )
            self._report_dispatcher.start()

    def _report_dispatch_loop(self) -> None:
        while not self._report_dispatch_stop.is_set():
            with self._state_lock:
                sessions = tuple(key for key in self._bindings if key != "*")
            for session_id in sessions:
                try:
                    self._dispatch_ready_reports(session_id)
                except Exception:
                    continue
            self._report_dispatch_wakeup.wait(0.5)
            self._report_dispatch_wakeup.clear()

    def _dispatch_ready_reports(self, session_id: str) -> None:
        reports = getattr(self._store, "list_agent_turn_reports")(
            session_id,
            states=("waiting", "queued"),
        )
        now = monotonic()
        for report in reports:
            if report.thread_status not in {"success", "failed"}:
                continue
            attempts, next_at = self._report_retry.get(report.delivery_id, (0, 0.0))
            if next_at > now:
                continue
            envelope = MessageEnvelope(
                delivery_id=report.delivery_id,
                sender_kind="agent",
                source_thread_id=report.agent_thread_id,
                target_kind="report",
                target_id=report.recipient_thread_id,
                session_id=report.session_id,
                thread_id=report.recipient_thread_id,
                payload={
                    "content": report.reply_content,
                    "report_status": report.thread_status,
                },
                source_message_ids=(report.delivery_id,),
                created_at=report.created_at,
                correlation_id=report.turn_id,
            )
            try:
                getattr(self._queue, "dispatch_report")(envelope)
                getattr(self._store, "mark_agent_turn_report_state")(
                    session_id,
                    report.delivery_id,
                    "queued",
                )
                self._report_retry.pop(report.delivery_id, None)
                self._drain_inactive_reports(session_id, report.recipient_thread_id)
            except Exception:
                delays = (0.5, 1.0, 2.0, 5.0)
                delay = delays[min(attempts, len(delays) - 1)]
                self._report_retry[report.delivery_id] = (attempts + 1, monotonic() + delay)

    def _ack_report(self, session_id: str, claimed: object) -> None:
        envelope = getattr(claimed, "envelope")
        getattr(self._store, "mark_agent_turn_report_state")(session_id, envelope.delivery_id, "delivered")
        getattr(self._queue, "ack")(claimed)

    def _drain_inactive_reports(self, session_id: str, thread_id: str) -> None:
        runtime_thread = getattr(self._store, "get_runtime_thread")(session_id, thread_id)
        if runtime_thread is None or runtime_thread.running_turn_id:
            return
        turn_id = runtime_thread.current_turn_id
        turn = getattr(self._store, "get_node")(session_id, turn_id) if turn_id else None
        target = getattr(self._store, "get_thread_node")(session_id, thread_id)
        if not isinstance(turn, RuntimeState) or target is None:
            return
        was_paused = turn.status == "paused"
        if turn.status not in {"paused", "success", "failed"}:
            return
        first_sender = ""
        while True:
            claimed = getattr(self._queue, "claim_report")(
                thread_id,
                f"agent-report-inactive-{thread_id}-{uuid4().hex}",
                recover=True,
            )
            if claimed is None:
                break
            envelope = claimed.envelope
            first_sender = first_sender or envelope.source_thread_id
            if not getattr(self._store, "has_canonical_delivery")(session_id, envelope.delivery_id):
                turn = getattr(self._store, "append_agent_report")(
                    session_id,
                    thread_id,
                    delivery_id=envelope.delivery_id,
                    reply_content=envelope.content,
                )
                if self._thread_events is not None:
                    self._thread_events.publish_frame(thread_id, NodeFrame.snapshot(turn), turn)
            self._ack_report(session_id, claimed)
        if was_paused and first_sender:
            current = getattr(self._store, "get_node")(session_id, turn.id)
            if isinstance(current, RuntimeState) and current.status == "paused":
                resumed = getattr(self._store, "resume_turn_node")(current.id)
                self._submit_turn(target, resumed, creator_thread_id=first_sender)

    def consume_runtime_reports(self, runtime: AgentRuntime) -> int:
        """Drain all FIFO Assistant reports at a running Turn safe boundary."""

        if self._store is None or self._queue is None:
            return 0
        thread_id = str(runtime.run.thread_id or runtime.state.session_id)
        turn_id = str(runtime.run.turn_id or "")
        if runtime.run.status != "running" or not thread_id or not turn_id:
            return 0
        count = 0
        while True:
            claimed = getattr(self._queue, "claim_report")(
                thread_id,
                f"agent-report-running-{thread_id}-{uuid4().hex}",
            )
            if claimed is None:
                break
            envelope = claimed.envelope
            if not getattr(self._store, "has_canonical_delivery")(runtime.state.session_id, envelope.delivery_id):
                publish = runtime.services.publish or (lambda _event: None)
                publish(
                    RuntimeEvent(
                        "subagent_report",
                        envelope.content,
                        {
                            "reply_content": envelope.content,
                            "delivery_id": envelope.delivery_id,
                            "report_status": envelope.payload.get("report_status"),
                        },
                    )
                )
                runtime.state.messages.append(AssistantMessage(name="subagent_report", content=envelope.content))
                runtime.run.history = runtime.state.messages
                if not getattr(self._store, "has_canonical_delivery")(
                    runtime.state.session_id,
                    envelope.delivery_id,
                ):
                    getattr(self._store, "append_agent_report")(
                        runtime.state.session_id,
                        thread_id,
                        delivery_id=envelope.delivery_id,
                        reply_content=envelope.content,
                    )
            self._ack_report(runtime.state.session_id, claimed)
            count += 1
        if count:
            runtime.save()
        return count

    def send_from_root(
        self,
        session_id: str,
        target_thread_id: str,
        content: str,
        *,
        references: list[dict[str, str]] | None = None,
        runtime_config: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        self._require_services()
        target = getattr(self._store, "get_thread_node")(session_id, target_thread_id)
        source = (
            getattr(self._store, "get_thread_node")(session_id, target.root_thread_id) if target is not None else None
        )
        if (
            source is None
            or source.depth != 0
            or source.root_thread_id != source.thread_id
            or target is None
            or target.session_id != session_id
            or target.root_thread_id != source.thread_id
            or target.depth <= 0
        ):
            raise ToolError("The target must be a Subagent Thread in this Session tree.")
        parsed_references = self._references_from_web(session_id, target, references or [])
        if not content.strip():
            raise ToolError("Agent message requires task text.")
        return self._dispatch_message(
            session_id,
            source.thread_id,
            target_thread_id,
            content,
            references=parsed_references,
            runtime_config=runtime_config,
        )

    def _references_from_web(
        self,
        session_id: str,
        target: ThreadNode,
        values: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        if not values:
            return []
        runtime_thread = getattr(self._store, "get_runtime_thread")(session_id, target.thread_id)
        turn = (
            getattr(self._store, "get_node")(session_id, runtime_thread.current_turn_id)
            if runtime_thread is not None and runtime_thread.current_turn_id
            else None
        )
        if not isinstance(turn, RuntimeState):
            raise ToolError("The target Agent has no canonical current Turn.")
        absolute: list[dict[str, str]] = []
        for value in values:
            source, raw_path, display_path = value.get("source"), value.get("path"), value.get("display_path")
            if (
                source not in {"project", "upload"}
                or not isinstance(raw_path, str)
                or not isinstance(display_path, str)
                or not display_path
            ):
                raise ToolError("Browser Agent references require source, path, and display_path.")
            if not Path(raw_path).is_absolute():
                raise ToolError("Browser Agent reference paths must be absolute.")
            absolute.append({"path": raw_path})
        parsed = self._parse_references(session_id, target, absolute)
        return [
            {
                "source": str(value["source"]),
                "path": reference["path"],
                "display_path": str(value["display_path"]),
            }
            for value, reference in zip(values, parsed, strict=True)
        ]
