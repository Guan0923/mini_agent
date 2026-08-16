"""Execution-only AgentExecutor boundary for the RuntimeState tree.

``RuntimeState`` is deliberately not an execution context.  An executor owns
provider/planner/tool objects and receives a node or a path as input.  This
module provides a small concrete boundary that can be adopted incrementally by
the existing runner while keeping the serialized protocol free of callbacks,
HTTP objects and secrets.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from backend.domain.runtime_state import (
    NodeStatus,
    NodeWriter,
    RuntimeState,
    RuntimeStateTree,
    message_payload,
)


class PlannerPort(Protocol):
    def __call__(self, context: Sequence[RuntimeState], **kwargs: Any) -> Mapping[str, Any] | RuntimeState: ...


class ProviderPort(Protocol):
    def __call__(self, context: Sequence[RuntimeState], **kwargs: Any) -> Mapping[str, Any]: ...


@dataclass
class ExecutorDependencies:
    """Non-serializable execution dependencies, kept outside a node."""

    planner: PlannerPort | None = None
    provider: ProviderPort | None = None
    tools: Any = None
    hooks: Any = None
    approval: Any = None
    steering: Any = None
    provider_client: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


class AgentExecutor:
    """Run one request against a RuntimeState path using a node writer.

    The default implementation is intentionally provider-neutral.  Applications
    may supply a ``operation`` callback for their planner/provider workflow;
    that callback receives the canonical model context and returns either a
    canonical ``data`` mapping or a complete ``RuntimeState``.  The executor
    still owns the lifecycle and therefore always emits create/update/delete
    frames in the same order.
    """

    def __init__(
        self,
        writer: NodeWriter,
        *,
        dependencies: ExecutorDependencies | None = None,
        planner: PlannerPort | None = None,
        provider: ProviderPort | None = None,
        tools: Any = None,
        hooks: Any = None,
        approval: Any = None,
        steering: Any = None,
    ) -> None:
        self.writer = writer
        self.dependencies = dependencies or ExecutorDependencies(
            planner=planner,
            provider=provider,
            tools=tools,
            hooks=hooks,
            approval=approval,
            steering=steering,
        )
        if planner is not None:
            self.dependencies.planner = planner
        if provider is not None:
            self.dependencies.provider = provider
        if tools is not None:
            self.dependencies.tools = tools
        if hooks is not None:
            self.dependencies.hooks = hooks
        if approval is not None:
            self.dependencies.approval = approval
        if steering is not None:
            self.dependencies.steering = steering

    def context(
        self, state: RuntimeState | Sequence[RuntimeState], *, tree: RuntimeStateTree | None = None
    ) -> list[RuntimeState]:
        if isinstance(state, RuntimeState):
            if tree is None:
                return [state]
            return tree.model_input(state)
        return [item.clone() for item in state]

    def run(
        self,
        state: RuntimeState | Sequence[RuntimeState],
        *,
        tree: RuntimeStateTree | None = None,
        operation: Callable[..., Mapping[str, Any] | RuntimeState] | None = None,
        status_on_success: NodeStatus = "success",
        **kwargs: Any,
    ) -> RuntimeState:
        """Execute from ``state`` and persist only lifecycle frames.

        A caller normally creates the user node first and passes that leaf.  A
        returned assistant/tool message is written as a child; if the
        operation raises, its failed placeholder remains in storage and can be
        recovered without replaying a side effect.
        """

        path = [item.clone() for item in state] if not isinstance(state, RuntimeState) else None
        source = state if isinstance(state, RuntimeState) else (path[-1] if path else None)
        if source is None:
            raise ValueError("AgentExecutor.run requires a non-empty state/path.")
        context = self.context(state, tree=tree)
        callback = operation or self.dependencies.provider or self.dependencies.planner
        if callback is None:
            raise RuntimeError("AgentExecutor requires a provider, planner, or operation callback.")
        node = self.writer.create(
            session_id=source.session_id,
            parent=source,
            user=source.user,
            provider_name=source.provider_name,
            model=source.model,
            permission_mode=source.permission_mode,
            running_mode=source.running_mode,
            cwd=source.cwd,
            first_kept_entry_id=source.firstKeptEntryId,
            compaction_idx=source.compactionIdx,
        )
        try:
            result = callback(context, **kwargs)
            data = result.to_dict().get("data") if isinstance(result, RuntimeState) else dict(result)
            self.writer.update_data(node, data)
            return self.writer.delete(node.session_id, node.id, status=status_on_success)
        except Exception:
            self.writer.fail(node.session_id, node.id)
            raise

    def run_message(
        self,
        state: RuntimeState | Sequence[RuntimeState],
        role: str,
        content: Any,
        *,
        tree: RuntimeStateTree | None = None,
        status: NodeStatus = "success",
        **kwargs: Any,
    ) -> RuntimeState:
        """Convenience operation for deterministic tests and simple runtimes."""

        return self.run(
            state,
            tree=tree,
            operation=lambda _context, **_ignored: message_payload(role, content),
            status_on_success=status,
            **kwargs,
        )


__all__ = ["AgentExecutor", "ExecutorDependencies", "PlannerPort", "ProviderPort"]
