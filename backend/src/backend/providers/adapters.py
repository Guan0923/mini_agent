"""Provider adapter contract independent of HTTP request orchestration."""

from __future__ import annotations

from typing import Any, Protocol

from backend.domain import ChatMessage, ToolSpec
from backend.runtime.core.context import AgentRuntime, PreparedResponse


class ProviderAdapter(Protocol):
    """Translate between the runtime exchange and one provider wire format."""

    @property
    def endpoint(self) -> str: ...

    @property
    def headers(self) -> dict[str, str]: ...

    @property
    def timeout_seconds(self) -> int: ...

    @property
    def operation(self) -> str: ...

    @property
    def context_size(self) -> int: ...

    def estimate_tokens(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        request_parameters: dict[str, Any],
    ) -> int: ...

    def prepare_request(self, runtime: AgentRuntime) -> dict[str, Any]: ...

    def prepare_response(self, runtime: AgentRuntime) -> PreparedResponse: ...
