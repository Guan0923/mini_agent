# Architecture

Mini-Agent keeps provider wire formats outside its execution model. Public pipeline methods accept one `AgentRuntime`; provider adapters translate between that runtime and vendor payloads.

## Four-layer layout (deployment boundaries)

The deployable system has four responsibilities. The browser and terminal are clients; the backend is a loopback process on the user's computer; only the cloud process can reach PostgreSQL.

```text
frontend/ ──HTTP/SSE──> local backend (127.0.0.1:8000)
tui/      ──future unified protocol──┘
                              │ HTTPS (optional)
                              ▼
                       cloud API (8100) ──> PostgreSQL
```

- `frontend/` is a browser client. It never imports Python and only calls the local backend contract. Vite proxies `/api` and `/benchmark` to port 8000 during development; a production backend may serve `frontend/dist`.
- `backend/` owns the Agent Runtime, model/provider calls, tools, workspace, per-session SQLite, local settings, browser sessions and cloud synchronization jobs. It binds to loopback and has no PostgreSQL, SMTP or account-password authority.
- `tui/` remains an independent terminal client for this change. Its Web protocol compatibility is intentionally unchanged.
- `cloud/` is an independent FastAPI service for accounts, mail verification, device grants, bearer tokens, key envelopes and encrypted snapshot metadata/chunks. It owns `DATABASE_URL`, SMTP and the cloud master key; it must not import Agent Runtime modules.
- `benchmarks/`, `docs/` and `scripts/` are support directories, not deployment layers.

## Runtime

```text
TUI -> ConversationService -> RuntimeRunner port -> AgentRunner(runtime) -> workflows -> planner / tools
                              |                         |             |
                              +-> RuntimeState          +-> RuntimeEvent / Skills / Subagents
                              +-> RuntimeServices
                              +-> RuntimeExchange

MCP stdio      -> McpToolAdapter -> approval-free ToolRegistry subset
HTTP transport <-> provider adapter prepare_request/prepare_response <-> AgentRuntime
Per-session SQLite <-> RuntimeState, messages, audit events, checkpoints, and sync outbox
Local backend `backend.cloud.CloudClient` <-> versioned cloud HTTPS API <-> PostgreSQL owner/revision metadata and ciphertext snapshots
```

## Runtime

`AgentRuntime` is session-scoped and intentionally split into three parts:

- `RuntimeState` is JSON-serializable. It owns typed messages, safe model settings, tool specifications, the active `RunState`, latest usage, pending assistant/tool progress, and completed run summaries.
- `RuntimeServices` contains process-local dependencies such as the planner, tool handlers, stores, event sinks, approval and steering callbacks, Subagent coordinator, the clock, and ID generation. Secrets and callables are never serialized.
- `RuntimeServices` also owns the process-local `HookManager`. Synchronous run, model, and tool hooks are rebound after session restoration and never serialized into checkpoints.
- `RuntimeExchange` contains one transient model operation: request mode, allowed tools, request payload, raw response or SSE iterator, prepared response, and reasoning callback.

The formal execution entry points (`AgentRunner.run`, planner capabilities, workflows, `ToolStepExecutor.execute`, and provider preparation functions) take only `AgentRuntime`. A deprecated `LegacyAgentRunner` and planner capability adapters isolate pre-Runtime embedding APIs.

One session permits one active turn. Runtime snapshots are saved at stable transitions, including model responses, tool results, and turn completion. SSE reasoning deltas are merged into one ordered durable `thinking` message; raw HTTP objects remain transient.

The runtime retains a non-blocking process-local steering callback for embedding callers. Workflows drain and merge steering only after model responses and before or after individual tool steps; an operation already in progress may finish before stale work is skipped and the new `UserMessage` is checkpointed. The Textual TUI does not bind running input to this callback: it keeps messages in a process-local next-turn queue and starts one merged follow-up run after the active run finishes or is cooperatively cancelled with Esc. Approval prompts remain the exclusive terminal input state while a review is pending.

