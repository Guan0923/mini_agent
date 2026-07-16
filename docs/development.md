# Development Guide

## Environment

Mini-Agent requires Python 3.11 or newer. Create an isolated environment and install the package with its development tools:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

The editable install exposes the `mini-agent` command and keeps the `src/` package layout identical to CI.

Tasks can reference workspace files with `@relative/path`, for example `summarize @README.md`. References are expanded before planning, remain workspace-confined, and are bounded to avoid unintentional oversized prompts.

Interactive TUI input uses a full-screen Textual application: the selectable, scrollable transcript fills the viewport, while status and single-line input remain fixed at the bottom. Type `/p` to see `/plan`, use Tab or the arrow keys to choose a command, and press Enter to submit it. Completion only activates at the beginning of a token, so slashes in paths and URLs remain ordinary text.

The input prompt remains active while an agent run is in progress. Plain-text messages submitted at `mini-agent[running]>` are merged in submission order and applied at the next safe point after the current model request or tool call finishes. Slash commands are unavailable during a run. Plan and tool reviews take precedence over steering input and temporarily switch the prompt to the existing review choices.

Transcript writes from worker threads are coalesced at about 30 FPS, so streaming reasoning never redraws the input buffer directly. The view keeps the latest 200,000 characters; complete history remains in SQLite and JSONL. PageUp or the mouse wheel pauses tail-following, while PageDown or Ctrl+End returns to the latest output. The alternate screen is restored on exit, followed only by the current session ID and `/use`/`--session-id` resume commands.

## Local validation

Run the same checks used by CI from the repository root:

```powershell
ruff check .
ruff format --check .
python -m pytest --cov=mini_agent --cov-report=term-missing
```

For a fast test-only loop:

```powershell
python -m pytest -q
```

## Running the application

```powershell
python run.py --planner rule "calculate (18 + 6) * 4"
python -m mini_agent --planner rule "calculate 2 + 2"
python run.py "读取 README.md"
mini-agent
```

The rule planner is offline and deterministic. The default LLM planner reads provider settings from `.env`; never commit that file or real API keys.

In Plan mode, the model may use the built-in `request_user_input` control ToolSpec after read-only exploration. It is not one of the three registered execution tools. Each call asks one to three single-choice questions with two or three model options; the TUI appends a final free-form option, collects all answers, and resumes the same Plan run with one structured ToolMessage result. Multiple question rounds are allowed within `max_actions`; `PLAN REVIEW` starts only after the model returns the final numbered plan.

`PLAN REVIEW` offers exactly three choices. `Implement` completes the Plan run and starts a separate Agent run in the current session with the complete Plan conversation plus `Implement the plan`. `Implement and Clear Session` creates and activates a new isolated session containing only the final plan and implementation message. `Cancel and Stay in plan mode` cancels the run, retains the complete Plan conversation, and leaves the TUI in Plan mode. Plan Review has no Supplement option; Tool Review continues to use `Continue`, `Cancel`, and `Supplement`.

## Runtime data

The application writes local runtime data below the selected workspace:

- `logs/`: JSONL audit streams for individual runs, including normalized model request/response messages;
- `.mini_agent/checkpoints.db`: run checkpoints, typed runtime snapshots, compact conversation projections, and ordered session runtime messages;

These paths are ignored by Git. `LOG_FULL_MESSAGES=True` is the development default; set it to `False` in `.env` to write summaries instead of complete audit-message bodies. Sensitive key names and values are redacted in both modes. Existing artifact files are left untouched, but the active Agent runtime no longer creates or consumes them. Do not use production credentials or sensitive personal data in checked-in fixtures.

## Change boundaries

- Domain types remain independent of the TUI, providers, and concrete storage. Active chat history contains only system, user, and assistant messages.
- Runtime owns strategy execution, preprocessing, and checkpoint/session ports. `ConversationService` owns the active session, one durable turn at a time, same-session handoffs, and isolated-session handoffs requested by `RunHandoff.new_session`.
- Tools validate untrusted model arguments and remain workspace-confined. Keep default workspace assembly in `tools/catalog.py`; keep `ToolRegistry` independent of concrete tool implementations.
- Provider adapters own wire conversion only and accept active chat messages, never artifact snapshots. Keep reusable HTTP and SSE mechanics in `providers/client.py`.
- Storage adapters implement runtime ports and are chosen only by `runtime.factory`. Dormant artifact adapters remain independently testable but are not composed into AgentRunner.
- TUI renders runtime events and owns terminal commands/approval prompts; keep the Plan Review and Tool Review decision sets separate, and do not implement tool behavior or session persistence in the TUI.

When adding behavior, add or update focused tests in `tests/`, then run the complete validation commands before opening a pull request.
