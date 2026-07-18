"""Session-scoped runtime state passed through the public agent pipeline."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from uuid import uuid4

from mini_agent.domain import (
    AssistantMessage,
    ChatMessage,
    RunState,
    RuntimeMessage,
    ToolSpec,
    UserMessage,
    message_to_dict,
)
from mini_agent.domain.messages import messages_from_dicts
from mini_agent.domain.state import utc_now

from .config import RunnerSettings
from .contracts import CancellationHandler, Confirm, EventHandler, InterruptHandler, SteeringHandler
from .hooks import HookManager

RuntimeOperation = Literal["decision", "strategy", "plan", "evaluate", "replan", "summarize"]
OutputMode = Literal["text", "json", "tools"]
RuntimeStatus = Literal["idle", "running"]


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    task: str
    status: str
    mode: str
    final_answer: str | None = None


@dataclass
class RuntimeState:
    """Serializable state required to resume one complete conversation session."""

    session_id: str
    messages: list[ChatMessage] = field(default_factory=list)
    provider: str = "unknown"
    model: str = "unknown"
    request_parameters: dict[str, Any] = field(default_factory=dict)
    runner_settings: RunnerSettings = field(default_factory=RunnerSettings)
    tool_specs: list[ToolSpec] = field(default_factory=list)
    current_run: RunState | None = None
    run_history: list[RunSummary] = field(default_factory=list)
    active_message: AssistantMessage | None = None
    active_tool_index: int | None = None
    usage: dict[str, Any] | None = None
    turn_usage: dict[str, Any] | None = None
    status: RuntimeStatus = "idle"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "messages": [message_to_dict(message) for message in self.messages],
            "provider": self.provider,
            "model": self.model,
            "request_parameters": self.request_parameters,
            "runner_settings": {
                "max_retries": self.runner_settings.max_retries,
                "max_model_repairs": self.runner_settings.max_model_repairs,
                "max_transport_retries": self.runner_settings.max_transport_retries,
                "max_tool_recoveries": self.runner_settings.max_tool_recoveries,
                "max_actions": self.runner_settings.max_actions,
                "max_replans": self.runner_settings.max_replans,
                "strategy": self.runner_settings.strategy,
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
            "current_run": self.current_run.to_dict() if self.current_run else None,
            "run_history": [summary.__dict__ for summary in self.run_history],
            "active_message": message_to_dict(self.active_message) if self.active_message else None,
            "active_tool_index": self.active_tool_index,
            "usage": self.usage,
            "turn_usage": self.turn_usage,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RuntimeState:
        settings = data.get("runner_settings") or {}
        active_data = data.get("active_message")
        active_message = None
        if isinstance(active_data, dict):
            parsed = messages_from_dicts([active_data])[0]
            if not isinstance(parsed, AssistantMessage):
                raise ValueError("Runtime active_message must be an AssistantMessage.")
            active_message = parsed
        return cls(
            session_id=str(data["session_id"]),
            messages=messages_from_dicts([dict(item) for item in data.get("messages", [])]),
            provider=str(data.get("provider") or "unknown"),
            model=str(data.get("model") or "unknown"),
            request_parameters=dict(data.get("request_parameters") or {}),
            runner_settings=RunnerSettings(**settings),
            tool_specs=[
                ToolSpec(
                    name=str(item["name"]),
                    description=str(item.get("description") or ""),
                    parameters=dict(item.get("parameters") or {}),
                    provider_options={
                        str(provider): dict(options)
                        for provider, options in (item.get("provider_options") or {}).items()
                        if isinstance(options, dict)
                    },
                )
                for item in data.get("tool_specs", [])
            ],
            current_run=RunState.from_dict(data["current_run"]) if data.get("current_run") else None,
            run_history=[RunSummary(**item) for item in data.get("run_history", [])],
            active_message=active_message,
            active_tool_index=data.get("active_tool_index"),
            usage=data.get("usage") if isinstance(data.get("usage"), dict) else None,
            turn_usage=data.get("turn_usage") if isinstance(data.get("turn_usage"), dict) else None,
            status=data.get("status", "idle"),
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
    prepared_response: PreparedResponse | None = None
    context: dict[str, Any] = field(default_factory=dict)
    on_reasoning: Callable[[str], None] | None = None
    on_content: Callable[[str], None] | None = None

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
        self.prepared_response = None
        self.context = {}
        self.on_reasoning = None
        self.on_content = None


class RuntimeStore(Protocol):
    def save_runtime(self, state: RuntimeState) -> None: ...

    def append_runtime_message(self, session_id: str, run_id: str, message: RuntimeMessage) -> None: ...


def new_tool_call_id() -> str:
    return f"call_{uuid4().hex}"


def new_exchange_id() -> str:
    return f"exchange_{uuid4().hex}"


@dataclass
class RuntimeServices:
    """Non-serializable dependencies rebound when a session is opened."""

    planner: object
    tools: object
    checkpoint_store: object | None = None
    runtime_store: RuntimeStore | None = None
    on_event: EventHandler | None = None
    interrupt: InterruptHandler | None = None
    steering: SteeringHandler | None = None
    cancel_requested: CancellationHandler | None = None
    confirm: Confirm | None = None
    id_factory: Callable[[], str] = new_tool_call_id
    clock: Callable[[], str] = utc_now
    publish: EventHandler | None = None
    hooks: HookManager = field(default_factory=HookManager)


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
                tool_specs=list(tool_specs or []),
            ),
            services=RuntimeServices(planner=planner, tools=tools),
        )


def text_messages(messages: list[ChatMessage]) -> list[dict[str, str]]:
    """Return the durable user/assistant projection used by the terminal UI."""

    projected: list[dict[str, str]] = []
    for message in messages:
        if isinstance(message, UserMessage):
            projected.append({"role": "user", "content": message.content or ""})
        elif isinstance(message, AssistantMessage) and not message.tool_messages:
            projected.append({"role": "assistant", "content": message.content or ""})
    return projected
