"""Interactive terminal UI; it delegates all application setup to runtime.factory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mini_agent.observability import EventFanout, JsonlRunLogger
from mini_agent.providers import ModelConfigurationError
from mini_agent.runtime import AgentRunner, RunnerSettings, build_runner

from .approval import TerminalApproval
from .presenter import TerminalPresenter

HELP = """Commands:
  <task>                 Run a task.
  /agent                 Enter normal Agent mode.
  /plan                  Create a plan, then require approval before executing it.
  :help                  Show this help.
  :tools                 List available tools.
  :trace                 Print the last run trace as JSON.
  :quit                  Exit.
"""


class TerminalApp:
    def __init__(self, runner: AgentRunner, log_dir: Path | None = None) -> None:
        self.runner = runner
        self.last_state = None
        self.mode = "agent"
        self.conversation: list[dict[str, str]] = []
        self.presenter = TerminalPresenter()
        self._approval = TerminalApproval()
        sinks = [self.presenter.on_event]
        if log_dir is not None:
            sinks.append(JsonlRunLogger(log_dir))
        self._event_sink = EventFanout(sinks)

    def start(self) -> None:
        print("Mini-Agent TUI — type :help for commands, :quit to exit.")
        while True:
            try:
                prompt = "mini-agent[plan]> " if self.mode == "plan" else "mini-agent> "
                task = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                print("\nBye.")
                return
            if not self._handle(task):
                return

    def run_task(self, task: str) -> None:
        self.last_state = self.runner.run(
            task,
            mode=self.mode,
            conversation=self.conversation if self.mode == "agent" else None,
            on_event=self._event_sink,
            interrupt=self._approval,
        )
        if self.last_state.mode == "agent" and self.mode == "plan":
            self.mode = "agent"
            print("Agent mode enabled after plan approval.")

    def _handle(self, task: str) -> bool:
        if not task:
            return True
        if task == "/agent":
            self.mode = "agent"
            print("Agent mode enabled.")
            return True
        if task == "/plan":
            self.mode = "plan"
            print("Plan mode enabled: execution requires plan approval. Use /agent to return to Agent mode.")
            return True
        if task == ":quit":
            print("Bye.")
            return False
        if task == ":help":
            print(HELP)
            return True
        if task == ":tools":
            print("\n".join(self.runner.tools.names()))
            return True
        if task == ":trace":
            print(json.dumps(self.last_state.to_dict(), ensure_ascii=False, indent=2) if self.last_state else "No run yet.")
            return True
        self.run_task(task)
        return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Mini-Agent terminal lab")
    parser.add_argument("task", nargs="*", help="Run one task and exit.")
    parser.add_argument("--workspace", default=".", help="Workspace available to tools (default: current directory).")
    parser.add_argument("--planner", choices=("llm", "rule"), default="llm", help="Planning strategy (default: llm).")
    parser.add_argument(
        "--strategy",
        choices=("auto", "reactive", "plan_execute", "dynamic_replan"),
        default="auto",
        help="Execution strategy override; auto lets the LLM choose (default: auto).",
    )
    parser.add_argument("--max-actions", type=int, default=8, help="Maximum model decisions per task (default: 8).")
    parser.add_argument("--max-retries", type=int, default=1, help="Retries for a failed tool call (default: 1).")
    parser.add_argument("--max-replans", type=int, default=2, help="Maximum dynamic replans per task (default: 2).")
    parser.add_argument("--log-dir", default="logs", help="Directory for persistent JSONL run logs (default: logs).")
    args = parser.parse_args()
    workspace = Path(args.workspace)
    try:
        settings = RunnerSettings(
            max_actions=args.max_actions,
            max_retries=args.max_retries,
            max_replans=args.max_replans,
            strategy=args.strategy,
        )
        runner = build_runner(workspace, args.planner, settings)
    except (ModelConfigurationError, ValueError) as exc:
        parser.error(f"{exc} Use --planner rule for offline mode.")
    app = TerminalApp(runner, workspace / args.log_dir)
    if args.task:
        app.run_task(" ".join(args.task))
    else:
        app.start()
