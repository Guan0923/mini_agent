import { describe, expect, it } from "vitest";
import type { ChatMessage, RuntimeNodeFrame, RuntimeStateNode } from "../types";
import {
  appendLegacyRuntimeEvent,
  integrateRuntimeNodeFrame,
  projectRuntimeNode,
} from "./runtimeDetailProjection";

function node(
  id: string,
  parentId: string,
  message: Record<string, unknown>,
  status: RuntimeStateNode["status"] = "success",
): RuntimeStateNode {
  return {
    session_id: "s",
    parent_session_id: parentId ? "s" : "",
    id,
    parent_id: parentId,
    version: "0.2.0",
    firstKeptEntryId: id,
    compactionIdx: id,
    user: "",
    provider: "",
    cwd: "",
    timestamp: `2026-01-01T00:00:0${id.length}+00:00`,
    status,
    data: { type: "message", message },
  };
}

function conversation(): { id: string; title: string; messages: ChatMessage[]; runtimeNodes: RuntimeStateNode[] } {
  return {
    id: "c",
    title: "会话",
    messages: [
      { id: "u", role: "user", content: "问题", events: [] },
      { id: "a", role: "assistant", content: "", events: [], running: true },
    ],
    runtimeNodes: [],
  };
}

describe("runtime detail projection", () => {
  it("projects reasoning, tool calls, results, and text from one node", () => {
    const projected = projectRuntimeNode(node("assistant", "user", {
      role: "assistant",
      run_id: "run-1",
      content: [
        { type: "reasoning", text: "先分析" },
        { type: "tool_call", name: "search", call_id: "call-1", arguments: { q: "x" } },
        { type: "tool_result", tool: "search", call_id: "call-1", content: "结果" },
        { type: "text", text: "答案" },
      ],
    }));
    expect(projected?.content).toBe("答案");
    expect(projected?.runId).toBe("run-1");
    expect(projected?.events.map((event) => event.kind)).toEqual(["thinking", "tool_call", "tool_result"]);
  });

  it("replaces an update instead of duplicating events", () => {
    const first = node("assistant", "user", {
      role: "assistant",
      content: [{ type: "reasoning", text: "旧" }],
    });
    const updated = node("assistant", "user", {
      role: "assistant",
      content: [{ type: "reasoning", text: "新" }, { type: "text", text: "完成" }],
    });
    let current = integrateRuntimeNodeFrame(conversation(), { type: "node.create", node: first });
    current = integrateRuntimeNodeFrame(current, { type: "node.update", node: updated });
    expect(current.messages[1].content).toBe("完成");
    expect(current.messages[1].events.map((event) => event.message)).toEqual(["新"]);
    expect(current.messages[1].runtimeNodeIds).toEqual(["s:assistant"]);
  });

  it("treats an empty assistant update as authoritative", () => {
    const first = node("assistant", "user", {
      role: "assistant",
      content: [{ type: "reasoning", text: "临时思考" }, { type: "text", text: "临时答案" }],
    });
    const cleared = node("assistant", "user", { role: "assistant", content: [] });
    let current = integrateRuntimeNodeFrame(conversation(), { type: "node.create", node: first });
    current = integrateRuntimeNodeFrame(current, { type: "node.update", node: cleared });
    expect(current.messages[1].content).toBe("");
    expect(current.messages[1].events).toEqual([]);
  });

  it("keeps legacy thinking open until the explicit end event", () => {
    const initial: ChatMessage = { id: "a", role: "assistant", content: "", events: [], running: true };
    const started = appendLegacyRuntimeEvent(initial, { kind: "thinking_start", message: "", data: {} });
    const delta = appendLegacyRuntimeEvent(started, { kind: "thinking_delta", message: "持续", data: {} });
    const ended = appendLegacyRuntimeEvent(delta, { kind: "thinking_end", message: "", data: {} });
    expect(ended.events[0]).toMatchObject({ kind: "thinking", message: "持续", data: { streaming: false } });
  });
});