## Lifecycle Hooks

Subclass `AgentHook` and inject instances at the composition boundary:

```python
from backend.runtime import AgentHook, ModelHookContext, build_application


class TemperatureHook(AgentHook):
    def before_model(self, context: ModelHookContext) -> None:
        context.request_parameters["temperature"] = 0.2


application = build_application(workspace, hooks=[TemperatureHook()])
```

Available pairs are `before_run/after_run`, `before_model/after_model`, and `before_tool/after_tool`. Before hooks run in registration order and may call `context.cancel(reason)`; after hooks run in reverse order with a success, failure, or cancellation snapshot. Model contexts permit request message, tool, and parameter replacement. Tool contexts permit argument replacement, followed by schema validation and approval of the final arguments. Hook exceptions are fail-closed and produce `hook_failed` runtime events.

## Messages

Top-level history contains `SystemMessage`, `UserMessage`, and `AssistantMessage`. Every message has `name`, `role`, and `content`. Assistant messages additionally preserve reasoning, raw provider logprobs, and zero or more nested `ToolMessage` values.

A ToolMessage owns the model's call ID, tool name, arguments, status, result or error content, and retryability. Pending tool calls live in `RuntimeState.active_message`; after every nested tool has a terminal result, the complete AssistantMessage moves into history.

Each account or guest `~/.mini_agent/<user_id>/runtime/<session_id>/state.db` retains resumable runtime state, ordered runtime messages, checkpoints, and compact conversation projections. The same session directory owns an independent `workspace/` and `uploads/`; Web and network TUI do not receive separate directory layers. Usage is kept in its provider-native JSON shape and overwritten with the most recent completed turn's final model usage.

## Plan Messages and Handoffs

Plan mode is a read-only conversation workflow rather than an unconditional proposal generator. Plain assistant text completes with a `response` event. The built-in `request_user_input` control pauses for material clarification, while `request_plan_review` carries a non-empty Markdown plan into the existing `InterruptRequest(kind="plan")` review. Both controls are exposed beside read-only tools, must be called alone, and are not registered with `ToolRegistry` or listed by `/tools`. A submitted plan remains in the control ToolMessage arguments and becomes the run `final_answer` only after implementation approval, avoiding a duplicate proposal message in the same history.

Review decisions behave as follows:

- `Implement` completes the Plan run and creates a `RunHandoff(new_session=False)`. `ConversationService` starts a distinct Agent run in the current session with a new run ID, the complete Plan history, and an automatic `UserMessage(content="Implement the plan")`.
- `Implement and Clear Session` completes and persists the Plan run, then follows `RunHandoff(new_session=True)` into a newly created, active session. The isolated context contains only `AssistantMessage(final_plan)` and the automatic implementation message.
- `Cancel and Stay in plan mode` cancels the Plan run, preserves the complete Plan conversation, and leaves the TUI in Plan mode.

The handoff is sequential rather than a mode mutation inside one run: the Plan run remains an auditable producer, while the Agent run is an independently checkpointed consumer in either the existing or a fresh session. The Agent prompt explicitly declares prior Plan-mode restrictions inactive, and the Agent run executes through the default decision workflow.

Plan questions, Plan Review, and Tool Review intentionally use separate decision vocabularies. Questions return `answer` with an answer map (an empty list explicitly skips one question) or `cancel`; Plan Review accepts only the three choices above; Tool Review remains `Continue / Cancel / Supplement`, so tool feedback behavior is unchanged.

## Layered Skills

`SkillCatalog` merges direct child manifests from `~/.mini_agent/<user_id>/skills` and `<workspace>/.mini_agent/skills`; a project Skill fully overrides the same global name. Discovery is fail-fast and validates UTF-8, bounded size and line count, exact metadata, directory-name equality, duplicates within each layer, and resolved path confinement. Optional resources remain ordinary files; Skills do not register tools or expand permissions.

