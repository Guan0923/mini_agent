# Repository Guidelines

## Project Structure & Module Organization

Mini-Agent is a Python 3.11+ terminal-first agent lab. Source code lives in `src/mini_agent/`; tests live in `tests/`.

- `domain/`: pure run-state types such as `AgentAction` and `RunState`.
- `runtime/`: dependency assembly, execution loop, settings, and structured runtime events.
- `planning/`: rule-based and LLM planning strategies.
- `tools/`: tool contracts, registry, calculator, and workspace-confined file operations.
- `providers/`: HTTP transport plus provider-specific adapters, currently DeepSeek.
- `tui/`: terminal input, output, and confirmation prompts.

Keep dependency flow inward: TUI composes the runtime; runtime invokes planners and tools; provider adapters must not depend on TUI code.

## Build, Test, and Development Commands

Run these commands from the repository root:

```powershell
python run.py --planner rule                 # Run offline interactive TUI
python run.py "calculate (18 + 6) * 4"      # Run one task with configured provider
python -m pytest -q                          # Run the complete test suite
```

`pyproject.toml` defines the `src` layout and the only runtime dependency, `requests`. Use a virtual environment when installing dependencies: `python -m pip install -e .`.

## Coding Style & Naming Conventions

Use four-space indentation, type hints for public APIs, and concise module docstrings. Prefer `snake_case` for modules, functions, and variables; use `PascalCase` for classes and dataclasses. Keep provider transport generic in `providers/client.py`; put provider-specific request construction and response parsing in files such as `providers/deepseek.py`.

Do not add tool behavior directly to the TUI or runner. Register tools through `ToolRegistry`, and preserve workspace path checks and confirmation requirements for writes. The runner publishes `RuntimeEvent` objects; terminal formatting belongs in `tui/presenter.py`.

## Testing Guidelines

Use `pytest`; name files `test_*.py` and functions `test_<behavior>`. Mock HTTP sessions for provider tests—never call a paid API in automated tests. Add tests for success paths, invalid provider responses, and permission or path-boundary failures when changing tools.

## Commit & Pull Request Guidelines

Follow the existing Conventional Commit-style pattern: `type: concise summary`, for example `feat: add anthropic adapter` or `fix: render agent replies`. Keep commits focused. Pull requests should describe behavior changes, list test commands run, link relevant issues, and include terminal output for user-visible TUI changes.

## Security & Configuration

Copy `.env.example` to `.env`; never commit real API keys. Treat model-generated tool arguments as untrusted and validate them before execution.
