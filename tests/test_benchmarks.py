from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.grading.scoring import summarize
from benchmarks.grading.verifiers import python_verifier
from benchmarks.metrics import RunMetrics
from benchmarks.model import CheckerVerdict, TaskResult
from benchmarks.report import build_report
from benchmarks.sandbox import Sandbox
from benchmarks.tasks import ALL_TASKS
from benchmarks.verifiers.verify import VERIFIERS

FIXTURES = Path(__file__).parents[1] / "benchmarks" / "fixtures"


def _copy_fixture(name: str, destination: Path) -> Path:
    shutil.copytree(FIXTURES / name, destination)
    return destination


def _metrics() -> RunMetrics:
    return RunMetrics(1.0, 1, 1, 0, 0, 0, 0, 0, [])


def _result(task_name: str, capability: str, score: float | None, attempt: int, passed: bool) -> TaskResult:
    return TaskResult(
        task_name=task_name,
        capability=capability,
        status="completed" if score is not None else "error",
        score=score,
        final_answer="",
        metrics=_metrics(),
        verdicts=[CheckerVerdict(score)] if score is not None else [],
        passed=passed,
        attempt=attempt,
    )


def test_registry_is_nine_source_backed_tasks() -> None:
    assert len(ALL_TASKS) == 9
    assert len({task.name for task in ALL_TASKS}) == 9
    assert {task.capability for task in ALL_TASKS} == {"terminal", "software_engineering", "tool_workflow"}
    assert all(sum(task.capability == capability for task in ALL_TASKS) == 3 for capability in _capabilities())
    assert all(task.planner_modes == frozenset({"llm"}) for task in ALL_TASKS)
    for task in ALL_TASKS:
        source = task.source
        assert all(
            isinstance(value, str) and value.strip()
            for value in (
                source.benchmark,
                source.task_id,
                source.url,
                source.source_revision,
                source.license,
                source.adaptation_notes,
            )
        )


def _capabilities() -> tuple[str, ...]:
    return ("terminal", "software_engineering", "tool_workflow")


@pytest.mark.parametrize("task", ALL_TASKS, ids=lambda task: task.name)
def test_untouched_fixture_fails_its_verifier(task) -> None:
    # Calling the standalone verifier through its task-specific name keeps the
    # assertion explicit while avoiding any model/runtime dependency.
    names = {
        "terminal-analyze-access-log": "terminal_access_log",
        "terminal-cancel-async-tasks": "terminal_cancel_async",
        "terminal-countdown-462": "terminal_countdown",
        "swe-requests-2317": "swe_requests",
        "swe-pytest-11143": "swe_pytest",
        "swe-astropy-14365": "swe_astropy",
        "tau-retail-0-exchange": "tau_retail_exchange",
        "tau-retail-113-cancel-all": "tau_retail_cancel_all",
        "tau-airline-3-baggage": "tau_airline_baggage",
    }
    passed, _ = VERIFIERS[names[task.name]](FIXTURES / task.seed.fixture, "")
    assert not passed


def test_terminal_access_log_oracle_passes(tmp_path: Path) -> None:
    workspace = _copy_fixture("terminal-analyze-access-log", tmp_path / "workspace")
    (workspace / "report.txt").write_text(
        "Total requests: 7\n"
        "Unique IP addresses: 3\n"
        "Top 3 URLs:\n"
        "  /api/users: 3\n"
        "  /health: 2\n"
        "  /login: 1\n"
        "404 errors: 1\n",
        encoding="utf-8",
    )
    assert VERIFIERS["terminal_access_log"](workspace, "")[0]


def test_terminal_countdown_oracle_passes_and_accepts_one_final_newline(tmp_path: Path) -> None:
    workspace = _copy_fixture("terminal-countdown-462", tmp_path / "workspace")
    (workspace / "output.txt").write_text("(3 + 11) * (8 + 25)\r\n", encoding="utf-8", newline="")
    assert VERIFIERS["terminal_countdown"](workspace, "")[0]


def test_terminal_async_oracle_passes(tmp_path: Path) -> None:
    workspace = _copy_fixture("terminal-cancel-async-tasks", tmp_path / "workspace")
    (workspace / "run.py").write_text(
        "import asyncio\n\n"
        "async def run_tasks(tasks, max_concurrent):\n"
        "    semaphore = asyncio.Semaphore(max_concurrent)\n"
        "    async def invoke(task):\n"
        "        async with semaphore:\n"
        "            await task()\n"
        "    children = [asyncio.create_task(invoke(task)) for task in tasks]\n"
        "    try:\n"
        "        await asyncio.gather(*children)\n"
        "    except BaseException:\n"
        "        for child in children:\n"
        "            if not child.done(): child.cancel()\n"
        "        await asyncio.gather(*children, return_exceptions=True)\n"
        "        raise\n",
        encoding="utf-8",
    )
    assert VERIFIERS["terminal_cancel_async"](workspace, "")[0]


