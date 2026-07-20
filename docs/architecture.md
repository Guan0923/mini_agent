# Architecture

Mini-Agent keeps provider wire formats outside its execution model. Public pipeline methods accept one `AgentRuntime`; provider adapters translate between that runtime and vendor payloads.

```text
TUI -> ConversationService -> RuntimeRunner port -> AgentRunner(runtime) -> workflows -> planner / tools
                              |                         |
                              +-> RuntimeState          +-> RuntimeEvent
                              +-> RuntimeServices
                              +-> RuntimeExchange

HTTP transport <-> provider adapter prepare_request/prepare_response <-> AgentRuntime
SQLite        <-> RuntimeState snapshots and user/assistant projections
```

## Runtime

`AgentRuntime` is session-scoped and intentionally split into three parts:

- `RuntimeState` is JSON-serializable. It owns typed messages, safe model settings, tool specifications, the active `RunState`, plan state, latest usage, pending assistant/tool progress, and completed run summaries.
- `RuntimeServices` contains process-local dependencies such as the planner, tool handlers, stores, event sinks, approval and steering callbacks, the clock, and ID generation. Secrets and callables are never serialized.
- `RuntimeServices` also owns the process-local `HookManager`. Synchronous run, model, and tool hooks are rebound after session restoration and never serialized into checkpoints.
- `RuntimeExchange` contains one transient model operation: request mode, allowed tools, request payload, raw response or SSE iterator, prepared response, and reasoning callback.

The formal execution entry points (`AgentRunner.run`, planner capabilities, workflows, `ToolStepExecutor.execute`, and provider preparation functions) take only `AgentRuntime`. A deprecated `LegacyAgentRunner` and planner capability adapters isolate pre-Runtime embedding APIs.

One session permits one active turn. Runtime snapshots are saved at stable transitions, including model responses, tool results, plan changes, and turn completion. SSE reasoning deltas are merged into one ordered durable `thinking` message; raw HTTP objects remain transient.

The runtime retains a non-blocking process-local steering callback for embedding callers. Workflows drain and merge steering only after strategy/model responses and before or after individual tool or plan steps; an operation already in progress may finish before stale work is skipped and the new `UserMessage` is checkpointed. The Textual TUI does not bind running input to this callback: it keeps messages in a process-local next-turn queue and starts one merged follow-up run after the active run finishes or is cooperatively cancelled with Esc. Approval prompts remain the exclusive terminal input state while a review is pending.

## Lifecycle Hooks

Subclass `AgentHook` and inject instances at the composition boundary:

```python
from mini_agent.runtime import AgentHook, ModelHookContext, build_application


class TemperatureHook(AgentHook):
    def before_model(self, context: ModelHookContext) -> None:
        context.request_parameters["temperature"] = 0.2


application = build_application(workspace, hooks=[TemperatureHook()])
```

Available pairs are `before_run/after_run`, `before_model/after_model`, and `before_tool/after_tool`. Before hooks run in registration order and may call `context.cancel(reason)`; after hooks run in reverse order with a success, failure, or cancellation snapshot. Model contexts permit request message, tool, and parameter replacement. Tool contexts permit argument replacement, followed by schema validation and approval of the final arguments. Hook exceptions are fail-closed and produce `hook_failed` runtime events.

## Messages

Top-level history contains `SystemMessage`, `UserMessage`, and `AssistantMessage`. Every message has `name`, `role`, and `content`. Assistant messages additionally preserve reasoning, raw provider logprobs, and zero or more nested `ToolMessage` values.

A ToolMessage owns the model's call ID, tool name, arguments, status, result or error content, and retryability. Pending tool calls live in `RuntimeState.active_message`; after every nested tool has a terminal result, the complete AssistantMessage moves into history. `ExecutionPlan` steps use the same ToolMessage type.

SQLite `session_runtime` snapshots retain resumable state, while `session_runtime_messages` keeps the immutable ordered audit stream for every completed or in-progress run. `session_messages` remains a compact user/assistant text projection used by the TUI and session listings; one run may contain multiple ordered user rows when steering is applied, but still has at most one assistant row. Usage is kept in its provider-native JSON shape and overwritten with the most recent completed turn's final model usage.

## Plan Messages and Handoffs

Plan mode is a read-only conversation workflow rather than an unconditional proposal generator. Plain assistant text completes with a `response` event. The built-in `request_user_input` control pauses for material clarification, while `request_plan_review` carries a non-empty Markdown plan into the existing `InterruptRequest(kind="plan")` review. Both controls are exposed beside read-only tools, must be called alone, and are not registered with `ToolRegistry` or listed by `/tools`. A submitted plan remains in the control ToolMessage arguments and becomes the run `final_answer` only after implementation approval, avoiding a duplicate proposal message in the same history.

Review decisions behave as follows:

- `Implement` completes the Plan run and creates a `RunHandoff(new_session=False)`. `ConversationService` starts a distinct Agent run in the current session with a new run ID, the complete Plan history, and an automatic `UserMessage(content="Implement the plan")`.
- `Implement and Clear Session` completes and persists the Plan run, then follows `RunHandoff(new_session=True)` into a newly created, active session. The isolated context contains only `AssistantMessage(final_plan)` and the automatic implementation message.
- `Cancel and Stay in plan mode` cancels the Plan run, preserves the complete Plan conversation, and leaves the TUI in Plan mode.

