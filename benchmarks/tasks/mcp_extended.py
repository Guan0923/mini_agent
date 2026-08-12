"""MCP edge-case benchmark tasks."""

from __future__ import annotations

from ..grading.programmatic import content_contains, files_exist, status_completed, tool_used
from ..model import BenchmarkTask, Budgets, Seed, SeedFile, SeedMcp

TASKS = (
    BenchmarkTask(
        name="mcp-missing-sku",
        description="Preserve an MCP unknown-item response instead of inventing a price.",
        capability="mcp",
        prompt=(
            "Use mcp_bench_demo_inventory_lookup to look up the missing SKU D4, then write the exact "
            "response to notes/missing.txt. Do not invent a price."
        ),
        seed=Seed(
            files=(SeedFile("inventory.csv", "SKU,item,price\nA1,bolt,10\n"),),
            mcp=SeedMcp(server_name="bench_demo", tools=("inventory_lookup",)),
        ),
        checkers=(
            status_completed,
            files_exist("notes/missing.txt"),
            content_contains("notes/missing.txt", "UNKNOWN:D4"),
            tool_used("mcp_bench_demo_inventory_lookup"),
        ),
        budgets=Budgets(max_tool_calls=16),
        planner_modes=frozenset({"llm"}),
    ),
)
