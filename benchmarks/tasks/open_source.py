"""Nine deterministic tasks adapted from open agent benchmarks."""

from __future__ import annotations

from ..grading.verifiers import python_verifier
from ..model import BenchmarkTask, Budgets, Seed, SeedMcp, SourceMetadata


def _source(
    benchmark: str,
    task_id: str,
    url: str,
    revision: str,
    license_name: str,
    notes: str,
) -> SourceMetadata:
    return SourceMetadata(benchmark, task_id, url, revision, license_name, notes)


TERMINAL_SOURCE = "https://github.com/harbor-framework/terminal-bench/tree/main/original-tasks"
SWE_SOURCE = "https://huggingface.co/datasets/SWE-bench/SWE-bench_Lite/viewer/default/test"
TAU_SOURCE = "https://github.com/sierra-research/tau2-bench/blob/main/data/tau2/domains"


TASKS = (
    BenchmarkTask(
        name="terminal-analyze-access-log",
        description="Analyze a realistic web access log and write a deterministic summary report.",
        capability="terminal",
        difficulty="easy",
        source=_source(
            "Terminal-Bench",
            "analyze-access-logs",
            f"{TERMINAL_SOURCE}/analyze-access-logs",
            "terminal-bench-core-0.1.1",
            "Apache-2.0",
            "Vendored the small log fixture and retained the report contract; the Docker wrapper was removed.",
        ),
        prompt=(
            "Analyze access_log in the workspace and create report.txt. It must contain exactly these lines: "
            "Total requests: <number>, Unique IP addresses: <number>, Top 3 URLs:, three indented URL count "
            "lines, and 404 errors: <number>. Compute the values from the log rather than guessing, and keep "
            "the report deterministic."
        ),
        seed=Seed(fixture="terminal-analyze-access-log"),
        checkers=(python_verifier("terminal_access_log"),),
        budgets=Budgets(max_model_turns=8, max_tool_calls=20),
        tags=("terminal", "data-analysis", "text-processing"),
    ),
    BenchmarkTask(
        name="terminal-cancel-async-tasks",
        description="Implement bounded async execution with cancellation-safe cleanup.",
        capability="terminal",
        difficulty="hard",
        source=_source(
            "Terminal-Bench",
            "cancel-async-tasks",
            f"{TERMINAL_SOURCE}/cancel-async-tasks",
            "terminal-bench-core-0.1.1",
            "Apache-2.0",
            "Reduced the original system task to a platform-neutral Python module and hidden async verifier.",
        ),
        prompt=(
            "Create run.py with async run_tasks(tasks: list[Callable[[], Awaitable[None]]], max_concurrent: int) "
            "-> None. Run jobs with the requested concurrency limit, propagate failures, and make sure cancelling "
            "the parent run cancels and awaits every child so each job's finally cleanup executes. Use only the "
            "standard library."
        ),
        seed=Seed(fixture="terminal-cancel-async-tasks"),
        checkers=(python_verifier("terminal_cancel_async", timeout_seconds=12),),
        budgets=Budgets(max_model_turns=12, max_tool_calls=32, max_replans=3),
        tags=("terminal", "software-engineering", "async", "concurrency"),
    ),
    BenchmarkTask(
        name="terminal-countdown-462",
        description="Construct a safe exact arithmetic expression for a countdown puzzle.",
        capability="terminal",
        difficulty="easy",
        source=_source(
            "Terminal-Bench",
            "countdown-game",
            f"{TERMINAL_SOURCE}/countdown-game",
            "terminal-bench-core-0.1.1",
            "Apache-2.0",
            "Kept the arithmetic contract and changed /app/output.txt to workspace-relative output.txt.",
        ),
        prompt=(
            "Using [3, 8, 11, 19, 25, 75], construct an arithmetic expression that evaluates exactly to 462. "
            "Use each number at most once and only +, -, *, / and parentheses. Write only the expression on one "
            "line in output.txt; do not add an explanation or an equals sign."
        ),
        seed=Seed(fixture="terminal-countdown-462"),
        checkers=(python_verifier("terminal_countdown"),),
        budgets=Budgets(max_model_turns=6, max_tool_calls=16),
        tags=("terminal", "mathematics", "safe-evaluation"),
    ),
    BenchmarkTask(
        name="swe-requests-2317",
        description="Fix a bytes HTTP method normalization regression in a minimal Requests fixture.",
        capability="software_engineering",
        difficulty="easy",
        source=_source(
            "SWE-bench Lite",
            "psf__requests-2317",
            SWE_SOURCE,
            "091991be0da19de9108dbe5e3752917fea3d7fdc",
            "Apache-2.0 (Requests); MIT (SWE-bench data/harness)",
            "Vendored only the relevant method-preparation path and replaced the network regression test with a hidden local test.",
        ),
        prompt=(
            "Inspect the minimal Requests fixture under requests_like and fix the reported bytes-method bug: "
            "preparing b'GET' must produce the native method GET rather than the literal text \"b'GET'\". "
            "Keep ordinary string methods working and do not add dependencies."
        ),
        seed=Seed(fixture="swe-requests-2317"),
        checkers=(python_verifier("swe_requests"),),
        budgets=Budgets(max_model_turns=10, max_tool_calls=28, max_replans=3),
        tags=("swe", "requests", "regression"),
    ),
    BenchmarkTask(
        name="swe-pytest-11143",
        description="Fix numeric first-expression handling in an assertion-rewrite fixture.",
        capability="software_engineering",
        difficulty="medium",
        source=_source(
            "SWE-bench Lite",
            "pytest-dev__pytest-11143",
            SWE_SOURCE,
            "6995257cf470d2143ad1683824962de4071c0eb7",
            "MIT (Pytest); MIT (SWE-bench data/harness)",
            "Vendored the affected AST classification and a local assertion-rewrite contract instead of the full Pytest repository.",
        ),
        prompt=(
            "Fix the minimal assertion-rewrite fixture under mini_pytest. A module whose first statement is a "
            "numeric expression must not be classified as a docstring, while a real string docstring must still "
            "be recognized. Preserve the assertion rewrite behavior represented by rewrite_assertions()."
        ),
        seed=Seed(fixture="swe-pytest-11143"),
        checkers=(python_verifier("swe_pytest"),),
        budgets=Budgets(max_model_turns=10, max_tool_calls=28, max_replans=3),
        tags=("swe", "pytest", "ast", "regression"),
    ),
    BenchmarkTask(
        name="swe-astropy-14365",
        description="Make QDP command parsing case-insensitive without treating data rows as commands.",
        capability="software_engineering",
        difficulty="medium",
        source=_source(
            "SWE-bench Lite",
            "astropy__astropy-14365",
            SWE_SOURCE,
            "7269fa3e33e8d02485a647da91a5a2a60a06af61",
            "BSD-3-Clause (Astropy); MIT (SWE-bench data/harness)",
            "Vendored the relevant QDP parser path and a local mixed-case regression fixture without Astropy dependencies.",
        ),
        prompt=(
            "Fix mini_astropy/qdp.py so QDP commands such as read serr 1 2 are accepted case-insensitively, "
            "while command names are normalized and numeric data rows remain data rather than commands. Keep "
            "existing uppercase behavior unchanged."
        ),
        seed=Seed(fixture="swe-astropy-14365"),
        checkers=(python_verifier("swe_astropy"),),
        budgets=Budgets(max_model_turns=10, max_tool_calls=28, max_replans=3),
        tags=("swe", "astropy", "parser", "regression"),
    ),
    BenchmarkTask(
        name="tau-retail-0-exchange",
        description="Use a policy-constrained retail MCP workflow to exchange two delivered items.",
        capability="tool_workflow",
        difficulty="hard",
        source=_source(
            "τ³-bench",
            "retail/0",
            f"{TAU_SOURCE}/retail/tasks.json",
            "v1.0.1",
            "MIT",
            "Flattened the user simulator into one authorized request and kept the retail policy, tool trace, and database state.",
        ),
        prompt=(
            "Read policy.md, then use the retail MCP tools. You are Yusuf Rossi in ZIP 19122. For delivered order "
            "#W2378156, exchange the mechanical keyboard for the clicky, full-size, RGB version and exchange the "
            "Apple HomeKit thermostat for the Google Home-compatible version. Verify the user, order, and product "
            "details before the mutation. The user explicitly authorizes this single exchange; use the order's "
            "payment method and do not mutate anything else."
        ),
        seed=Seed(
            fixture="tau-retail-0-exchange",
            mcp=SeedMcp(
                server_name="tau3_retail",
                profile="retail",
                tools=(
                    "find_user_id_by_name_zip",
                    "get_user_details",
                    "get_order_details",
                    "get_product_details",
                    "exchange_delivered_order_items",
                ),
            ),
        ),
        checkers=(python_verifier("tau_retail_exchange"),),
        budgets=Budgets(max_model_turns=14, max_tool_calls=40, max_replans=3),
        tags=("tau3", "mcp", "retail", "stateful"),
    ),
    BenchmarkTask(
        name="tau-retail-113-cancel-all",
        description="Use a retail MCP workflow to cancel every pending order without touching delivered orders.",
        capability="tool_workflow",
        difficulty="medium",
        source=_source(
            "τ³-bench",
            "retail/113",
            f"{TAU_SOURCE}/retail/tasks.json",
            "v1.0.1",
            "MIT",
            "Flattened the user simulator and supplied explicit authorization/reason while preserving pending-order policy and state checks.",
        ),
        prompt=(
            "Read policy.md and use the retail MCP tools. You are Yara Muller in ZIP 85041 and explicitly want "
            "every pending order cancelled because the items were ordered by mistake. Verify the customer, find "
            "all of that customer's pending orders, cancel only those orders, and do not touch delivered orders."
        ),
        seed=Seed(
            fixture="tau-retail-113-cancel-all",
            mcp=SeedMcp(
                server_name="tau3_retail",
                profile="retail",
                tools=(
                    "find_user_id_by_name_zip",
                    "get_user_details",
                    "list_pending_orders",
                    "cancel_pending_order",
                ),
            ),
        ),
        checkers=(python_verifier("tau_retail_cancel_all"),),
        budgets=Budgets(max_model_turns=12, max_tool_calls=32, max_replans=3),
        tags=("tau3", "mcp", "retail", "policy"),
    ),
    BenchmarkTask(
        name="tau-airline-3-baggage",
        description="Verify airline membership and reservation data before answering a baggage question.",
        capability="tool_workflow",
        difficulty="medium",
        source=_source(
            "τ³-bench",
            "airline/3",
            f"{TAU_SOURCE}/airline/tasks.json",
            "v1.0.1",
            "MIT",
            "Flattened the user simulator into a read-only request while retaining membership verification and communication criteria.",
        ),
        prompt=(
            "Read policy.md and use the airline MCP tools to answer the question. You are Anya Garcia with user ID "
            "anya_garcia_5901 and reservation JMO1MG. Verify the reservation cabin, passenger count, and actual "
            "membership level before answering how many suitcases are allowed. This is read-only: do not change any "
            "reservation. State the answer clearly in your final response."
        ),
        seed=Seed(
            fixture="tau-airline-3-baggage",
            mcp=SeedMcp(
                server_name="tau3_airline",
                profile="airline",
                tools=("get_user_details", "get_reservation_details"),
            ),
        ),
        checkers=(python_verifier("tau_airline_baggage"),),
        budgets=Budgets(max_model_turns=10, max_tool_calls=24, max_replans=2),
        tags=("tau3", "mcp", "airline", "read-only"),
    ),
)


__all__ = ["TASKS"]
