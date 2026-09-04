"""Reconcile committed Todo receipts before generic tool-call recovery."""

from __future__ import annotations

import json

from backend.domain import RuntimeStateNode, TodoListStore


def reconcile_todo_receipts(store, todo_store: TodoListStore | None, node: RuntimeStateNode) -> RuntimeStateNode:
    if todo_store is None:
        return node
    messages = node.data[node.current_data_idx]
    terminal_ids = {
        str(item.get("call_id"))
        for message in messages
        for item in message.get("content", [])
        if item.get("type") == "tool_result" and item.get("call_id")
    }
    recovered: dict[str, str] = {}
    for message in messages:
        content = message.get("content", [])
        for item in content:
            call_id = str(item.get("call_id") or "")
            if (
                item.get("type") != "tool_call"
                or item.get("name") != "update_todo_list"
                or item.get("status") != "running"
                or not call_id
                or call_id in terminal_ids
            ):
                continue
            receipt = todo_store.receipt(node.session_id, node.id, call_id)
            if receipt is None:
                continue
            result = json.dumps(receipt.to_dict(), ensure_ascii=False, separators=(",", ":"))
            item["status"] = "success"
            item["replay_safe"] = False
            content.append(
                {
                    "type": "tool_result",
                    "call_id": call_id,
                    "tool": "update_todo_list",
                    "content": result,
                    "status": "success",
                    "replay_safe": False,
                    "retryable": True,
                }
            )
            terminal_ids.add(call_id)
            recovered[call_id] = result
    if not recovered:
        return node

    repaired = RuntimeStateNode.from_dict(node.to_dict())
    store.update_node(repaired)
    runtime = store.load_runtime(node.session_id)
    if runtime is not None and runtime.current_run is not None and runtime.current_run.turn_id == node.id:
        candidates = []
        if runtime.active_message is not None:
            candidates.extend(runtime.active_message.tool_messages)
        candidates.extend(runtime.current_run.actions)
        for tool in candidates:
            result = recovered.get(tool.call_id)
            if result is not None:
                tool.status = "succeeded"
                tool.content = result
                tool.retryable = True
        store.save_runtime(runtime)
    return repaired


__all__ = ["reconcile_todo_receipts"]
