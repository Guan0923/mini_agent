"""Session-scoped runtime state passed through the public agent pipeline."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

from backend.domain import (
    CHECKPOINT_PREAMBLE,
    DEFAULT_TIME_ZONE,
    AssistantMessage,
    ChatMessage,
    RunState,
    ToolMessage,
    ToolSpec,
    UserMessage,
    message_to_dict,
    normalize_provider_options,
)
from backend.domain.messages import messages_from_dicts
from backend.domain.runtime_state import RuntimeState as RuntimeTreeNode
from backend.domain.state import utc_now

from .config import RunnerSettings
from .contracts import CancellationHandler, Confirm, EventHandler, InterruptHandler, SteeringHandler, SuspensionHandler
from .ports import RuntimeStore

RuntimeOperation = Literal[
    "skill_selection",
    "decision",
    "plan",
    "summarize",
    "title",
    "finalize",
]
OutputMode = Literal["text", "json", "tools"]
RuntimeStatus = Literal["idle", "running"]


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    task: str
    status: str
    mode: str
    final_answer: str | None = None
    workflow_id: str | None = None
    attempt: int = 1


@dataclass
class RuntimeState:
    """Serializable state required to resume one complete conversation session."""

    session_id: str
    workspace_root: str | None = None
    timezone: str = DEFAULT_TIME_ZONE
    messages: list[ChatMessage] = field(default_factory=list)
    provider: str = "unknown"
    model: str = "unknown"
    provider_name: str = "unknown"
    model_snapshot: dict[str, Any] = field(default_factory=dict)
    permission_mode: str = "read_only"
    running_mode: str = "agent"
    request_parameters: dict[str, Any] = field(default_factory=dict)
    runner_settings: RunnerSettings = field(default_factory=RunnerSettings)
    tool_specs: list[ToolSpec] = field(default_factory=list)
    current_run: RunState | None = None
    run_history: list[RunSummary] = field(default_factory=list)
    active_message: AssistantMessage | None = None
    active_tool_index: int | None = None
    usage: dict[str, Any] | None = None
    turn_usage: dict[str, Any] | None = None
    token_usage: dict[str, Any] = field(default_factory=dict)
    status: RuntimeStatus = "idle"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self, *, include_runtime_messages: bool = True) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "workspace_root": self.workspace_root,
            "timezone": self.timezone,
            "messages": [message_to_dict(message) for message in self.messages],
            "provider": self.provider,
            "model": self.model,
            "provider_name": self.provider_name,
            "model_snapshot": self.model_snapshot,
            "permission_mode": self.permission_mode,
            "running_mode": self.running_mode,
            "request_parameters": self.request_parameters,
            "runner_settings": {
                "max_transport_retries": self.runner_settings.max_transport_retries,
                "max_tool_calls": self.runner_settings.max_tool_calls,
                "log_full_messages": self.runner_settings.log_full_messages,
            },
            "tool_specs": [
                {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                    "provider_options": spec.provider_options,
                }
                for spec in self.tool_specs
            ],
            "current_run": (
                self.current_run.to_dict(include_runtime_messages=include_runtime_messages)
                if self.current_run
                else None
            ),
            "run_history": [summary.__dict__ for summary in self.run_history],
            "active_message": message_to_dict(self.active_message) if self.active_message else None,
            "active_tool_index": self.active_tool_index,
            "usage": self.usage,
            "turn_usage": self.turn_usage,
            "token_usage": self.token_usage,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RuntimeState:
        raw_settings = data.get("runner_settings") or {}
        legacy_tool_calls = raw_settings.get("max_actions") if isinstance(raw_settings, dict) else None
        settings = {
            key: raw_settings[key]
            for key in ("max_transport_retries", "max_tool_calls", "log_full_messages")
            if isinstance(raw_settings, dict) and key in raw_settings
        }
        if (
            "max_tool_calls" not in settings
            and isinstance(legacy_tool_calls, int)
            and not isinstance(legacy_tool_calls, bool)
        ):
            settings["max_tool_calls"] = legacy_tool_calls
        active_data = data.get("active_message")
        active_message = None
        if isinstance(active_data, dict):
            parsed = messages_from_dicts([active_data])[0]
            if not isinstance(parsed, AssistantMessage):
                raise ValueError("Runtime active_message must be an AssistantMessage.")
            active_message = parsed
        return cls(
            session_id=str(data["session_id"]),
            workspace_root=(str(data["workspace_root"]) if data.get("workspace_root") is not None else None),
            timezone=str(data.get("timezone") or DEFAULT_TIME_ZONE),
            messages=messages_from_dicts([dict(item) for item in data.get("messages", [])]),
            provider=(
                "chat_completions"
                if str(data.get("provider") or "").casefold() == "deepseek"
                else str(data.get("provider") or "unknown")
            ),
            model=str(data.get("model") or "unknown"),
            provider_name=(
                "default"
                if str(data.get("provider_name") or data.get("provider") or "").casefold() == "deepseek"
                else str(data.get("provider_name") or data.get("provider") or "unknown")
            ),
            model_snapshot=dict(data.get("model_snapshot") or {}),
            permission_mode=str(data.get("permission_mode") or "read_only"),
            running_mode=str(data.get("running_mode") or "agent"),
            request_parameters=dict(data.get("request_parameters") or {}),
            runner_settings=RunnerSettings(**settings),
            tool_specs=[
                ToolSpec(
                    name=str(item["name"]),
                    description=str(item.get("description") or ""),
                    parameters=dict(item.get("parameters") or {}),
                    provider_options=normalize_provider_options(item.get("provider_options")),
                )
                for item in data.get("tool_specs", [])
            ],
            current_run=RunState.from_dict(data["current_run"]) if data.get("current_run") else None,
            run_history=[RunSummary(**item) for item in data.get("run_history", [])],
            active_message=active_message,
            active_tool_index=data.get("active_tool_index"),
            usage=data.get("usage") if isinstance(data.get("usage"), dict) else None,
            turn_usage=data.get("turn_usage") if isinstance(data.get("turn_usage"), dict) else None,
            token_usage=dict(data.get("token_usage") or {}),
            status="running" if data.get("status") == "running" else "idle",
            created_at=str(data.get("created_at") or utc_now()),
            updated_at=str(data.get("updated_at") or utc_now()),
        )


@dataclass
class PreparedResponse:
    message: AssistantMessage
    usage: dict[str, Any] | None = None
    response_id: str | None = None
    model: str | None = None
    finish_reason: str | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuntimeExchange:
    """Transient state for exactly one model request and response."""

    operation: RuntimeOperation | None = None
    exchange_id: str | None = None
    output_mode: OutputMode = "text"
    allowed_tools: list[ToolSpec] = field(default_factory=list)
    operation_tools: list[ToolSpec] = field(default_factory=list)
    messages: list[ChatMessage] = field(default_factory=list)
    stream: bool = False
    request: dict[str, Any] | None = None
    raw_response: dict[str, Any] | Iterable[dict[str, Any]] | None = None
    wire_request: dict[str, Any] | None = None
    wire_response: Any = None
    transport_metadata: dict[str, Any] = field(default_factory=dict)
    prepared_response: PreparedResponse | None = None
    context: dict[str, Any] = field(default_factory=dict)
    on_reasoning: Callable[[str], None] | None = None
    on_content: Callable[[str], None] | None = None
    required_tool_name: str | None = None

    def reset(self) -> None:
        self.operation = None
        self.exchange_id = None
        self.output_mode = "text"
        self.allowed_tools = []
        self.operation_tools = []
        self.messages = []
        self.stream = False
        self.request = None
        self.raw_response = None
        self.wire_request = None
        self.wire_response = None
        self.transport_metadata = {}
        self.prepared_response = None
        self.context = {}
        self.on_reasoning = None
        self.on_content = None
        self.required_tool_name = None


def new_tool_call_id() -> str:
    return f"call_{uuid4().hex}"


def new_exchange_id() -> str:
    return f"exchange_{uuid4().hex}"


def successful_items(items: Sequence[object]) -> list[Mapping[str, Any]]:
    """Return only complete canonical Items for provider context projection."""

    return [item for item in items if isinstance(item, Mapping) and item.get("status") == "success"]


def _chat_messages_from_nodes(nodes: Sequence[RuntimeTreeNode]) -> list[ChatMessage]:
    """Project each selected Turn version into the existing planner port."""

    result: list[ChatMessage] = []
    for node in nodes:
        for message in node.selected_messages:
            blocks = successful_items(message.get("content", []))
            if message.get("role") == "user":
                user_text = "".join(
                    str(item.get("text") or item.get("summary") or "")
                    for item in blocks
                    if item.get("type") in {"text", "bash", "compaction"}
                )
                references: list[str] = []
                for item in blocks:
                    for reference in item.get("references", []) if isinstance(item.get("references"), list) else []:
                        if isinstance(reference, Mapping) and reference.get("path"):
                            references.append(f"- @{reference['path']} ({reference.get('source', 'project')})")
                if references:
                    user_text = f"{user_text}\n\nFile references:\n" + "\n".join(references)
                if user_text:
                    result.append(UserMessage(content=user_text))
                continue

            summary = next((str(item.get("summary") or "") for item in blocks if item.get("type") == "compaction"), "")
            if summary:
                result.append(UserMessage(content=f"{CHECKPOINT_PREAMBLE}\n\n{summary}"))
            text_parts = [str(item.get("text") or "") for item in blocks if item.get("type") in {"text", "bash"}]
            reasoning_parts = [str(item.get("text") or "") for item in blocks if item.get("type") == "reasoning"]
            calls: dict[str, ToolMessage] = {}
            completed_call_ids = {
                str(item.get("call_id") or "")
                for item in blocks
                if item.get("type") == "tool_result" and item.get("call_id")
            }
            for item in blocks:
                kind = item.get("type")
                call_id = str(item.get("call_id") or "")
                if kind == "tool_call" and call_id in completed_call_ids:
                    calls[call_id] = ToolMessage(
                        name=str(item.get("name") or "unknown"),
                        call_id=call_id,
                        arguments=(
                            dict(item.get("arguments") or {}) if isinstance(item.get("arguments"), Mapping) else {}
                        ),
                    )
                elif kind == "tool_result" and call_id:
                    tool = calls.get(call_id)
                    if tool is None:
                        continue
                    content = item.get("content")
                    tool.content = (
                        content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
                    )
                    tool.status = "succeeded"
                    tool.retryable = item.get("retryable") if isinstance(item.get("retryable"), bool) else None
                    tool.failure_code = item.get("failure_code") if isinstance(item.get("failure_code"), str) else None
                elif kind == "error":
                    text_parts.append(str(item.get("message") or "Execution failed."))
            assistant = AssistantMessage(
                content="".join(text_parts) or None,
                reasoning="".join(reasoning_parts) or None,
                tool_messages=list(calls.values()),
            )
            if assistant.content or assistant.reasoning or assistant.tool_messages:
                result.append(assistant)
    return result


@dataclass
class RuntimeServices:
    """Non-serializable dependencies rebound when a session is opened."""

    planner: object
    tools: object
    skill_catalog: object | None = None
    skill_auto_select: bool = False
    project_skill_gate: object | None = None
    checkpoint_store: object | None = None
    runtime_store: RuntimeStore | None = None
    on_event: EventHandler | None = None
    # Hidden recoverable events still need to update the canonical node tree.
    runtime_node_event: EventHandler | None = None
    interrupt: InterruptHandler | None = None
    steering: SteeringHandler | None = None
    cancel_requested: CancellationHandler | None = None
    suspend_requested: SuspensionHandler | None = None
    register_operation_abort: Callable[[Callable[[], None]], Callable[[], None]] | None = None
    confirm: Confirm | None = None
    id_factory: Callable[[], str] = new_tool_call_id
    clock: Callable[[], str] = utc_now
    publish: EventHandler | None = None
    subagents: object | None = None
    sandbox_launcher: object | None = None
    sandbox_config: Mapping[str, Any] | None = None
    sandbox_user_id: str | None = None
    # Optional local-provider resolver. It deliberately lives on the
    # non-serializable service bundle so provider credentials never enter a
    # RuntimeState or persisted node.
    provider_config_resolver: Callable[[str], object] | None = None
    pending_runtime_config: dict[str, Any] | None = None
    # Canonical message-tree context provider.  It is deliberately a callback
    # rather than a persisted field: the bridge owns the dynamic sidecar and
    # can replace a failed placeholder immediately before every model request.
    runtime_node_context: Callable[[], Sequence[RuntimeTreeNode]] | None = None
    # In-process JobScope.  It is intentionally non-serializable and is
    # supplied by AgentRunner when a run is opened.
    job_scope: object | None = None


@dataclass
class AgentRuntime:
    """The only argument accepted by public execution-pipeline methods."""

    state: RuntimeState
    services: RuntimeServices
    exchange: RuntimeExchange = field(default_factory=RuntimeExchange)

    @property
    def run(self) -> RunState:
        if self.state.current_run is None:
            raise RuntimeError("AgentRuntime has no active run.")
        return self.state.current_run

    def touch(self) -> None:
        self.state.updated_at = self.services.clock()

    def next_tool_call_id(self) -> str:
        return self.services.id_factory()

    def next_exchange_id(self) -> str:
        return new_exchange_id()

    def stop_requested(self) -> bool:
        cancel = self.services.cancel_requested
        suspend = self.services.suspend_requested
        return bool((cancel is not None and cancel()) or (suspend is not None and suspend()))

    def model_nodes(self) -> list[RuntimeTreeNode]:
        """Return the canonical provider context when a message-tree bridge is active.

        The callback returns the *path* already resolved by the active bridge,
        including the dynamic leaf.  Keeping this method on ``AgentRuntime``
        gives planners and token estimators one stable boundary and prevents
        individual providers from accidentally reading a durable empty
        placeholder.
        """

        provider = self.services.runtime_node_context
        if not callable(provider):
            return []
        try:
            values = provider()
        except (KeyError, RuntimeError, ValueError):
            # A stream may close between a final node replacement and a late
            # UI event.  Falling back to the in-memory transcript is safer
            # than making an otherwise completed request fail during cleanup.
            return []
        return [item.clone() for item in values if isinstance(item, RuntimeTreeNode)]

    def model_messages(self, *, current_turn_only: bool = False) -> list[ChatMessage]:
        """Convert canonical nodes to provider-neutral chat messages.

        ``RuntimeState`` intentionally stores provider-neutral content blocks,
        while the existing planner/provider ports use ``ChatMessage`` objects.
        This adapter is the only conversion boundary between those contracts.
        Tool calls remain on their assistant message; independent
        ``tool_result`` nodes are merged back by ``call_id`` so all three wire
        protocols receive the same logical conversation.
        """

        nodes = self.model_nodes()
        if not nodes:
            messages = list(self.state.messages)
        else:
            messages = _chat_messages_from_nodes(nodes)
        if not current_turn_only or not nodes:
            return messages
        boundary = max((index for index, item in enumerate(messages) if isinstance(item, UserMessage)), default=0)
        return messages[boundary:]

    def request_config(self) -> dict[str, Any]:
        """Return the immutable configuration captured for this model call."""

        snapshot = self.exchange.context.get("runtime_config_snapshot")
        if isinstance(snapshot, dict):
            return copy.deepcopy(snapshot)
        return {
            "provider_name": self.state.provider_name,
            "model": self.state.model,
            "model_snapshot": copy.deepcopy(self.state.model_snapshot),
            "permission_mode": self.state.permission_mode,
            "running_mode": self.state.running_mode,
            "request_parameters": copy.deepcopy(self.state.request_parameters),
        }

    def apply_pending_runtime_config(self) -> bool:
        """Apply a UI configuration change at an execution boundary.

        The bridge publishes the node update immediately, but this method is
        called immediately before a model/tool/decision boundary.  Therefore an
        in-flight request keeps its captured parameters while the next one sees
        the new provider, model, permission and mode.
        """

        pending = self.services.pending_runtime_config
        if not isinstance(pending, dict) or not pending:
            return False
        self.services.pending_runtime_config = None
        provider_name = pending.get("provider_name")
        provider_changed = False
        if provider_name is not None:
            selected_provider = str(provider_name)
            provider_changed = selected_provider.casefold() != self.state.provider_name.casefold()
            self.state.provider_name = selected_provider
            resolver = self.services.provider_config_resolver
            if callable(resolver):
                resolved = resolver(selected_provider)
                # ``provider_name`` is a user-owned configuration identity;
                # ``provider`` remains the internal protocol/adapter kind.
                # Keep the latter out of nodes, but keep it correct for
                # diagnostics and legacy runtime consumers.
                resolved_provider = getattr(resolved, "provider", None)
                if isinstance(resolved_provider, str) and resolved_provider:
                    self.state.provider = resolved_provider
                config_model = getattr(resolved, "model", None)
                config_context = getattr(resolved, "context_size", None)
                config_output = getattr(resolved, "max_tokens", None)
                if provider_changed:
                    # A named provider is a complete model configuration.  A
                    # switch must not inherit reasoning/thinking/temperature
                    # or token limits from the previous provider; only the
                    # explicit model patch below may override these defaults.
                    self.state.model_snapshot = {
                        "reasoning_effort": "medium",
                        "current_model": config_model if isinstance(config_model, str) and config_model else "unknown",
                        "context_length": config_context
                        if isinstance(config_context, int) and config_context > 0
                        else 128000,
                        "output_length": config_output
                        if isinstance(config_output, int) and config_output > 0
                        else 8192,
                        "thinking": "enable",
                        "temperature": 1.0,
                    }
                else:
                    if isinstance(config_model, str) and config_model:
                        self.state.model_snapshot["current_model"] = config_model
                    if isinstance(config_context, int) and config_context > 0:
                        self.state.model_snapshot["context_length"] = config_context
                    if isinstance(config_output, int) and config_output > 0:
                        self.state.model_snapshot["output_length"] = config_output
            elif provider_changed:
                # Without a configured resolver there is no provider
                # record to load, but stale model fields still must not leak
                # across a named-provider switch.
                self.state.model_snapshot = {
                    "reasoning_effort": "medium",
                    "current_model": "unknown",
                    "context_length": 128000,
                    "output_length": 8192,
                    "thinking": "enable",
                    "temperature": 1.0,
                }
        model = pending.get("model")
        if isinstance(model, dict):
            self.state.model_snapshot = {**self.state.model_snapshot, **model}
            current_model = self.state.model_snapshot.get("current_model")
            if isinstance(current_model, str) and current_model:
                self.state.model = current_model
            output_length = self.state.model_snapshot.get("output_length")
            if isinstance(output_length, int) and output_length > 0:
                self.state.request_parameters["max_tokens"] = output_length
            temperature = self.state.model_snapshot.get("temperature")
            if isinstance(temperature, (int, float)) and not isinstance(temperature, bool):
                self.state.request_parameters["temperature"] = temperature
            thinking = self.state.model_snapshot.get("thinking")
            if thinking == "disable":
                self.state.request_parameters.pop("reasoning_effort", None)
                self.state.request_parameters["thinking"] = {"type": "disabled"}
            elif thinking == "enable":
                effort = self.state.model_snapshot.get("reasoning_effort")
                if effort is not None:
                    self.state.request_parameters["reasoning_effort"] = effort
                self.state.request_parameters["thinking"] = {"type": "enabled"}
        elif provider_changed:
            # Keep non-managed provider extensions intact while replacing
            # parameters controlled by the new model snapshot.
            self.state.model = str(self.state.model_snapshot.get("current_model") or "unknown")
            output_length = self.state.model_snapshot.get("output_length")
            if isinstance(output_length, int) and output_length > 0:
                self.state.request_parameters["max_tokens"] = output_length
            self.state.request_parameters["temperature"] = self.state.model_snapshot.get("temperature", 1.0)
            self.state.request_parameters["reasoning_effort"] = self.state.model_snapshot.get(
                "reasoning_effort", "medium"
            )
            self.state.request_parameters["thinking"] = {"type": "enabled"}
        permission_mode = pending.get("permission_mode")
        if permission_mode is not None:
            self.state.permission_mode = str(permission_mode)
        running_mode = pending.get("running_mode")
        if running_mode is not None:
            self.state.running_mode = str(running_mode)
            if self.state.current_run is not None:
                self.state.current_run.mode = str(running_mode)  # type: ignore[assignment]
        return True

    def save(self) -> None:
        self.touch()
        if self.services.runtime_store is not None:
            self.services.runtime_store.save_runtime(self.state)

    @classmethod
    def ephemeral(
        cls,
        *,
        session_id: str,
        planner: object,
        tools: object,
        messages: list[ChatMessage] | None = None,
        settings: RunnerSettings | None = None,
        provider: str = "unknown",
        model: str = "unknown",
        tool_specs: list[ToolSpec] | None = None,
    ) -> AgentRuntime:
        return cls(
            state=RuntimeState(
                session_id=session_id,
                messages=list(messages or []),
                runner_settings=settings or RunnerSettings(),
                provider=provider,
                model=model,
                provider_name=provider,
                model_snapshot={"current_model": model},
                tool_specs=list(tool_specs or []),
            ),
            services=RuntimeServices(planner=planner, tools=tools),
        )


def text_messages(messages: list[ChatMessage]) -> list[dict[str, str]]:
    """Return the durable user/assistant projection used by local clients."""

    projected: list[dict[str, str]] = []
    for message in messages:
        if isinstance(message, UserMessage):
            projected.append({"role": "user", "content": message.content or ""})
        elif isinstance(message, AssistantMessage) and not message.tool_messages:
            projected.append({"role": "assistant", "content": message.content or ""})
    return projected
