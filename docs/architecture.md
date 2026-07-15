# Architecture

Mini-Agent uses a small, dependency-inward architecture so that models, tools, and user interfaces can evolve independently.

```text
TUI adapters → runtime → planning / tools / providers → domain
                    ↓              ↓
          observability sinks   checkpoint store
```

## Responsibilities

- `domain` owns serializable run state and model actions. It has no HTTP, filesystem, or terminal dependencies.
- `runtime` is the application layer. `factory.py` composes dependencies and `config.py` stores execution limits. `runner.py` only creates a run and dispatches it: `routing.py` resolves the execution strategy, `workflows.py` implements Reactive, Plan-Execute, and Dynamic-Replan flows, `steps.py` owns confirmation/retry/tool events, and `outcomes.py` owns completed/failed state transitions. `checkpointing.py` defines the storage boundary; `checkpoints.py` is the SQLite adapter. Every runtime component publishes `RuntimeEvent` values instead of terminal strings.
- `runtime.contracts` defines Human-in-the-Loop requests and decisions. Runtime asks for approval before each tool call and after a Plan-mode plan is prepared; it never reads terminal input directly.
- `observability` contains optional event consumers. `EventFanout` broadcasts the same event to the TUI and persistent sinks; `JsonlRunLogger` writes one complete JSON object per event to an isolated run log. Runtime code never opens log files directly.
- `planning` turns conversation history into a validated next action. LLM and rule planners implement the same protocol.
- `tools` contains local capabilities. The registry labels read-only tools so interfaces and policies can distinguish mutations. `run_command` is a non-read-only workspace-rooted adapter: it uses Bash on Unix-like systems and PowerShell on Windows, and requires one explicit Human-in-the-Loop approval because a shell command can have arbitrary side effects.
- `providers` contains HTTP transport and vendor adapters. `client.py` owns request transport; `deepseek.py` owns DeepSeek payloads, response parsing, and SSE deltas.
- `tui` owns prompts, command handling, confirmations, and rendering runtime events. `approval.py` is the terminal adapter for the runtime approval contract.

## Extension Rules

To add a provider, create a provider adapter that satisfies the planner's completion methods; do not add provider parsing to `runtime` or `tui`. To add a tool, implement it behind the `Tool` contract and register it in `ToolRegistry`, setting `read_only=False` for any mutation. To add an execution strategy, add a workflow that consumes `RunState`, `ToolStepExecutor`, and `RuntimeEvent`; do not put strategy-specific loops back in `AgentRunner`. Dynamic workflows should preserve previous plan revisions and replace only pending steps. To add another checkpoint backend, implement `CheckpointStore` and wire it in `factory.py`. To add another interface, implement the approval contract and consume `RuntimeEvent` through independent adapters rather than duplicating execution logic.