def test_swe_oracles_pass(tmp_path: Path) -> None:
    requests = _copy_fixture("swe-requests-2317", tmp_path / "requests")
    request_path = requests / "requests_like" / "sessions.py"
    request_path.write_text(
        request_path.read_text(encoding="utf-8").replace(
            "self.method = builtin_str(method).upper()",
            "self.method = (method.decode('ascii') if isinstance(method, bytes) else builtin_str(method)).upper()",
        ),
        encoding="utf-8",
    )
    assert VERIFIERS["swe_requests"](requests, "")[0]

    pytest_workspace = _copy_fixture("swe-pytest-11143", tmp_path / "pytest")
    rewrite = pytest_workspace / "mini_pytest" / "rewrite.py"
    rewrite.write_text(
        rewrite.read_text(encoding="utf-8")
        .replace(
            'if isinstance(first, ast.Expr):\n        return "docstring"',
            'if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):\n        return "docstring"\n    if isinstance(first, ast.Expr):\n        return "expression"',
        )
        .replace(
            "return source\n",
            'return source + "\\n# AssertionError instrumentation"\n',
        ),
        encoding="utf-8",
    )
    assert VERIFIERS["swe_pytest"](pytest_workspace, "")[0]

    astropy = _copy_fixture("swe-astropy-14365", tmp_path / "astropy")
    source = astropy / "mini_astropy" / "qdp.py"
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "if tokens and tokens[0] in _COMMANDS:\n            commands.append((tokens[0], tokens[1:]))",
            "if tokens and tokens[0].upper() in _COMMANDS:\n            normalized = [token.upper() for token in tokens]\n            commands.append((normalized[0], normalized[1:]))",
        )
        + "\n\ndef parse_data_rows(text):\n    rows = []\n    for line in text.splitlines():\n        tokens = line.split()\n        if not tokens or tokens[0].upper() in _COMMANDS:\n            continue\n        row = []\n        for token in tokens:\n            if token.upper() == 'NO': row.append(None)\n            elif token.lower() == 'nan': row.append(float('nan'))\n            else: row.append(float(token))\n        rows.append(row)\n    return rows\n",
        encoding="utf-8",
    )
    assert VERIFIERS["swe_astropy"](astropy, "")[0]


def _write_audit(workspace: Path, entries: list[dict]) -> None:
    (workspace / "mcp_audit.jsonl").write_text(
        "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries), encoding="utf-8"
    )


def test_tau_oracles_pass(tmp_path: Path) -> None:
    exchange = _copy_fixture("tau-retail-0-exchange", tmp_path / "exchange")
    state = json.loads((exchange / "retail_state.json").read_text(encoding="utf-8"))
    state["orders"]["#W2378156"]["items"] = ["7706410293", "7747408585"]
    (exchange / "retail_state.json").write_text(json.dumps(state), encoding="utf-8")
    _write_audit(
        exchange,
        [
            {
                "name": "find_user_id_by_name_zip",
                "arguments": {"first_name": "Yusuf", "last_name": "Rossi", "zip": "19122"},
                "mutating": False,
            },
            {"name": "get_user_details", "arguments": {"user_id": "yusuf_rossi_19122"}, "mutating": False},
            {"name": "get_order_details", "arguments": {"order_id": "#W2378156"}, "mutating": False},
            *[
                {"name": "get_product_details", "arguments": {"product_id": product_id}, "mutating": False}
                for product_id in ("1151293680", "4983901480", "7706410293", "7747408585")
            ],
            {
                "name": "exchange_delivered_order_items",
                "arguments": {
                    "order_id": "#W2378156",
                    "item_ids": ["1151293680", "4983901480"],
                    "new_item_ids": ["7706410293", "7747408585"],
                    "payment_method_id": "credit_card_9513926",
                },
                "mutating": True,
            },
        ],
    )
    assert VERIFIERS["tau_retail_exchange"](exchange, "")[0]

    cancel = _copy_fixture("tau-retail-113-cancel-all", tmp_path / "cancel")
    cancel_state = json.loads((cancel / "retail_state.json").read_text(encoding="utf-8"))
    for order_id in ("#W5056519", "#W5995614"):
        cancel_state["orders"][order_id].update({"status": "cancelled", "cancellation_reason": "ordered by mistake"})
    (cancel / "retail_state.json").write_text(json.dumps(cancel_state), encoding="utf-8")
    _write_audit(
        cancel,
        [
            {
                "name": "find_user_id_by_name_zip",
                "arguments": {"first_name": "Yara", "last_name": "Muller", "zip": "85041"},
                "mutating": False,
            },
            {"name": "get_user_details", "arguments": {"user_id": "yara_muller_85041"}, "mutating": False},
            {"name": "list_pending_orders", "arguments": {"user_id": "yara_muller_85041"}, "mutating": False},
            *[
                {
                    "name": "cancel_pending_order",
                    "arguments": {"order_id": order_id, "reason": "ordered by mistake"},
                    "mutating": True,
                }
                for order_id in ("#W5056519", "#W5995614")
            ],
        ],
    )
    assert VERIFIERS["tau_retail_cancel_all"](cancel, "")[0]

    airline = _copy_fixture("tau-airline-3-baggage", tmp_path / "airline")
    _write_audit(
        airline,
        [
            {"name": "get_user_details", "arguments": {"user_id": "anya_garcia_5901"}, "mutating": False},
            {"name": "get_reservation_details", "arguments": {"reservation_id": "JMO1MG"}, "mutating": False},
        ],
    )
    assert VERIFIERS["tau_airline_baggage"](airline, "Anya is Silver and can bring 4 suitcases.")[0]