`SkillActivator` runs before both Plan-mode dispatch and the default Agent execution workflow. With an LLM planner and a non-empty catalog, it claims one model turn and invokes the `SkillSelector` capability with metadata only. The runtime unions semantic selections with installed names explicitly referenced as `$name`, resolves them in stable catalog order, snapshots full instructions/root/hash into `RunState.active_skills`, and emits `skills_selected`. Empty catalogs add no request; planners without the capability retain normal behavior unless a known Skill was explicitly requested, which fails clearly.

`LLMPlanner` appends active snapshots to each later operation's system message before context estimation. The appended policy keeps every preceding system constraint authoritative and cannot bypass tool schemas, workspace confinement, or approval. Checkpoints serialize snapshots directly, and Plan Review copies them into `RunHandoff`, so implementation uses the exact approved Skill version even across an isolated session. A later ordinary user turn starts with an empty active set and selects again.


## Subagents

`SubagentCoordinator` handles the runtime-only `delegate_tasks` and `get_subagent_results` tools. One parent call starts a thread-pool batch of independent child runs, each with a fresh session ID, standard tools, inherited cancellation, and approval requests routed through a bounded bridge that is drained by the parent invocation thread. Worker threads never mutate or publish through the parent Runtime directly. Child runners omit delegation tools, making the topology deliberately single-level.

`RunState.subagent_batches` persists task order, status, clipped answers, and errors. A completed batch can be read in pages; recovery converts a still-running batch to `indeterminate` instead of replaying it. `WorkspaceWriteLock` permits unrelated file writes concurrently, serializes equal normalized paths, and makes commands exclusive with every file mutation. This is process-local coordination, not a distributed lock.

## MCP Boundary

`backend.mcp` is a separate adapter over the shared tool registry, not a second agent runtime. The stdio server derives MCP schemas from ToolSpecs and exposes only read-only tools that do not require confirmation: `read_file`, `glob`, `grep`, and `get_current_time`. Calls retain JSON Schema validation, bounded output, and workspace confinement. Mutation, command, and network tools are rejected because stdio has no interactive approval channel.

The MCP server requires neither model credentials nor PostgreSQL. Separately, the client merges global and project `mcp.toml` server definitions, with a complete project override by name. Project MCP configuration must be trusted by workspace/config digest before any process starts. Each AgentRunner owns its own `ExternalMcpManager` and long-lived stdio sessions; imported tools are approval-gated mutations from the planner's perspective and are excluded from Plan mode. Initialization, calls, and shutdown have finite configured timeouts. User-owned sensitive environment values are stored only as `env://` or `keyring://` references and resolved at process start; plaintext secrets never enter snapshots, logs, audit records, or model context.

## Local-first persistence and synchronization

The backend composition root uses `LocalAuthStore(<data_root>/client.db)` plus `PerUserSettingsRepository(<data_root>/<user_id>/user.db)`. `client.db` contains only hashed local browser sessions, cached identity metadata and guest-import state; cloud access tokens are encrypted in the account's `user.db` with the OS credential-backed local key. Each session database is self-contained and stores an owner device ID, remote revision, read-only flag, schema version, full runtime history, and stable outbox operation IDs. Remote sessions owned by another device are imported read-only; `/fork` copies a terminal run into a new session and gives ownership to the current device.

`SyncCoordinator` has one event-driven background worker. Startup, checkpoint notification, and normal shutdown trigger push-then-pull; there is no periodic polling. Network, authentication, and server failures are categorized without logging tokens or snapshots and leave the outbox for a later lifecycle retry.

The deployable `cloud` FastAPI service is the only component that accesses PostgreSQL. It authenticates bearer tokens and derives the user from the token, serializes snapshot writes per user, enforces parent-head conflict rules, retains the latest three completed snapshots and validates ciphertext chunks. TLS terminates at the deployment layer; local clients reject non-HTTPS cloud endpoints (except loopback development hosts) and never receive database credentials. Guests never call cloud and remain fully usable offline; an account can continue locally while cloud requests are paused, with only an explicit 401 clearing its remote credential.