The handoff is sequential rather than a mode mutation inside one run: the Plan run remains an auditable producer, while the Agent run is an independently checkpointed consumer in either the existing or a fresh session. The Agent prompt explicitly declares prior Plan-mode restrictions inactive, and the normal strategy router selects the implementation strategy.

Plan questions, Plan Review, and Tool Review intentionally use separate decision vocabularies. Questions return `answer` with an answer map (an empty list explicitly skips one question) or `cancel`; Plan Review accepts only the three choices above; Tool Review remains `Continue / Cancel / Supplement`, so tool feedback behavior is unchanged.

## Project Skills

`SkillCatalog` discovers direct child manifests under `<workspace>/.mini_agent/skills`. Discovery is fail-fast and validates UTF-8, bounded size and line count, exact `name`/`description` YAML metadata, directory-name equality, duplicate names, and resolved path confinement. Optional resources remain ordinary workspace files; Skills do not register tools or expand permissions.

`SkillActivator` runs before both Plan-mode dispatch and Agent strategy routing. With an LLM planner and a non-empty catalog, it claims one model turn and invokes the `SkillSelector` capability with metadata only. The runtime unions semantic selections with installed names explicitly referenced as `$name`, resolves them in stable catalog order, snapshots full instructions/root/hash into `RunState.active_skills`, and emits `skills_selected`. Empty catalogs add no request; planners without the capability retain normal behavior unless a known Skill was explicitly requested, which fails clearly.

`LLMPlanner` appends active snapshots to each later operation's system message before context estimation. The appended policy keeps every preceding system constraint authoritative and cannot bypass tool schemas, workspace confinement, or approval. Checkpoints serialize snapshots directly, and Plan Review copies them into `RunHandoff`, so implementation uses the exact approved Skill version even across an isolated session. A later ordinary user turn starts with an empty active set and selects again.


## Provider Boundary

`providers/client.py` exposes the provider-selecting `LLMClient` facade and the schema-neutral `JsonHttpTransport`. The facade selects a wire adapter from `ModelConfig.provider`, coordinates transport, and records request diagnostics:

```python
LLMClient.run(runtime: AgentRuntime) -> PreparedResponse
DeepSeek.prepare_request(runtime: AgentRuntime) -> dict[str, object]
DeepSeek.prepare_response(runtime: AgentRuntime) -> PreparedResponse
```

`JsonHttpTransport` owns HTTP status handling, JSON decoding, SSE event decoding, redirect policy, and response cleanup. `DeepSeek` only expands active chat messages, constructs the vendor payload, validates tool-call arguments, aggregates streamed fragments, and converts the response back to provider-neutral messages. Artifact snapshots are not accepted at the provider boundary.

Tool decisions use DeepSeek native Tool Calls. Strategy selection, plan creation, evaluation, and replanning remain JSON-output operations. Another API should add its own adapter without changing domain messages or workflows.

## Responsibilities

- `domain` owns typed messages, ToolSpec, run/plan values, and compatibility serialization.
- `runtime` owns AgentRuntime, application composition, provider-neutral events, and contracts. Its root contains only lazy public exports. Implementations are grouped by responsibility: `core/` owns state, events, contracts, settings, and hooks; `application/` owns services and dependency composition; `execution/` owns runners, routing, workflows, steps, and outcomes; `conversation/` owns session orchestration, steering, references, and user questions; `planning/` owns Plan mode and Plan Review; `persistence/` owns checkpoint/artifact ports and persistent-event conversion.
- `ConversationService` and `AgentApplication` depend on the `RuntimeRunner` protocol; only the composition root selects `AgentRunner`.
- `PlanModeWorkflow` owns final proposal recording, review, and handoff so the runner only dispatches run modes and strategies.
- Lifecycle hooks use narrow provider-neutral contexts: before hooks may cancel, model hooks may replace messages/tools/request parameters, and tool hooks may replace arguments before validation and approval. After hooks receive snapshots in reverse registration order.
- `planning` converts prepared model responses into decisions and plans through runtime-only capability protocols. Model
  request lifecycle handling lives in `model_requests.py`, while structured output validation lives in `model_outputs.py`.
- `tools` owns handlers, executable JSON Schema validation, registration, workspace confinement, and confirmation metadata.
  `catalog.py` is a thin composition boundary; grouped default definitions live under `tools/default_tools/`.
- `providers` owns `LLMClient` selection, generic JSON/SSE transport, and vendor-specific request/response adapters.
- `storage` persists RuntimeState checkpoints, session snapshots, and compact conversation projections. Dormant artifact adapters remain available but are not composed into the runtime.
- `tui` handles terminal commands, approval input, RuntimeEvent presentation, and the process-local full-screen transcript only. `TerminalView` composes lifecycle and input routing, `ChoicePromptMixin` and `TranscriptRenderingMixin` own their respective state machines, `interactive_approval.py` bridges blocking runtime decisions, and `widgets/` contains reusable Textual controls. Worker threads enqueue display chunks; the Textual event loop owns all widget mutation and rendering.

## Extension Rules

Add a provider by implementing runtime-based request and response preparation in a new adapter, then register it with `LLMClient`; accept only active chat messages and reuse the generic transport rather than importing `requests` or storage in the adapter. Add a tool with an explicit ToolSpec schema and text-returning handler, then register it in a catalog. Add a workflow under `runtime/execution/` that consumes AgentRuntime and reuses ToolStepExecutor. New runtime implementation modules belong in the matching classified package instead of the runtime root. Storage adapters must persist RuntimeState without serializing RuntimeServices or RuntimeExchange. Dormant artifact adapters preserve revision immutability and path confinement for possible future use.
