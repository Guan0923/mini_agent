# Repository Guidelines

## Working Principles

- Before taking action, briefly state what you are about to do and why.
- Follow Occam's razor: choose the simplest direct change that fully solves the problem.
- Preserve unrelated user changes and untracked files. Inspect a dirty worktree before editing, and never use destructive Git commands without explicit authorization.
- Prefer evidence from the current source, tests, and configuration over assumptions or stale documentation.

## Project Structure & Dependency Direction

Mini-Agent is a Python 3.11+ agent lab. Production code lives in `backend/src/` (the `backend` package: setuptools `package-dir` maps the flat `backend/src` directory to the import name `backend`), `tui/src/` (the `tui` package, **deprecated** — see below), and `frontend/src/` (web client, TypeScript); focused tests live in `tests/`.

- `backend/src/domain/`: provider-neutral messages, plans, sessions, skills, errors, and run state.
- `backend/src/planning/`: rule-based and LLM planners, context management, model request lifecycle, and structured-output parsing.
- `backend/src/runtime/`: application composition, conversation orchestration, execution workflows, Plan mode, durable recovery, hooks, runtime events, and subagent coordination.
- `backend/src/providers/`: `client.py` orchestrates providers, `transport.py` owns generic JSON/SSE HTTP, and `deepseek/` owns DeepSeek wire conversion.
- `backend/src/tools/`: contracts and registry plus grouped `filesystem/`, `web/`, `default_tools/`, command, and delegation implementations.
- `backend/src/mcp/`: safe stdio server plus layered, approval-gated external MCP clients.
- `backend/src/storage/sqlite.py`: per-session local state, checkpoints, and audit records below the unified user runtime.
- `backend/src/storage/user_settings.py`: per-user `user.db`, the local source of truth for authenticated settings.
- `backend/src/sync/`: encrypted cloud snapshots, background save/restore jobs, and PostgreSQL ciphertext storage.
- `backend/src/observability/`: JSONL logging, redaction, and event fan-out.
- `frontend/`: web client; it consumes backend APIs and must not import backend implementation modules directly.

### Deprecated: TUI client (`tui/`)

The Textual terminal client is **deprecated**: do not read or extend `tui/src/` for new work. It remains in the repo only as a legacy entry point (`run.py`, `python -m tui`, installed `mini-agent` / `mini-agent-net` commands) and existing tests still exercise it, but no new investment should go there. The current client direction is the web frontend (`frontend/` served by `python -m backend.api`).

Keep dependencies inward: runtime invokes planner/tool ports; provider adapters never import storage; domain remains independent of outer layers. The web frontend communicates through backend APIs rather than sharing runtime internals.

## Build, Run, and Validation Commands

Run commands from the repository root:

```powershell
python -m pip install -e "backend[sync]" -e tui   # editable install (uv workspace: uv sync)
docker compose up -d                       # Only for PostgreSQL integration/server tests
python -m backend.api                       # Web backend server (current client path)
python run.py --planner rule                 # TUI (deprecated)
python -m pytest -q                          # Complete focused test suite
python -m ruff check .
python -m ruff format --check .
```

`python run.py`, `python -m tui`, and the installed `mini-agent` command share the same TUI CLI entry point (deprecated).

## Design and Coding Guidelines

- Use four-space indentation, type hints for public APIs, concise module docstrings, `snake_case` names, and `PascalCase` classes/dataclasses.
- Treat roughly 300 lines per file and at most 8 direct Python files per package as reviewability guidance, not mechanical limits. Split by responsibility; do not create thin forwarding modules only to satisfy a number.
- Keep HTTP/SSE mechanics in `providers/transport.py`; provider-specific request/response rules belong under the provider package.
- Register tools through `ToolRegistry`. Preserve JSON Schema validation, workspace confinement, output bounds, and approval requirements.
- Keep MCP exports restricted to read-only tools that do not require interactive confirmation.
- Keep subagent delegation single-level unless recursive coordination, budgets, cancellation, and persistence are explicitly redesigned together.
- Keep tool behavior out of presentation layers and the runner. Runtime publishes `RuntimeEvent`; presentation belongs in the client tier (web frontend), not the backend.
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

Use focused Conventional Commit-style messages such as `feat: add anthropic adapter` or `fix: render agent replies`. Pull requests should summarize behavior changes, list validation commands, link relevant issues, and include screenshots/terminal output for user-visible presentation changes.

## Security and Configuration

- Authenticated Web data lives below `~/.mini_agent/<user_id>/`; `config.toml` owns simple profile/agent/runtime preferences, while `user.db` owns provider ciphertext and sync state. `runtime/<session_id>/` owns `state.db`, workspace, and uploads. Web runtime must never fall back to a root-level config.
- Server deployment values such as PostgreSQL, CORS, SMTP, and cloud master-key versions come from process environment or the deployment secret manager. Standalone TOML config compatibility is legacy-only (the TUI client is deprecated; authenticated settings live in `user.db`).
- Treat model-generated tool arguments and all web/tool output as untrusted.
- Authentication headers, config secrets, legacy `.env` contents, sync tokens, MCP environment values, and full process environments must never be persisted. Keep recursive redaction intact when changing logs.
- `runtime.log_full_messages = true` records complete redacted bodies; `false` records summaries. Both modes must remain schema-compatible across JSONL, SQLite runtime/audit records, sync snapshots, runtime state, and history projections.
