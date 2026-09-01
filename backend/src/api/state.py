"""Shared single-user API runtime state for the local backend."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from threading import RLock

from backend.configuration import ClientPaths
from backend.domain import AssistantMessage, MessageQueueUnavailable
from backend.domain.runtime_state import (
    RuntimeState,
    message_payload,
    terminal_error_payload,
    utc_iso,
)
from backend.domain.terminal import TERMINAL_LABELS
from backend.jobs import JobRegistry
from backend.runtime.agent_thread_index import AgentThreadIndex
from backend.runtime.capability_settings import SubagentSettings
from backend.runtime.subagents import SubagentCoordinator
from backend.sandbox import BrokerConfiguration, SandboxMaintenanceGate, WindowsBrokerClient
from backend.storage.message_queue import MemoryMessageQueue, RedisMessageQueue
from backend.storage.projects import ProjectStore
from backend.storage.runtime_event_stream import MemoryRuntimeEventStream, RedisRuntimeEventStream
from backend.storage.settings import LocalSettingsStore
from backend.tools.terminal import available_terminal_executables, effective_terminal_type

from .agent_report_projection import project_frame
from .agent_thread_stream import AgentThreadEventHub

DEFAULT_DATA_ROOT = Path.home() / ".mini_agent"
INTERRUPTED_TURN_MESSAGE = "Turn interrupted because its backend process stopped."


class WebAppState:
    """Process-owned state for one local Mini-Agent installation."""

    def __init__(
        self,
        data_root: Path = DEFAULT_DATA_ROOT,
        *,
        project_picker: Callable[[], Path | None] | None = None,
        job_registry: JobRegistry | None = None,
        sandbox_broker: WindowsBrokerClient | None = None,
        message_queue: RedisMessageQueue | MemoryMessageQueue | None = None,
    ) -> None:
        root = Path(data_root)
        if root.is_symlink():
            raise ValueError("Local data root cannot be a symbolic link.")
        self.data_root = root.resolve()
        self.paths = ClientPaths(self.data_root)
        self.paths.ensure()
        self.settings = LocalSettingsStore(self.paths.state_db, self.paths.config_file)
        self.projects = ProjectStore(self.paths.projects_db)
        self.chat_workspace = self.paths.runtime_dir
        self.benchmark_root = self.data_root.parent / ".mini_agent-cache" / "benchmark"
        self.project_picker = project_picker
        self.job_registry = job_registry or JobRegistry()
        sandbox_config = self.settings.sandbox_config()
        self.sandbox_broker = sandbox_broker or WindowsBrokerClient.from_system(
            expected_proxy_port=int(sandbox_config["proxy_port"])
        )
        self.sandbox_maintenance = SandboxMaintenanceGate()
        self.sandbox_manifest_path = BrokerConfiguration.create().manifest_path
        self.system_job_scope = self.job_registry.root_scope()
        self.message_queue = message_queue or RedisMessageQueue.from_url()
        self.redis_pool = getattr(getattr(self.message_queue, "client", None), "connection_pool", None)
        redis_client = getattr(self.message_queue, "client", None)
        self.runtime_event_stream = (
            RedisRuntimeEventStream(redis_client, key_prefix=self.message_queue.key_prefix)
            if redis_client is not None
            else MemoryRuntimeEventStream()
        )
        self.mailbox = self.message_queue
        from .terminal_manager import TerminalManager

        self.terminal_manager = TerminalManager(self.message_queue)
        self.agent_thread_index = AgentThreadIndex()

        self.active_runtime_configs: dict[str, dict[str, object]] = {}
        self.active_runtime_bridges: dict[str, object] = {}
        self.active_turn_streams: dict[str, object] = {}
        self.active_turn_streams_lock = RLock()
        self.active_runtime_config_locks: dict[str, RLock] = {}
        from backend.storage.sqlite import SQLiteSessionStore

        agent_store = SQLiteSessionStore(self.paths, self.agent_thread_index)
        self.agent_thread_events = AgentThreadEventHub(
            lambda frame, current: project_frame(agent_store, frame, current)
        )
        self._reconcile_message_queue()
        self.agent_thread_index.rebuild(agent_store)
        self.subagent_coordinator = SubagentCoordinator(
            settings=SubagentSettings.from_config(self.settings.config_store.read()),
            store=agent_store,
            message_queue=self.message_queue,
            index=self.agent_thread_index,
            job_registry=self.job_registry,
            thread_events=self.agent_thread_events,
        )
        from .runtime_event_transport import RuntimeEventRelay

        self.runtime_event_relay = RuntimeEventRelay(self)
        self.runtime_event_relay.start()
        from .turn_message_worker import TurnMessageWorker

        self.turn_message_worker = TurnMessageWorker(self)
        self.turn_message_worker.start()

    @staticmethod
    def _delivery_in_node(node: RuntimeState, delivery_id: str) -> bool:
        return any(
            message.get("delivery_id") == delivery_id
            or any(item.get("delivery_id") == delivery_id for item in message.get("content", []))
            for version in node.data
            for message in version
        )

    @staticmethod
    def _fail_interrupted_node(store, node: RuntimeState) -> None:
        error = terminal_error_payload(
            "server",
            INTERRUPTED_TURN_MESSAGE,
            retryable=False,
            code="backend_process_stopped",
        )
        for version in node.data:
            for message in version:
                for item in message["content"]:
                    if item.get("status") == "running":
                        item["status"] = "failed"
            if version[-1]["role"] == "assistant":
                version[-1]["content"].append(error)
            else:
                version.append(message_payload("assistant", [error]))
        node.status = "failed"
        node.timestamp = utc_iso()
        store.finalize_node(RuntimeState.from_dict(node.to_dict()))

    @staticmethod
    def _fail_interrupted_run(store, session_id: str) -> None:
        run_id = store.running_run_id(session_id)
        if run_id is None:
            return
        runtime = store.load_runtime(session_id)
        if runtime is not None and runtime.current_run is not None and runtime.current_run.run_id == run_id:
            run = runtime.current_run
            run.status = "failed"
            assistant = next(
                (
                    message
                    for message in reversed(runtime.messages[run.turn_start_index :])
                    if isinstance(message, AssistantMessage)
                ),
                None,
            )
            if assistant is None:
                runtime.messages.append(AssistantMessage(content=INTERRUPTED_TURN_MESSAGE))
            elif INTERRUPTED_TURN_MESSAGE not in (assistant.content or ""):
                assistant.content = f"{assistant.content}\n\n{INTERRUPTED_TURN_MESSAGE}".strip()
            run.history = runtime.messages
            run.final_answer = INTERRUPTED_TURN_MESSAGE
            runtime.status = "idle"
            runtime.active_message = None
            runtime.active_tool_index = None
            runtime.usage = runtime.turn_usage
            runtime.turn_usage = None
            store.save_runtime(runtime)
        store.finish_turn(session_id, run_id, "failed", INTERRUPTED_TURN_MESSAGE)

    @staticmethod
    def _append_delivery_to_node(store, node: RuntimeState, envelope) -> RuntimeState:
        item: dict[str, object] = {
            "type": "text",
            "text": envelope.content,
            "status": "success",
        }
        if envelope.references:
            item["references"] = [dict(reference) for reference in envelope.references]
        message = message_payload("user", [item], delivery_id=envelope.delivery_id)
        repaired = RuntimeState.from_dict(node.to_dict())
        repaired.data[repaired.current_data_idx].append(message)
        repaired.timestamp = utc_iso()
        repaired = RuntimeState.from_dict(repaired.to_dict())
        store.update_node(repaired)
        return repaired

    def _reconcile_message_queue(self) -> None:
        from backend.storage.sqlite import SQLiteSessionStore

        store = SQLiteSessionStore(self.paths, self.agent_thread_index)
        running_nodes: list[RuntimeState] = []
        for summary in store.list_sessions(state="all"):
            for node in store.load_nodes(summary.session_id):
                if isinstance(node, RuntimeState) and node.status == "running":
                    running_nodes.append(node)
        try:
            pending = self.message_queue.pending_deliveries()
        except MessageQueueUnavailable:
            pending = []
        released_turns: set[str] = set()
        for claimed in pending:
            envelope = claimed.envelope
            if envelope.target_kind in {"thread", "report", "turn_start"}:
                continue
            node = store.find_node(envelope.target_id)
            sqlite_persisted = store.has_turn_delivery(envelope.session_id, envelope.delivery_id)
            canonical_persisted = isinstance(node, RuntimeState) and self._delivery_in_node(node, envelope.delivery_id)
            if (
                sqlite_persisted
                and isinstance(node, RuntimeState)
                and not canonical_persisted
                and node.status == "running"
            ):
                node = self._append_delivery_to_node(store, node, envelope)
                canonical_persisted = True
            if (
                canonical_persisted
                and not sqlite_persisted
                and isinstance(node, RuntimeState)
                and node.status == "running"
            ):
                run_id = store.running_run_id(envelope.session_id)
                if run_id is not None:
                    store.append_turn_input(
                        envelope.session_id,
                        run_id,
                        envelope.content,
                        delivery_id=envelope.delivery_id,
                    )
                    sqlite_persisted = True
            if sqlite_persisted and canonical_persisted:
                try:
                    self.message_queue.ack(claimed)
                except MessageQueueUnavailable:
                    pass
                continue
            if isinstance(node, RuntimeState) and node.status == "paused":
                continue
            released_turns.add(envelope.target_id)

        for session_id in {node.session_id for node in running_nodes}:
            self._fail_interrupted_run(store, session_id)
        for node in running_nodes:
            current = store.find_node(node.id)
            runtime_thread = store.get_runtime_thread(node.session_id, node.thread_id)
            if (
                isinstance(current, RuntimeState)
                and current.status == "running"
                and (runtime_thread is None or runtime_thread.origin_kind != "subagent")
            ):
                self._fail_interrupted_node(store, current)

        for turn_id in released_turns:
            try:
                self.message_queue.release_turn(turn_id)
            except MessageQueueUnavailable:
                break

    def session_workspace(self, session_id: str) -> Path:
        """Resolve the effective cwd for a session and validate project access."""

        bound = self.projects.session_project(session_id)
        if bound is None:
            self.paths.ensure_session(session_id)
            return self.paths.session_workspace(session_id)
        if bound.removed_at is not None:
            raise RuntimeError("项目已移除，请从回收站恢复后再运行。")
        path = Path(bound.cwd)
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_dir():
                raise RuntimeError("项目 cwd 不可访问，请恢复文件夹后重试。")
            return resolved
        except (OSError, RuntimeError) as exc:
            raise RuntimeError("项目 cwd 不可访问，请恢复文件夹后重试。") from exc

    def copy_session_files(self, source_session_id: str, target_session_id: str) -> None:
        from .user_data import copy_session_files

        copy_session_files(self.paths, source_session_id, target_session_id)

    def copy_session_uploads(self, source_session_id: str, target_session_id: str) -> None:
        from .user_data import copy_session_uploads

        copy_session_uploads(self.paths, source_session_id, target_session_id)

    def benchmark_data_root(self) -> Path:
        cache_root = self.benchmark_root.parent
        if cache_root.is_symlink() or (cache_root.exists() and not cache_root.is_dir()):
            raise ValueError("Benchmark cache root cannot be a symbolic link or regular file.")
        if self.benchmark_root.is_symlink() or (self.benchmark_root.exists() and not self.benchmark_root.is_dir()):
            raise ValueError("Benchmark directory must be a regular directory.")
        self.benchmark_root.mkdir(parents=True, exist_ok=True)
        return self.benchmark_root

    def settings_payload(self) -> dict[str, object]:
        result = self.settings.settings()
        if os.name == "nt":
            available = available_terminal_executables(is_windows=True)
            runtime = result.get("runtime_config")
            current = dict(runtime) if isinstance(runtime, dict) else {}
            requested = current.get("terminal_type", "cmd")
            effective = effective_terminal_type(requested, is_windows=True)
            notice: str | None = None
            if not available:
                notice = "未检测到当前系统可用的终端。"
            elif effective != requested:
                notice = "已保存的终端当前不可用，本次已回退到可用终端。"
            current["terminal_type"] = effective
            result["runtime_config"] = current
            result["terminal_options"] = [{"value": name, "label": TERMINAL_LABELS[name]} for name in available]
            result["terminal_notice"] = notice
        else:
            result["terminal_options"] = []
            result["terminal_notice"] = None
        return result

    def model_config(self, provider_name: str | None = None):
        return self.settings.model_config(provider_name)

    def agent_config(self) -> dict[str, object]:
        return self.settings.agent_config()

    def agent_preferences(self) -> str:
        return self.settings.agent_preferences()

    def runtime_config(self) -> dict[str, object]:
        return {"runtime": self.settings.runtime_config()}

    def close(self) -> None:
        self.turn_message_worker.close()
        self.runtime_event_relay.close()
        self.subagent_coordinator.close()
        self.job_registry.close_all(reason="web application closed", timeout=5.0)
        self.agent_thread_events.close()
        self.terminal_manager.close_all()
        closed: set[int] = set()
        for resource in (self.settings, self.projects, self.message_queue):
            if id(resource) in closed:
                continue
            closed.add(id(resource))
            close = getattr(resource, "close", None)
            if callable(close):
                close()


__all__ = ["DEFAULT_DATA_ROOT", "WebAppState"]
