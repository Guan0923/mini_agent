"""A minimal, deterministic MCP server used by the benchmark MCP task.

Run over stdio by the agent's external MCP manager. It reads ``inventory.csv``
from its working directory (the benchmark task's workspace), so every task
instance sees exactly the seeded data.
"""

from __future__ import annotations

import csv
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("bench_demo")


def _prices() -> dict[str, float]:
    prices: dict[str, float] = {}
    path = Path("inventory.csv")
    if not path.exists():
        return prices
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                prices[str(row["SKU"])] = float(row["price"])
            except (KeyError, ValueError):
                continue
    return prices


@mcp.tool()
def inventory_lookup(sku: str) -> str:
    """Look up an item and its price by SKU in inventory.csv (in the working directory)."""
    price = _prices().get(sku)
    if price is None:
        return f"UNKNOWN:{sku}"
    return f"{sku}={price:.0f}"


@mcp.tool()
def total_cost(skus: list[str]) -> str:
    """Sum the prices of the given SKUs."""
    prices = _prices()
    total = sum(prices.get(sku, 0.0) for sku in skus)
    return f"{total:.0f}"


@mcp.tool()
def echo(value: str) -> str:
    """Echo the given value back."""
    return value


if __name__ == "__main__":
    mcp.run()
