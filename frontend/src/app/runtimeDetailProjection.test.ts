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
    version: "0.3.0",
    firstKeptEntryId: id,
    compactionIdx: id,
    user: "",
    provider_name: "",
    model: { reasoning_effort: "medium", current_model: "demo", context_length: 128000, output_length: 8192, thinking: "enable", temperature: 1 },
    permission_mode: "approval_for_me",
    running_mode: "agent",
    usage: { input_tokens: null, cached_tokens: null, output_tokens: null, reasoning_tokens: null, total_tokens: null },
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

  it("clears a fork anchor after the first runtime node arrives", () => {
    const current = {
      ...conversation(),
      forkAnchorNodeId: "ancestor-node",
      forkAnchorSessionId: "ancestor-session",
    };
    const next = integrateRuntimeNodeFrame(current, {
      type: "node.create",
      node: node("assistant", "user", { role: "assistant", content: [] }, "running"),
    });
    expect(next).toMatchObject({
      lastNodeId: "assistant",
      forkAnchorNodeId: undefined,
      forkAnchorSessionId: undefined,
    });
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

  it("does not surface the failed placeholder from a dynamic node", () => {
    const placeholder = node("assistant", "user", { role: "assistant", content: [] }, "abort");
    const current = integrateRuntimeNodeFrame(conversation(), { type: "node.update", node: placeholder });

    expect(current.messages[1].error).toBeUndefined();
  });

  it("uses the tool block status instead of the parent node status", () => {
    const tool = node("tool", "user", {
      role: "tool_result",
      content: [{ type: "tool_result", tool: "search", status: "succeeded", content: "ok" }],
    }, "abort");

    expect(projectRuntimeNode(tool)?.events[0]?.kind).toBe("tool_result");
  });

  it("projects user denial without a terminal error and hides other recoverable failures", () => {
    const denied = node("denied", "assistant", {
      role: "tool_result",
      content: [{
        type: "tool_result",
        tool: "write_file",
        call_id: "call-denied",
        content: "The user denied this write_file tool call.",
        status: "failed",
        failure_code: "user_denied",
      }],
    }, "abort");
    const skipped = node("skipped", "denied", {
      role: "tool_result",
      content: [{
        type: "tool_result",
        tool: "run_command",
        call_id: "call-skipped",
        content: "Not executed because tool execution was interrupted.",
        status: "failed",
        failure_code: "user_denied_batch",
      }],
    }, "abort");

    expect(projectRuntimeNode(denied)).toMatchObject({
      error: undefined,
      events: [{
        kind: "tool_failed",
        data: { tool: "write_file", call_id: "call-denied", failure_code: "user_denied" },
      }],
    });
    expect(projectRuntimeNode(skipped)).toMatchObject({ error: undefined, events: [] });
  });

  it("clears a stale error when a terminal success replaces it", () => {
    const current = conversation();
    current.messages[1].error = "An unknown error caused the system to encounter an exception.";
    const completed = node("assistant", "user", { role: "assistant", content: [{ type: "text", text: "完成" }] });

    const next = integrateRuntimeNodeFrame(current, { type: "node.delete", node: completed });

    expect(next.messages[1].error).toBeUndefined();
    expect(next.messages[1].content).toBe("完成");
  });

  it("ignores stale error metadata on a successful historical node", () => {
    const completed = node("assistant", "user", {
      role: "assistant",
      content: [{ type: "text", text: "完成" }],
      error: { message: "An unknown error caused the system to encounter an exception." },
    }, "success");

    expect(projectRuntimeNode(completed)?.error).toBeUndefined();
  });

  it("projects a structured abort reason into the assistant error alert", () => {
    const aborted = node("assistant", "user", {
      role: "assistant",
      content: [{ type: "text", text: "The run was aborted." }],
      error: {
        category: "network",
        message: "The run was aborted because a network error interrupted communication.",
        detail: "Model request timed out.",
      },
    }, "abort");

    const current = integrateRuntimeNodeFrame(conversation(), { type: "node.delete", node: aborted });

    expect(current.messages[1].error).toBe(
      "The run was aborted because a network error interrupted communication.\n\nDetails: Model request timed out.",
    );
    expect(projectRuntimeNode(aborted)?.error).toContain("network error");
  });

  it("uses the generic abort reason when a terminal node has no error metadata", () => {
    const failed = node("failed", "user", { role: "assistant", content: [] }, "abort");

    expect(projectRuntimeNode(failed)).toMatchObject({
      role: "assistant",
      content: "",
      error: "The run was aborted for an unknown reason.",
    });
  });

  it("keeps legacy thinking open until the explicit end event", () => {
    const initial: ChatMessage = { id: "a", role: "assistant", content: "", events: [], running: true };
    const started = appendLegacyRuntimeEvent(initial, { kind: "thinking_start", message: "", data: {} });
    const delta = appendLegacyRuntimeEvent(started, { kind: "thinking_delta", message: "持续", data: {} });
    const ended = appendLegacyRuntimeEvent(delta, { kind: "thinking_end", message: "", data: {} });
    expect(ended.events[0]).toMatchObject({ kind: "thinking", message: "持续", data: { streaming: false } });
  });
});
