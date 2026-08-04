"""Token usage estimation and provider reconciliation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from backend.domain import AssistantMessage
from backend.runtime.core.context import AgentRuntime
from backend.runtime.core.events import RuntimeEvent

from .adapters import ProviderAdapter


class TokenUsageTracker:
    """Maintain per-exchange and aggregate token usage on runtime state."""

    def __init__(self, adapter: ProviderAdapter) -> None:
        self.adapter = adapter

    def begin(self, runtime: AgentRuntime) -> None:
        exchange_id = runtime.exchange.exchange_id
        if not exchange_id:
            return
        estimated_input = runtime.exchange.context.get("estimated_input_tokens")
        if isinstance(estimated_input, bool) or not isinstance(estimated_input, int) or estimated_input < 0:
            return
        requests = self.requests(runtime)
        entry = requests.setdefault(exchange_id, {})
        entry.update(
            {
                "exchange_id": exchange_id,
                "estimated_input_tokens": estimated_input,
                "estimated_output_tokens": 0,
                "provider_prompt_tokens": None,
                "provider_completion_tokens": None,
                "provider_total_tokens": None,
            }
        )
        self.refresh(runtime)
        self.publish_context_usage(runtime, entry, phase="estimated")

    def track_stream_output(self, runtime: AgentRuntime, previous_reasoning, previous_content) -> None:
        if not runtime.exchange.stream:
            return
        reasoning_parts: list[str] = []
        content_parts: list[str] = []

        def refresh() -> None:
            exchange_id = runtime.exchange.exchange_id
            estimate = getattr(self.adapter, "estimate_output_tokens", None)
            if not exchange_id or not callable(estimate):
                return
            message = AssistantMessage(
                content="".join(content_parts) or None,
                reasoning="".join(reasoning_parts) or None,
            )
            entry = self.requests(runtime).get(exchange_id)
            if entry is None:
                return
            entry["estimated_output_tokens"] = estimate(message)
            self.refresh(runtime)

        def on_reasoning(chunk: str) -> None:
            reasoning_parts.append(chunk)
            refresh()
            if previous_reasoning is not None:
                previous_reasoning(chunk)

        def on_content(chunk: str) -> None:
            content_parts.append(chunk)
            refresh()
            if previous_content is not None:
                previous_content(chunk)

        runtime.exchange.on_reasoning = on_reasoning
        runtime.exchange.on_content = on_content

    def complete(
        self,
        runtime: AgentRuntime,
        usage: dict[str, Any] | None,
        message: AssistantMessage,
    ) -> None:
        exchange_id = runtime.exchange.exchange_id
        if not exchange_id:
            return
        entry = self.requests(runtime).get(exchange_id)
        if entry is None:
            return
        estimate_output = getattr(self.adapter, "estimate_output_tokens", None)
        if callable(estimate_output):
            entry["estimated_output_tokens"] = estimate_output(message)
        if isinstance(usage, Mapping):
            entry["provider_prompt_tokens"] = self.token_value(usage.get("prompt_tokens"))
            entry["provider_completion_tokens"] = self.token_value(usage.get("completion_tokens"))
            entry["provider_total_tokens"] = self.token_value(usage.get("total_tokens"))
        self.refresh(runtime)
        self.publish_context_usage(runtime, entry, phase="provider")

    def discard_unconfirmed(self, runtime: AgentRuntime) -> None:
        """Remove a provisional request when no provider usage was returned."""

        exchange_id = runtime.exchange.exchange_id
        if not exchange_id:
            return
        entry = self.requests(runtime).get(exchange_id)
        if entry is None or entry.get("provider_prompt_tokens") is not None:
            return
        del self.requests(runtime)[exchange_id]
        self.refresh(runtime)
        self.publish_context_usage(runtime, None, phase="discarded")

    @staticmethod
    def token_value(value: Any) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    @staticmethod
    def requests(runtime: AgentRuntime) -> dict[str, dict[str, Any]]:
        requests = runtime.state.token_usage.setdefault("requests", {})
        if not isinstance(requests, dict):
            requests = {}
            runtime.state.token_usage["requests"] = requests
        return requests

    @classmethod
    def refresh(cls, runtime: AgentRuntime) -> None:
        requests = cls.requests(runtime)
        totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        current_input = 0
        for entry in requests.values():
            estimated_input = cls.token_value(entry.get("estimated_input_tokens")) or 0
            estimated_output = cls.token_value(entry.get("estimated_output_tokens")) or 0
            provider_input = cls.token_value(entry.get("provider_prompt_tokens"))
            provider_output = cls.token_value(entry.get("provider_completion_tokens"))
            provider_total = cls.token_value(entry.get("provider_total_tokens"))
            input_tokens = provider_input if provider_input is not None else estimated_input
            output_tokens = provider_output if provider_output is not None else estimated_output
            total_tokens = provider_total if provider_total is not None else input_tokens + output_tokens
            entry.update(
                {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                    "input_source": "provider" if provider_input is not None else "estimated",
                    "output_source": "provider" if provider_output is not None else "estimated",
                    "total_source": "provider" if provider_total is not None else "estimated",
                }
            )
            totals["input_tokens"] += input_tokens
            totals["output_tokens"] += output_tokens
            totals["total_tokens"] += total_tokens
            current_input = input_tokens
        runtime.state.token_usage["totals"] = totals
        runtime.state.token_usage["current_input_tokens"] = current_input

    @staticmethod
    def publish_context_usage(runtime: AgentRuntime, entry: dict[str, Any] | None, *, phase: str) -> None:
        context_size = getattr(runtime.services.planner, "client", None)
        context_size = getattr(context_size, "context_size", None)
        if not isinstance(context_size, int) or context_size < 1:
            return
        totals = runtime.state.token_usage.get("totals", {})
        cumulative_input_tokens = TokenUsageTracker.token_value(totals.get("input_tokens")) or 0
        current_input_tokens = TokenUsageTracker.token_value(runtime.state.token_usage.get("current_input_tokens")) or 0
        if entry is not None:
            input_source = entry["input_source"]
            estimated_input_tokens = entry["estimated_input_tokens"]
            estimated_output_tokens = entry["estimated_output_tokens"]
            provider_prompt_tokens = entry["provider_prompt_tokens"]
            provider_completion_tokens = entry["provider_completion_tokens"]
            provider_total_tokens = entry["provider_total_tokens"]
            exchange_id = entry["exchange_id"]
        else:
            input_source = "none"
            estimated_input_tokens = None
            estimated_output_tokens = None
            provider_prompt_tokens = None
            provider_completion_tokens = None
            provider_total_tokens = None
            exchange_id = None
        (runtime.services.publish or (lambda _event: None))(
            RuntimeEvent(
                "context_usage",
                "Context usage reconciled",
                {
                    "estimated_tokens": current_input_tokens,
                    "input_tokens": current_input_tokens,
                    "current_input_tokens": current_input_tokens,
                    "cumulative_input_tokens": cumulative_input_tokens,
                    "input_source": input_source,
                    "estimated_input_tokens": estimated_input_tokens,
                    "estimated_output_tokens": estimated_output_tokens,
                    "provider_prompt_tokens": provider_prompt_tokens,
                    "provider_completion_tokens": provider_completion_tokens,
                    "provider_total_tokens": provider_total_tokens,
                    "context_size": context_size,
                    "target_ratio": 0.8,
                    "target_tokens": int(context_size * 0.8),
                    "ratio": current_input_tokens / context_size,
                    "phase": phase,
                    "exchange_id": exchange_id,
                },
            )
        )
