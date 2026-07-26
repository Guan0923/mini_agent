# Development Guide

## Environment

Mini-Agent requires Python 3.11 or newer. Create an isolated environment and install the package with its development tools:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

The editable install exposes the `mini-agent` command and keeps the `src/` package layout used by tests.

### PostgreSQL

Start the local database before running the application or tests:

```powershell
docker compose up -d
Copy-Item .env.example .env
```

`DATABASE_URL` is required by the application and defaults to the Compose development database. Tests use the separate `TEST_DATABASE_URL` database, reset its `public` schema before each test, and must never point at a development or production database. Stop the local service with `docker compose down`; add `-v` only when deliberately discarding all PostgreSQL data.

Provider configuration comes from `<workspace>/.env`, with process environment variables taking precedence. `BASE_URL` may be a service root, a URL ending in `/v1`, or the complete `/chat/completions` endpoint. Only keys documented in `.env.example` are supported; invalid numeric/boolean values fail during startup.

Tasks can reference workspace files with `@relative/path`, for example `summarize @README.md`. References are expanded before planning, remain workspace-confined, and are bounded to avoid unintentional oversized prompts.

Interactive TUI input uses a full-screen Textual application: the selectable, scrollable transcript fills the viewport, while status and the editor remain fixed at the bottom. Enter submits and Ctrl+J inserts a newline. The editor grows with explicit or wrapped lines up to four rows, then keeps that height and scrolls internally. Type `/p` to see `/plan` and `/permission` with their command descriptions, then use Tab or the arrow keys to choose a command. Completion only activates at the beginning of a token, so slashes in paths and URLs remain ordinary text. Every status includes the active process-local permission mode.

The input prompt remains active while an agent run is in progress. Plain-text messages submitted during a run stay in a process-local TUI queue; after the active run finishes, the queue is merged in submission order and starts one follow-up run. Pressing Esc requests cooperative cancellation and starts the queued follow-up after cancellation completes. `/quit` and Ctrl+C instead cancel, discard the queue, and exit. Slash commands remain unavailable during a run. Plan questions, Plan Review, Tool Review, and permission selection hide the main editor and make the bottom choice area exclusive; selecting Other directly skips a question with an empty answer list, while Tab opens its custom editor.

Transcript writes from worker threads are coalesced at about 30 FPS, so streaming reasoning never redraws the input buffer directly. The view keeps the latest 200,000 characters; complete history remains in PostgreSQL and JSONL. PageUp or the mouse wheel pauses tail-following, while PageDown or Ctrl+End returns to the latest output. The alternate screen is restored on exit, followed by the current session ID and `/resume <session_id>`/`mini-agent --resume <session_id>` instructions.

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

The rule planner is offline and deterministic. The default LLM planner reads provider settings from `.env`; never commit that file or real API keys.

### Skills, Subagents, and MCP

Workspace Skills live under `.mini_agent/skills/<name>/SKILL.md`. Discovery validates YAML frontmatter, directory/name equality, UTF-8, size, line count, and path confinement. Add focused catalog/activation tests whenever the manifest contract changes; instructions and assets never bypass the normal tool or approval boundaries.

The LLM runner exposes `delegate_tasks` and `get_subagent_results` only on the parent runner. Child runs execute concurrently in one process with standard tools and shared-workspace write coordination, but cannot recursively delegate. Delegate only self-contained tasks; dependent tasks and overlapping writes should remain sequential.

Run the safe stdio MCP server without model or database configuration:

```powershell
mini-agent-mcp --workspace C:\path\to\workspace
```

The MCP adapter intentionally exports only approval-free read tools. Changes to its exposure policy require tests for definitions, argument validation, workspace confinement, and rejection of mutation/network tools.

Plan mode supports ordinary read-only conversation plus two built-in control ToolSpecs. `request_user_input` asks material clarification questions after exploration cannot resolve them. `request_plan_review` submits a non-empty Markdown plan for explicit review only when a plan is useful and complete. Both controls must be called alone, are excluded from `ToolRegistry`, and persist as one structured ToolMessage. Plain assistant text completes as a normal `response`; only `request_plan_review` opens `PLAN REVIEW`. `/history` opens a read-only full-screen USER/ASSISTANT projection and returns to the live transcript with Esc.

`PLAN REVIEW` offers exactly three choices. `Implement` completes the Plan run and starts a separate Agent run in the current session with the complete Plan conversation plus `Implement the plan`. `Implement and Clear Session` creates and activates a new isolated session containing only the final plan and implementation message. `Cancel and Stay in plan mode` cancels the run, retains the complete Plan conversation, and leaves the TUI in Plan mode. Plan Review has no Supplement option; Tool Review continues to use `Continue`, `Cancel`, and `Supplement`.

## Runtime data

The application writes local runtime data below the selected workspace:

- `logs/`: JSONL audit streams for individual runs, including normalized messages plus provider wire request/response bodies, stream events, transport status, retry attempts, durations, tool timing, and run summaries;
- PostgreSQL (`DATABASE_URL`): run checkpoints, typed runtime snapshots, compact conversation projections, and ordered session runtime messages;

These paths are ignored by Git. `LOG_FULL_MESSAGES=true` is the development default; set it to `false` in `.env` to write summaries instead of complete audit-message bodies. Sensitive key names and values are redacted in both modes. Wire payloads are retained without authentication headers; stream responses are stored as ordered JSON events. Do not use production credentials or sensitive personal data in checked-in fixtures.

## Change boundaries

- Domain types remain independent of the TUI, providers, and concrete storage. Active chat history contains only system, user, and assistant messages.
- Runtime keeps only lazy public exports at the package root and groups implementations into `core`, `application`, `execution`, `conversation`, `planning`, and `persistence`. `ConversationService` owns the active session, one durable turn at a time, same-session handoffs, and isolated-session handoffs requested by `RunHandoff.new_session`.
- Tools validate untrusted model arguments and remain workspace-confined. Keep default workspace assembly in `tools/catalog.py`; keep `ToolRegistry` independent of concrete tool implementations.
- Provider adapters own wire conversion only and accept active chat messages. Keep reusable HTTP and SSE mechanics in `providers/transport.py`.
- MCP reuses the tool registry but exposes only read-only, approval-free tools; do not add a second filesystem implementation.
- Subagent coordination belongs at the runtime composition boundary. Preserve single-level delegation, parent cancellation/approval routing, persisted batch summaries, and write coordination.
- Storage adapters implement runtime ports and are chosen only by `runtime.factory`; PostgreSQL schema migration and row mapping remain separate from session operations.
- TUI renders runtime events and owns terminal commands/approval prompts; keep application loops, components, screens, rendering state, and view behavior in their focused subpackages. Keep the Plan Review and Tool Review decision sets separate, and do not implement tool behavior or session persistence in the TUI.

When adding behavior, add or update focused tests in `tests/`, then run the complete validation commands before opening a pull request.
