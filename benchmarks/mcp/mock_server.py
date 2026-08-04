"""Profile-driven local MCP state machine for the τ³-bench adaptations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("retail", "airline"), required=True)
    parser.add_argument("--tools", nargs="*", default=None)
    return parser.parse_args()


mcp = FastMCP("tau3-benchmark")
_state_path = Path("retail_state.json")
_audit_path = Path("mcp_audit.jsonl")


def _load() -> dict[str, Any]:
    if not _state_path.exists():
        return {}
    with _state_path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    return value if isinstance(value, dict) else {}


def _save(state: dict[str, Any]) -> None:
    temporary = _state_path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(_state_path)


def _record(name: str, arguments: dict[str, Any], *, mutating: bool) -> None:
    with _audit_path.open("a", encoding="utf-8", newline="\n") as handle:
        json.dump({"name": name, "arguments": arguments, "mutating": mutating}, handle, ensure_ascii=False)
        handle.write("\n")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def find_user_id_by_name_zip(first_name: str, last_name: str, zip: str) -> str:
    arguments = {"first_name": first_name, "last_name": last_name, "zip": zip}
    _record("find_user_id_by_name_zip", arguments, mutating=False)
    for user_id, user in _load().get("users", {}).items():
        if (
            user.get("first_name") == first_name
            and user.get("last_name") == last_name
            and str(user.get("zip", "")) == str(zip)
        ):
            return _json({"user_id": user_id})
    return _json({"error": "user_not_found"})


def get_user_details(user_id: str) -> str:
    arguments = {"user_id": user_id}
    _record("get_user_details", arguments, mutating=False)
    user = _load().get("users", {}).get(user_id)
    return _json(user if isinstance(user, dict) else {"error": "user_not_found"})


def get_order_details(order_id: str) -> str:
    arguments = {"order_id": order_id}
    _record("get_order_details", arguments, mutating=False)
    order = _load().get("orders", {}).get(order_id)
    return _json(order if isinstance(order, dict) else {"error": "order_not_found"})


def list_pending_orders(user_id: str) -> str:
    arguments = {"user_id": user_id}
    _record("list_pending_orders", arguments, mutating=False)
    orders = _load().get("orders", {})
    result = [
        order_id
        for order_id, order in orders.items()
        if isinstance(order, dict) and order.get("user_id") == user_id and order.get("status") == "pending"
    ]
    return _json({"order_ids": sorted(result)})


def get_product_details(product_id: str) -> str:
    arguments = {"product_id": product_id}
    _record("get_product_details", arguments, mutating=False)
    product = _load().get("products", {}).get(product_id)
    return _json(product if isinstance(product, dict) else {"error": "product_not_found"})


def exchange_delivered_order_items(
    order_id: str,
    item_ids: list[str],
    new_item_ids: list[str],
    payment_method_id: str,
) -> str:
    arguments = {
        "order_id": order_id,
        "item_ids": item_ids,
        "new_item_ids": new_item_ids,
        "payment_method_id": payment_method_id,
    }
    _record("exchange_delivered_order_items", arguments, mutating=True)
    state = _load()
    order = state.get("orders", {}).get(order_id)
    products = state.get("products", {})
    if not isinstance(order, dict) or order.get("status") != "delivered":
        return _json({"error": "order_not_delivered"})
    if order.get("items") != item_ids or len(new_item_ids) != len(item_ids):
        return _json({"error": "item_mismatch"})
    if payment_method_id != order.get("payment_method_id"):
        return _json({"error": "payment_method_mismatch"})
    if any(item_id not in products for item_id in new_item_ids):
        return _json({"error": "replacement_not_found"})
    order["items"] = list(new_item_ids)
    order["payment_method_id"] = payment_method_id
    _save(state)
    return _json({"status": "exchanged", "order_id": order_id, "items": new_item_ids})


def cancel_pending_order(order_id: str, reason: str) -> str:
    arguments = {"order_id": order_id, "reason": reason}
    _record("cancel_pending_order", arguments, mutating=True)
    state = _load()
    order = state.get("orders", {}).get(order_id)
    if not isinstance(order, dict) or order.get("status") != "pending":
        return _json({"error": "order_not_pending"})
    order["status"] = "cancelled"
    order["cancellation_reason"] = reason
    _save(state)
    return _json({"status": "cancelled", "order_id": order_id})


def get_reservation_details(reservation_id: str) -> str:
    arguments = {"reservation_id": reservation_id}
    _record("get_reservation_details", arguments, mutating=False)
    reservation = _load().get("reservations", {}).get(reservation_id)
    return _json(reservation if isinstance(reservation, dict) else {"error": "reservation_not_found"})


def _configure_server(profile: str, tools: list[str] | None = None) -> None:
    """Select a profile and register only the tools required by that task."""
    global _state_path, mcp
    if profile not in {"retail", "airline"}:
        raise ValueError(f"unknown MCP profile: {profile}")
    _state_path = Path("retail_state.json" if profile == "retail" else "airline_state.json")
    mcp = FastMCP(f"tau3-{profile}")
    available = (
        {
            tool.__name__: tool
            for tool in (
                find_user_id_by_name_zip,
                get_user_details,
                get_order_details,
                list_pending_orders,
                get_product_details,
                exchange_delivered_order_items,
                cancel_pending_order,
            )
        }
        if profile == "retail"
        else {tool.__name__: tool for tool in (get_user_details, get_reservation_details)}
    )
    requested = set(tools) if tools else set(available)
    unknown = sorted(requested - set(available))
    if unknown:
        raise ValueError(f"unknown tool(s) for {profile} profile: {', '.join(unknown)}")
    for name in sorted(requested):
        mcp.tool()(available[name])


def main() -> int:
    args = _parse_args()
    try:
        _configure_server(args.profile, args.tools)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
