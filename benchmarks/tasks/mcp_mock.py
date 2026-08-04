"""MCP capability task: the agent must call tools on a mock MCP server."""

from __future__ import annotations

from ..grading.programmatic import content_contains, files_exist, tool_used
from ..model import BenchmarkTask, Budgets, Seed, SeedFile, SeedMcp

INVENTORY = "SKU,item,price\nA1,bolt,10\nB2,nut,5\nC3,screw,20\n"

TASKS = (
    BenchmarkTask(
        name="mcp-inventory",
        description="Use the mock MCP inventory tools to total three SKU prices.",
        capability="mcp",
        prompt=(
            "Use the mcp_bench_demo_inventory_lookup tool to look up SKU A1, B2 and C3, "
            "then use mcp_bench_demo_total_cost to sum their prices and write the total "
            "to notes/total.txt."
        ),
        seed=Seed(
            files=(
                SeedFile("inventory.csv", INVENTORY),
                SeedFile("notes/README.md", "# Inventory totals\n"),
            ),
            mcp=SeedMcp(server_name="bench_demo", tools=("inventory_lookup", "total_cost")),
        ),
        checkers=(
            files_exist("notes/total.txt"),
            content_contains("notes/total.txt", "35"),
            tool_used("mcp_bench_demo_inventory_lookup"),
            tool_used("mcp_bench_demo_total_cost"),
        ),
        budgets=Budgets(max_model_turns=8, max_tool_calls=24),
        planner_modes=frozenset({"llm"}),
    ),
)
