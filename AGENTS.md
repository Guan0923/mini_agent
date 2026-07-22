# Repository Guidelines

## Working Principles

- Before taking action, briefly state what you are about to do and why.
- Follow Occam's razor: choose the simplest direct change that fully solves the problem.
- Preserve unrelated user changes and untracked files. Inspect a dirty worktree before editing, and never use destructive Git commands without explicit authorization.
- Prefer evidence from the current source, tests, and configuration over assumptions or stale documentation.

## Project Structure & Dependency Direction

Mini-Agent is a Python 3.11+ terminal-first agent lab. Production code lives in `src/mini_agent/`; focused tests live in `tests/`.

- `domain/`: provider-neutral messages, plans, sessions, skills, errors, and run state.
- `planning/`: rule-based and LLM planners, context management, model request lifecycle, and structured-output parsing.
- `runtime/`: application composition, conversation orchestration, execution workflows, Plan mode, persistence ports, hooks, and runtime events.
- `providers/`: `client.py` orchestrates providers, `transport.py` owns generic JSON/SSE HTTP, and `deepseek/` owns DeepSeek wire conversion.
- `tools/`: contracts and registry plus grouped `filesystem/`, `web/`, `default_tools/`, and command implementations.
- `storage/`: SQLite checkpoint/session adapters split into operations, schema migration, and row mapping.
- `tui/`: CLI/application loops, approval components, screens, rendering, view behavior, and reusable widgets.
- `observability/`: JSONL logging, redaction, and event fan-out.

Keep dependencies inward: TUI composes runtime services; runtime invokes planner/tool ports; provider adapters never import TUI or storage; domain remains independent of outer layers.

## Build, Run, and Validation Commands

Run commands from the repository root:

```powershell
python -m pip install -e ".[dev]"
python run.py --planner rule                 # Offline interactive TUI
python run.py "calculate (18 + 6) * 4"      # One configured-provider task
python -m pytest -q                          # Complete focused test suite
python -m ruff check .
python -m ruff format --check .
```

`python run.py`, `python -m mini_agent`, and the installed `mini-agent` command share the same CLI entry point.

## Design and Coding Guidelines

- Use four-space indentation, type hints for public APIs, concise module docstrings, `snake_case` names, and `PascalCase` classes/dataclasses.
- Treat roughly 300 lines per file and at most 8 direct Python files per package as reviewability guidance, not mechanical limits. Split by responsibility; do not create thin forwarding modules only to satisfy a number.
- Keep HTTP/SSE mechanics in `providers/transport.py`; provider-specific request/response rules belong under the provider package.
- Register tools through `ToolRegistry`. Preserve JSON Schema validation, workspace confinement, output bounds, and approval requirements.
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

- Copy `.env.example` to `.env`; never commit real API keys or credentials. Process environment values override `.env` values.
- Treat model-generated tool arguments and all web/tool output as untrusted.
- Authentication headers, `.env` contents, and full process environments must never be persisted. Keep recursive redaction intact when changing logs.
- `LOG_FULL_MESSAGES=true` records complete redacted bodies; `false` records summaries. Both modes must remain schema-compatible across JSONL, SQLite, runtime state, and history projections.
