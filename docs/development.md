# Development Guide

## Environment

Mini-Agent requires Python 3.11 or newer. Create an isolated environment and install the package with its development tools:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

The editable install exposes the `mini-agent` command and keeps the `src/` package layout used by tests.

### Client configuration and optional synchronization

The client runs without PostgreSQL. Its sole runtime configuration is `~/mini_agent/config.toml`; use `config.toml.example` as a template or start once with `--planner rule` to generate the file and a stable device ID. Process environment values do not override client TOML. A legacy workspace `.env` is read only when TOML does not yet exist, then atomically migrated and deleted after validation.

Each session is stored in `~/mini_agent/<session_id>/state.db`. Optional synchronization pushes SQLite snapshot operations over HTTPS at startup, checkpoint notification, and normal shutdown, then pulls remote revisions. It does not poll periodically, and failures leave the local outbox intact.

PostgreSQL is required only for sync-server and legacy-adapter integration tests:

```powershell
docker compose up -d
```

Tests use the separate `TEST_DATABASE_URL` database, reset its `public` schema, and must never point at development or production data. The deployable sync server reads `MINI_AGENT_DATABASE_URL` and `MINI_AGENT_SYNC_TOKEN`; clients receive only an HTTPS endpoint and bearer token.

Tasks can reference workspace files with `@relative/path`, for example `summarize @README.md`. References are expanded before planning, remain workspace-confined, and are bounded to avoid unintentional oversized prompts.

Interactive TUI input uses a full-screen Textual application: the selectable, scrollable transcript fills the viewport, while status and the editor remain fixed at the bottom. Enter submits and Ctrl+J inserts a newline. The editor grows with explicit or wrapped lines up to four rows, then keeps that height and scrolls internally. Type `/p` to see `/plan` and `/permission` with their command descriptions, then use Tab or the arrow keys to choose a command. Completion only activates at the beginning of a token, so slashes in paths and URLs remain ordinary text. Every status includes the active process-local permission mode.

The input prompt remains active while an agent run is in progress. Plain-text messages submitted during a run stay in a process-local TUI queue; after the active run finishes, the queue is merged in submission order and starts one follow-up run. Pressing Esc requests cooperative cancellation and starts the queued follow-up after cancellation completes. `/quit` and Ctrl+C instead cancel, discard the queue, and exit. Slash commands remain unavailable during a run. Plan questions, Plan Review, Tool Review, and permission selection hide the main editor and make the bottom choice area exclusive; selecting Other directly skips a question with an empty answer list, while Tab opens its custom editor.

Transcript writes from worker threads are coalesced at about 30 FPS, so streaming reasoning never redraws the input buffer directly. The view keeps the latest 200,000 characters; complete history remains in per-session SQLite and JSONL. PageUp or the mouse wheel pauses tail-following, while PageDown or Ctrl+End returns to the latest output. The alternate screen is restored on exit, followed by the current session ID and `/resume <session_id>`/`mini-agent --resume <session_id>` instructions.

## Local validation

Run the required local checks from the repository root:

```powershell
python -m ruff check .
python -m ruff format --check .
python -m pytest --cov=backend --cov-report=term-missing
```

For a fast test-only loop:

```powershell
python -m pytest -q
```

The suite keeps focused contract and high-risk integration coverage. Avoid adding exhaustive TUI key/mouse variants or duplicate end-to-end scenarios when a representative state-transition test already exists. The configured 70% coverage gate measures the non-visual core; TUI contract tests still run, but Textual rendering modules are excluded from the percentage because exhaustive event-loop variants are intentionally not maintained.

## Running the application

```powershell
python run.py --planner rule "calculate (18 + 6) * 4"
python -m tui --planner rule "calculate 2 + 2"
python run.py "读取 README.md"
mini-agent
```

The rule planner is offline and deterministic. The default LLM planner reads `[model]` only from `~/mini_agent/config.toml`; never commit real API keys or synchronization tokens.

### Skills, Subagents, and MCP

Global Skills live under `~/mini_agent/skills/<name>/SKILL.md`, while workspace Skills live under `.mini_agent/skills/<name>/SKILL.md` and fully override same-named global entries. Discovery validates YAML frontmatter, directory/name equality, UTF-8, size, line count, and path confinement.

Explicit `$skill-name` references activate installed Skills directly and fail before the task model call when a name is unknown. Automatic routing is disabled by default; set `[skills].auto_select = true` only when the extra selector model call is desired. Selector calls are counted separately from task model turns.

