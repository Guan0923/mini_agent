"""Provider-neutral messages and tool specifications used by the agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

MessageRole = Literal["system", "user", "assistant", "tool"]
ToolStatus = Literal["pending", "succeeded", "failed", "indeterminate"]


@dataclass(kw_only=True)
class Message:
    """Minimum message shape shared by every internal message type."""

    name: str
    role: MessageRole
    content: str | None
    provider_options: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        """Read-only compatibility for callers migrating from message dictionaries."""

        if key not in {"name", "role", "content"}:
            raise KeyError(key)
        return getattr(self, key)


@dataclass(kw_only=True)
class SystemMessage(Message):
    name: str = "system"
    role: Literal["system"] = field(default="system", init=False)
    content: str | None = None


@dataclass(kw_only=True)
class UserMessage(Message):
    name: str = "user"
    role: Literal["user"] = field(default="user", init=False)
    content: str | None = None


@dataclass(kw_only=True)
class ToolMessage(Message):
    """One assistant-requested tool call together with its eventual result."""

    name: str
    role: Literal["tool"] = field(default="tool", init=False)
    content: str | None = None
    call_id: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    status: ToolStatus = "pending"
    retryable: bool | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ToolMessage.name must not be empty.")
        if not self.call_id:
            raise ValueError("ToolMessage.call_id must not be empty.")
        if self.status == "pending" and self.content is not None:
            raise ValueError("A pending ToolMessage cannot contain a result.")
        if self.status != "pending" and self.content is None:
            raise ValueError("A completed ToolMessage must contain a result.")


@dataclass(kw_only=True)
class AssistantMessage(Message):
    name: str = "assistant"
    role: Literal["assistant"] = field(default="assistant", init=False)
    content: str | None = None
    reasoning: str | None = None
    logprobs: dict[str, Any] | None = None
    tool_messages: list[ToolMessage] = field(default_factory=list)


ChatMessage: TypeAlias = SystemMessage | UserMessage | AssistantMessage


@dataclass(frozen=True)
class ToolSpec:
    """Provider-neutral function definition exposed to a model."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    provider_options: dict[str, dict[str, Any]] = field(default_factory=dict)


def _provider_options_from_dict(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = data.get("provider_options")
    if not isinstance(raw, dict):
        return {}
    return {str(provider): dict(options) for provider, options in raw.items() if isinstance(options, dict)}


def tool_message_to_dict(message: ToolMessage) -> dict[str, Any]:
    return {
        "name": message.name,
        "role": message.role,
        "content": message.content,
        "call_id": message.call_id,
        "arguments": message.arguments,
        "status": message.status,
        "retryable": message.retryable,
        "provider_options": message.provider_options,
    }


def tool_message_from_dict(data: dict[str, Any], *, fallback_call_id: str | None = None) -> ToolMessage:
    call_id = data.get("call_id") or data.get("tool_call_id") or fallback_call_id
    if not isinstance(call_id, str) or not call_id:
        raise ValueError("Serialized ToolMessage is missing call_id.")
    content = data.get("content")
    status = data.get("status")
    if status not in {"pending", "succeeded", "failed", "indeterminate"}:
        status = "pending" if content is None else "succeeded"
    return ToolMessage(
        name=str(data.get("name") or data.get("tool") or "unknown"),
        content=content if isinstance(content, str) or content is None else str(content),
        provider_options=_provider_options_from_dict(data),
        call_id=call_id,
        arguments=dict(data.get("arguments") or {}),
        status=status,
        retryable=data.get("retryable") if isinstance(data.get("retryable"), bool) else None,
    )


def message_to_dict(message: ChatMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": message.name,
        "role": message.role,
        "content": message.content,
        "provider_options": message.provider_options,
    }
    if isinstance(message, AssistantMessage):
        payload.update(
            reasoning=message.reasoning,
            logprobs=message.logprobs,
            tool_messages=[tool_message_to_dict(tool) for tool in message.tool_messages],
        )
    return payload


def message_from_dict(data: dict[str, Any]) -> ChatMessage:
    """Load current messages and degrade legacy top-level tool rows safely."""

    role = data.get("role")
    content = data.get("content")
    text = content if isinstance(content, str) or content is None else str(content)
    if role == "system":
        return SystemMessage(
            name=str(data.get("name") or "system"),
            content=text,
            provider_options=_provider_options_from_dict(data),
        )
    if role == "user":
        return UserMessage(
            name=str(data.get("name") or "user"),
            content=text,
            provider_options=_provider_options_from_dict(data),
        )
    if role == "assistant":
        tools = [tool_message_from_dict(item) for item in data.get("tool_messages", []) if isinstance(item, dict)]
        return AssistantMessage(
            name=str(data.get("name") or "assistant"),
            content=text,
            provider_options=_provider_options_from_dict(data),
            reasoning=data.get("reasoning") if isinstance(data.get("reasoning"), str) else None,
            logprobs=data.get("logprobs") if isinstance(data.get("logprobs"), dict) else None,
            tool_messages=tools,
        )
    if role == "artifact":
        raise ValueError("Unsupported message role: artifact")
    if role == "tool":
        # Old checkpoints stored tool results as top-level rows without call metadata.
        return AssistantMessage(
            name=str(data.get("name") or "tool"),
            content=text,
            provider_options=_provider_options_from_dict(data),
        )
    raise ValueError(f"Unknown serialized message role: {role!r}.")


def messages_from_dicts(values: list[dict[str, Any]]) -> list[ChatMessage]:
    return [message_from_dict(value) for value in values]
