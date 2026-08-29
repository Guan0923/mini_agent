"""Runtime service dependencies and mutable AgentRuntime facade."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from backend.domain import (
    AssistantMessage,
    ChatMessage,
    RunState,
    ToolSpec,
    UserMessage,
)
from backend.domain.runtime_state import RuntimeState as RuntimeTreeNode
from backend.domain.state import utc_now

from ..config import RunnerSettings
from ..contracts import CancellationHandler, Confirm, EventHandler, InterruptHandler, SteeringHandler, SuspensionHandler
from ..ports import RuntimeStore
from .exchange import RuntimeExchange, _chat_messages_from_nodes, new_exchange_id, new_tool_call_id
from .state import RuntimeState

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
    # The NodeBridge ignores pre-decision Items until the immutable Turn Trace
    # context and initial User Item have been committed together.
    turn_trace_initialized: bool = False
    # Canonical message-tree context provider.  It is deliberately a callback
    # rather than a persisted field: the bridge owns the dynamic sidecar and
    # can replace a failed placeholder immediately before every model request.
    runtime_node_context: Callable[[], Sequence[RuntimeTreeNode]] | None = None
    context_prefix_messages: list[ChatMessage] = field(default_factory=list)
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
            messages = [*self.services.context_prefix_messages, *_chat_messages_from_nodes(nodes)]
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