def test_sandbox_rejects_escape_and_isolates_fixtures(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        Sandbox._fixture_path(str(FIXTURES.resolve()))
    with pytest.raises(ValueError):
        Sandbox._fixture_path("../fixtures")
    with pytest.raises(ValueError):
        Sandbox._workspace_path(tmp_path, "../outside.txt")

    sandbox = Sandbox(tmp_path / "sandbox")
    sandbox.prepare()
    task = next(task for task in ALL_TASKS if task.name == "tau-retail-0-exchange")
    first = sandbox.materialize_workspace(task)
    second = sandbox.materialize_workspace(task)
    assert first != second
    first_state = first / "retail_state.json"
    first_state.write_text(first_state.read_text(encoding="utf-8").replace("delivered", "cancelled"), encoding="utf-8")
    assert "delivered" in (second / "retail_state.json").read_text(encoding="utf-8")
    mcp_config = (first / ".mini_agent" / "mcp.toml").read_text(encoding="utf-8")
    assert "--tools" in mcp_config and "exchange_delivered_order_items" in mcp_config


def test_verifier_failures_are_contained(monkeypatch, tmp_path: Path) -> None:
    checker = python_verifier("does-not-matter", timeout_seconds=1)
    context = SimpleNamespace(workspace=tmp_path, final_answer="")

    def timeout(*args, **kwargs):
        raise __import__("subprocess").TimeoutExpired(args[0], 1)

    monkeypatch.setattr("benchmarks.grading.verifiers.subprocess.run", timeout)
    assert checker(context).score == 0.0

    def start_error(*args, **kwargs):
        raise OSError("cannot start")

    monkeypatch.setattr(
        "benchmarks.grading.verifiers.subprocess.run",
        start_error,
    )
    assert checker(context).score == 0.0

    monkeypatch.setattr(
        "benchmarks.grading.verifiers.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="verifier failed"),
    )
    assert checker(context).score == 0.0

    monkeypatch.setattr(
        "benchmarks.grading.verifiers.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="not-json", stderr=""),
    )
    assert checker(context).score == 0.0
    monkeypatch.setattr(
        "benchmarks.grading.verifiers.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="x" * 20_001, stderr=""),
    )
    assert checker(context).score == 0.0


def test_repeat_aggregation_is_equal_weight_per_task() -> None:
    results = [
        _result("a", "terminal", 1.0, 1, True),
        _result("a", "terminal", 0.0, 2, False),
        _result("b", "tool_workflow", 1.0, 1, True),
        _result("b", "tool_workflow", 1.0, 2, True),
    ]
    summary = summarize(results)
    assert summary["attempts_run"] == 4
    assert summary["tasks_run"] == 2
    assert summary["overall_score"] == 0.75
    report = build_report(results)
    assert report["tasks"][0]["attempts"] == 2
    assert report["tasks"][0]["passes"] == 1
    assert report["tasks"][0]["pass_rate"] == 0.5
    assert [run["attempt"] for run in report["runs"]] == [1, 2, 1, 2]