## Provider Boundary

`providers/client.py` exposes the provider-selecting `LLMClient` facade, while `providers/transport.py` owns the schema-neutral `JsonHttpTransport`. The facade selects a wire adapter from `ModelConfig.provider`, coordinates transport, and records request diagnostics:

```python
LLMClient.run(runtime: AgentRuntime) -> PreparedResponse
DeepSeek.prepare_request(runtime: AgentRuntime) -> dict[str, object]
DeepSeek.prepare_response(runtime: AgentRuntime) -> PreparedResponse
```

`JsonHttpTransport` owns HTTP status handling, JSON decoding, SSE event decoding, redirect policy, and response cleanup. `DeepSeek` only expands active chat messages, constructs the vendor payload, validates tool-call arguments, aggregates streamed fragments, and converts the response back to provider-neutral messages. Artifact snapshots are not accepted at the provider boundary.

Tool decisions use DeepSeek native Tool Calls. Another API should add its own adapter without changing domain messages or workflows.

## Responsibilities

- `domain` owns typed messages, ToolSpec, run values, and compatibility serialization.
- `runtime` owns AgentRuntime, application composition, provider-neutral events, contracts, recovery, and Subagent coordination. Its root contains only lazy public exports. Implementations are grouped by responsibility: `core/` owns state, events, contracts, settings, and hooks; `application/` owns services and dependency composition; `execution/` owns runners, workflows, steps, and outcomes; `conversation/` owns session orchestration, steering, references, recovery, and user questions; `planning/` owns Plan mode and Plan Review; `persistence/` owns checkpoint ports and persistent-event conversion.
- `ConversationService` and `AgentApplication` depend on the `RuntimeRunner` protocol; only the composition root selects `AgentRunner`.
- `PlanModeWorkflow` owns final proposal recording, review, and handoff so the runner only dispatches run modes and strategies.
- Lifecycle hooks use narrow provider-neutral contexts: before hooks may cancel, model hooks may replace messages/tools/request parameters, and tool hooks may replace arguments before validation and approval. After hooks receive snapshots in reverse registration order.
- `planning` converts prepared model responses into decisions and plans through runtime-only capability protocols. Model
  request lifecycle handling lives in `model_requests.py`, while structured output validation lives in `model_outputs.py`.
- `tools` owns handlers, executable JSON Schema validation, registration, workspace confinement, and confirmation metadata.
  `catalog.py` is a thin composition boundary; grouped default definitions live under `tools/default_tools/`.
- `providers` owns `LLMClient` selection, generic JSON/SSE transport, and vendor-specific request/response adapters.
- `storage.sqlite` persists local session state; `sync` owns snapshot packaging/jobs and the repository port, while `backend.cloud` owns the HTTPS adapter. PostgreSQL repositories and account authority exist only under `cloud/`.
- `mcp` adapts the approval-free read-tool subset to stdio without depending on model or persistence services.
- `tui` handles terminal commands, approval input, RuntimeEvent presentation, and the process-local full-screen transcript only. `application/` owns the CLI loop and command routing; `components/` owns completion and approvals; `screens/`, `rendering/`, `view_parts/`, and `widgets/` isolate Textual responsibilities. Worker threads enqueue display chunks; the Textual event loop owns all widget mutation and rendering.

## Extension Rules

Add a provider by implementing runtime-based request and response preparation in a new adapter, then register it with `LLMClient`; accept only active chat messages and reuse the generic transport rather than importing `requests` or storage in the adapter. Add a tool with an explicit ToolSpec schema and text-returning handler, then register it in a catalog. Add a workflow under `runtime/execution/` that consumes AgentRuntime and reuses ToolStepExecutor. New runtime implementation modules belong in the matching classified package instead of the runtime root. Storage adapters must persist RuntimeState without serializing RuntimeServices or RuntimeExchange.
