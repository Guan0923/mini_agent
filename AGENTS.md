# Repository Guidelines

## Working Principles

- Before taking action, briefly state what you are about to do and why.
- Follow Occam's razor: choose the simplest direct change that fully solves the problem.
- Preserve unrelated user changes and untracked files. Inspect a dirty worktree before editing, and never use destructive Git commands without explicit authorization.
- Prefer evidence from the current source, tests, and configuration over assumptions or stale documentation.

## Project Structure & Dependency Direction

Mini-Agent is a Python 3.11+ agent lab. Production code is split into `src/backend/` (runtime and adapters), `src/tui/` (Textual terminal interface), and `src/frontend/` (reserved for a future web frontend); focused tests live in `tests/`.

- `backend/domain/`: provider-neutral messages, plans, sessions, skills, errors, and run state.
- `backend/planning/`: rule-based and LLM planners, context management, model request lifecycle, and structured-output parsing.
- `backend/runtime/`: application composition, conversation orchestration, execution workflows, Plan mode, durable recovery, hooks, runtime events, and subagent coordination.
- `backend/providers/`: `client.py` orchestrates providers, `transport.py` owns generic JSON/SSE HTTP, and `deepseek/` owns DeepSeek wire conversion.
- `backend/tools/`: contracts and registry plus grouped `filesystem/`, `web/`, `default_tools/`, command, and delegation implementations.
- `backend/mcp/`: safe stdio server plus layered, approval-gated external MCP clients.
- `backend/storage/sqlite.py`: per-session local state, checkpoints, audit records, and sync outbox.
- `backend/sync/`: HTTPS client/coordinator and the isolated PostgreSQL synchronization service.
- `backend/observability/`: JSONL logging, redaction, and event fan-out.
- `tui/`: CLI/application loops, approval components, screens, rendering, view behavior, and reusable widgets.
- `frontend/`: reserved for a future browser frontend; it consumes backend APIs and must not import backend implementation modules directly.

Keep dependencies inward: TUI composes backend runtime services; runtime invokes planner/tool ports; provider adapters never import TUI or storage; domain remains independent of outer layers. Future frontend code communicates through backend APIs rather than sharing runtime internals.

## Build, Run, and Validation Commands

Run commands from the repository root:

```powershell
python -m pip install -e ".[dev]"
docker compose up -d                       # Only for PostgreSQL integration/server tests
python run.py --planner rule                 # Offline interactive TUI
python run.py "calculate (18 + 6) * 4"      # One configured-provider task
python -m pytest -q                          # Complete focused test suite
python -m ruff check .
python -m ruff format --check .
```

`python run.py`, `python -m tui`, and the installed `mini-agent` command share the same TUI CLI entry point.

## Design and Coding Guidelines

- Use four-space indentation, type hints for public APIs, concise module docstrings, `snake_case` names, and `PascalCase` classes/dataclasses.
- Treat roughly 300 lines per file and at most 8 direct Python files per package as reviewability guidance, not mechanical limits. Split by responsibility; do not create thin forwarding modules only to satisfy a number.
- Keep HTTP/SSE mechanics in `providers/transport.py`; provider-specific request/response rules belong under the provider package.
- Register tools through `ToolRegistry`. Preserve JSON Schema validation, workspace confinement, output bounds, and approval requirements.
- Keep MCP exports restricted to read-only tools that do not require interactive confirmation.
- Keep subagent delegation single-level unless recursive coordination, budgets, cancellation, and persistence are explicitly redesigned together.
- Keep tool behavior out of the TUI and runner. Runtime publishes `RuntimeEvent`; terminal presentation belongs under `tui/rendering/`.
- Reuse shared normalization, validation, path, and persistence helpers instead of duplicating private implementations.
- Use `apply_patch` for intentional source edits when available; bulk formatting and mechanical moves may use dedicated tools.

## Testing Guidelines

- Use `pytest`; name files `test_*.py` and functions `test_<behavior>`.
- Keep a focused representative test for each contract or state transition. Avoid exhaustive key/mouse variants and duplicate end-to-end scenarios when lower-level coverage already proves the behavior.
- Preserve high-risk coverage for provider parsing/transport errors, retries, redaction, persistence migrations, path confinement, approvals, and command execution.
- Mock HTTP sessions and provider responses; automated tests must never call a paid model API.
- When changing tools, cover success, invalid arguments, permission boundaries, and path/security failures.
- Run the complete suite after structural changes. On Windows, use an external pytest `--basetemp` if the workspace temp directory has ACL issues.

## Commits and Pull Requests

Use focused Conventional Commit-style messages such as `feat: add anthropic adapter` or `fix: render agent replies`. Pull requests should summarize behavior changes, list validation commands, link relevant issues, and include terminal output for user-visible TUI changes.

## Security and Configuration

- Client configuration comes only from `~/mini_agent/config.toml`; a workspace `.env` is a one-time migration source and process environment values do not override client TOML.
- Treat model-generated tool arguments and all web/tool output as untrusted.
- Authentication headers, config secrets, legacy `.env` contents, sync tokens, MCP environment values, and full process environments must never be persisted. Keep recursive redaction intact when changing logs.
- `runtime.log_full_messages = true` records complete redacted bodies; `false` records summaries. Both modes must remain schema-compatible across JSONL, SQLite runtime/audit records, sync snapshots, runtime state, and history projections.
