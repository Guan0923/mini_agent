"""Command-line entry point for the mini-agent benchmark harness.

Usage::

    python -m benchmarks.run --list
    python -m benchmarks.run --task tools-read-file --planner rule   # free offline smoke
    python -m benchmarks.run --all --output report.json              # full run (llm mode)
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks.run",
        description="Mini-Agent benchmark harness: run agent tasks and grade the results.",
    )
    parser.add_argument("--list", action="store_true", help="List available tasks and exit.")
    parser.add_argument(
        "--task",
        action="append",
        default=[],
        metavar="NAME",
        help="Run specific tasks (repeatable or comma-separated).",
    )
    parser.add_argument("--all", action="store_true", help="Run every task (default when no --task is given).")
    parser.add_argument(
        "--capability",
        choices=("skills", "tools", "mcp", "subagents"),
        help="Filter tasks by capability.",
    )
    parser.add_argument(
        "--planner",
        choices=("llm", "rule"),
        default="llm",
        help="Planner to use: llm (real model, needs config) or rule (offline, free).",
    )
    parser.add_argument("--output", type=Path, help="JSON report path (default benchmarks/output/<ts>/report.json).")
    parser.add_argument("--sandbox", type=Path, help="Sandbox root directory for client state.")
    parser.add_argument("--config", type=Path, help="config.toml to seed the sandbox with (default ~/mini_agent/config.toml).")
    parser.add_argument("--keep-workspaces", action="store_true", help="Keep per-task workspaces for debugging.")
    parser.add_argument("--max-model-turns", type=int, help="Override max model turns for every task.")
    parser.add_argument("--max-tool-calls", type=int, help="Override max tool calls for every task.")
    return parser


def _preflight_model_config(config_path: Path, parser: argparse.ArgumentParser) -> None:
    """Fail early with a clear message when llm mode has no usable model config."""
    if not config_path.exists():
        parser.error(
            f"model config not found at {config_path}. Configure api_key/base_url/model in "
            "~/mini_agent/config.toml (any OpenAI-compatible endpoint works), or pass --config "
            "PATH, or use --planner rule for an offline smoke run."
        )
    try:
        with config_path.open("rb") as handle:
            values = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        parser.error(f"cannot read model config {config_path}: {exc}")
    model = values.get("model") if isinstance(values.get("model"), dict) else {}
    missing = [name for name in ("api_key", "base_url", "model") if not str(model.get(name) or "").strip()]
    if missing:
        parser.error(
            f"[model] in {config_path} is missing: {', '.join(missing)}. "
            "Configure it (any OpenAI-compatible endpoint works), or use --planner rule."
        )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    from .report import build_report, print_summary, write_report
    from .runner import run_one_task
    from .sandbox import Sandbox, activate_client_paths
    from .tasks import ALL_TASKS, resolve_tasks

    if args.list:
        for task in ALL_TASKS:
            modes = ",".join(sorted(task.planner_modes))
            print(f"{task.name:<24} [{task.capability:<8}] planner={modes:<7} {task.description}")
        return 0

    try:
        selected = resolve_tasks(args.task, capability=args.capability, planner=args.planner)
    except ValueError as exc:
        parser.error(str(exc))
    if not selected:
        parser.error("no tasks match the given filters")

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    if args.output is not None:
        output_path = args.output
        sandbox_root = args.sandbox or (output_path.parent / "sandbox")
    else:
        base = REPO_ROOT / "benchmarks" / "output" / timestamp
        output_path = base / "report.json"
        sandbox_root = args.sandbox or base / "sandbox"

    source_config = args.config or (Path.home() / "mini_agent" / "config.toml")
    if args.planner == "llm":
        _preflight_model_config(source_config, parser)

    sandbox = Sandbox(sandbox_root, source_config)
    sandbox.prepare()
    activate_client_paths(sandbox.paths)

    results = []
    for task in selected:
        print(f"[bench] running {task.name} ({task.capability}, planner={args.planner}) ...", file=sys.stderr)
        result = run_one_task(
            task,
            planner=args.planner,
            sandbox=sandbox,
            keep_workspaces=args.keep_workspaces,
            max_model_turns=args.max_model_turns,
            max_tool_calls=args.max_tool_calls,
        )
        results.append(result)
        print(f"[bench] {task.name}: status={result.status} score={result.score}", file=sys.stderr)

    meta = {
        "planner": args.planner,
        "timestamp": datetime.now(UTC).isoformat(),
        "config_source": str(source_config),
    }
    report = build_report(results, meta=meta)
    write_report(report, output_path)
    print_summary(report)
    print(f"\nreport written to {output_path}")

    return 1 if any(result.status == "error" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