The LLM runner exposes `delegate_tasks` and `get_subagent_results` only on the parent runner. Child runs execute concurrently in one process with standard tools and shared-workspace write coordination, but cannot recursively delegate. Delegate only self-contained tasks; dependent tasks and overlapping writes should remain sequential.

The `[subagents]` table bounds `max_tasks_per_batch`, `max_workers`, `task_timeout_seconds`, and `batch_timeout_seconds`. Worker threads submit parent events and approvals through a bounded bridge; only the parent invocation thread mutates durable runtime state. Time spent waiting for approval is excluded from execution deadlines, while cancellation remains cooperative at model, command, and tool boundaries.

Run the safe stdio MCP server without model or database configuration:

```powershell
mini-agent-mcp --workspace C:\path\to\workspace
```

The MCP adapter intentionally exports only approval-free read tools. The client separately merges `~/mini_agent/mcp.toml` with `.mini_agent/mcp.toml`, fully overriding same-named project servers; external tools use long-lived stdio sessions, always require approval, and are unavailable in Plan mode.

Global MCP configuration is treated as user-owned and trusted. A project MCP file is keyed by canonical workspace-path and semantic configuration hashes in `~/mini_agent/mcp-trust.toml`; moving the workspace or changing any command, argument, working directory, or environment value requires approval again. Run `mini-agent --workspace <path> --trust-project-mcp` to review environment variable names (never values), record trust, and exit without starting a server. Non-interactive runs refuse untrusted project MCP.

The `[mcp]` table configures finite positive initialization, call, and shutdown timeouts. Tool names are validated before registry insertion, MCP sessions are owned by one application/runner instance, and closing that instance cannot stop another instance's MCP processes.

Plan mode supports ordinary read-only conversation plus two built-in control ToolSpecs. `request_user_input` asks material clarification questions after exploration cannot resolve them. `request_plan_review` submits a non-empty Markdown plan for explicit review only when a plan is useful and complete. Both controls must be called alone, are excluded from `ToolRegistry`, and persist as one structured ToolMessage. Plain assistant text completes as a normal `response`; only `request_plan_review` opens `PLAN REVIEW`. `/history` opens a read-only full-screen USER/ASSISTANT projection and returns to the live transcript with Esc.

`PLAN REVIEW` offers exactly three choices. `Implement` completes the Plan run and starts a separate Agent run in the current session with the complete Plan conversation plus `Implement the plan`. `Implement and Clear Session` creates and activates a new isolated session containing only the final plan and implementation message. `Cancel and Stay in plan mode` cancels the run, retains the complete Plan conversation, and leaves the TUI in Plan mode. Plan Review has no Supplement option; Tool Review continues to use `Continue`, `Cancel`, and `Supplement`.

## Runtime data

The application writes mutable client data below `~/mini_agent`:

- `config.toml`, `mcp.toml`, and `skills/`: global client configuration and extensions;
- `logs/`: redacted JSONL audit streams;
- `<session_id>/state.db`: messages, runs, checkpoints, runtime/audit events, metadata, and sync outbox.

Set `runtime.log_full_messages = false` to write summaries instead of complete redacted message bodies. Authentication headers, model credentials, sync tokens, and MCP environment values must never be persisted or included in model context.

## Change boundaries

- Domain types remain independent of the TUI, providers, and concrete storage. Active chat history contains only system, user, and assistant messages.
- Runtime keeps only lazy public exports at the package root and groups implementations into `core`, `application`, `execution`, `conversation`, `planning`, and `persistence`. `ConversationService` owns the active session, one durable turn at a time, same-session handoffs, and isolated-session handoffs requested by `RunHandoff.new_session`.
- Tools validate untrusted model arguments and remain workspace-confined. Keep default workspace assembly in `tools/catalog.py`; keep `ToolRegistry` independent of concrete tool implementations.
- Provider adapters own wire conversion only and accept active chat messages. Keep reusable HTTP and SSE mechanics in `providers/transport.py`.
- MCP reuses the tool registry but exposes only read-only, approval-free tools; do not add a second filesystem implementation.
- Subagent coordination belongs at the runtime composition boundary. Preserve single-level delegation, parent cancellation/approval routing, persisted batch summaries, and write coordination.
- The client composition root selects SQLite. PostgreSQL belongs only to the deployable sync server (legacy adapters remain isolated for compatibility tests).
- TUI renders runtime events and owns terminal commands/approval prompts; keep application loops, components, screens, rendering state, and view behavior in their focused subpackages. Keep the Plan Review and Tool Review decision sets separate, and do not implement tool behavior or session persistence in the TUI.

When adding behavior, add or update focused tests in `tests/`, then run the complete validation commands before opening a pull request.
