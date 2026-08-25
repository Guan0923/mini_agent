import { describe, expect, it } from "vitest";
import type { Conversation, RuntimeStateNode } from "../types";
import { integrateRuntimeNodeFrame, messagesBeforeRewind, projectTurnPath } from "./runtimeDetailProjection";

function turn(overrides: Partial<RuntimeStateNode> = {}): RuntimeStateNode {
  return {
    thread_id: "session_1",
    parent_thread_id: "",
    session_id: "session_1",
    parent_session_id: "",
    id: "turn_1",
    parent_id: "",
    version: "0.0.1",
    firstKeptItemSize: 8,
    compactionId: "turn_1",
    user: "user_1",
    provider_name: "local",
    model: { reasoning_effort: "medium", current_model: "deterministic", context_length: 4096, output_length: 512, thinking: "enable", temperature: 0 },
    permission_mode: "read_only",
    running_mode: "agent",
    usage: { input_tokens: 1, cached_tokens: 0, output_tokens: 1, reasoning_tokens: 0, total_tokens: 2 },
    cwd: "C:\\workspace",
    timestamp: "2026-08-25T00:00:00+00:00",
    status: "success",
    current_data_idx: 0,
    data: [[
      { role: "user", content: [{ type: "text", text: "hello" }] },
      { role: "assistant", content: [{ type: "text", text: "world" }] },
    ]],
    ...overrides,
  };
}

describe("Turn protocol projection", () => {
  it("replaces full snapshots by Turn id", () => {
    const conversation: Conversation = { id: "session_1", title: "x", messages: [], runtimeNodes: [] };
    const created = integrateRuntimeNodeFrame(conversation, { type: "turn.create", turn: turn({ status: "running" }) });
    const updated = integrateRuntimeNodeFrame(created, {
      type: "turn.update",
      turn: turn({ data: [[{ role: "user", content: [{ type: "text", text: "hello" }] }, { role: "assistant", content: [{ type: "text", text: "done" }] }]] }),
    });
    expect(updated.runtimeNodes).toHaveLength(1);
    expect(updated.messages.map((message) => message.content)).toEqual(["hello", "done"]);
  });

  it("projects assistant Items without grouping or reordering repeated types", () => {
    const items = [
      { type: "reasoning", text: "思考一" },
      { type: "tool_call", call_id: "call-1", name: "read_file", arguments: {} },
      { type: "tool_result", call_id: "call-1", tool: "read_file", content: "ok" },
      { type: "text", text: "回答一" },
      { type: "reasoning", text: "思考二" },
      { type: "text", text: "回答二" },
    ];
    const projected = projectTurnPath(new Map([["session_1:turn_1", turn({
      data: [[
        { role: "user", content: [{ type: "text", text: "hello" }] },
        { role: "assistant", content: items },
      ]],
    })]]), "turn_1");

    expect(projected[1].items).toEqual(items);
    expect(projected[1].content).toBe("回答一回答二");
  });

  it("keeps Skill selection metadata out of assistant presentation", () => {
    const projected = projectTurnPath(new Map([["session_1:turn_1", turn({
      status: "running",
      data: [[
        { role: "user", content: [{ type: "text", text: "hello" }] },
        { role: "assistant", content: [{ type: "skill_snapshot", event: "skills_selected", text: "none", skills: [] }] },
      ]],
    })]]), "turn_1");

    expect(projected[1].items).toEqual([]);
    expect(projected[1].content).toBe("");
    expect(projected[1].events).toContainEqual(expect.objectContaining({ kind: "skill_snapshot", message: "none" }));
  });

  it("folds persisted tool approval lifecycle Items into one allowed status", () => {
    const items = [
      { type: "tool_call", call_id: "call-search", name: "web_search", arguments: { query: "local" } },
      { type: "approval", event: "approval_requested", call_id: "call-search", tool: "web_search", text: "Call tool web_search?" },
      { type: "approval", event: "decision_requested", decision_id: "dec-search", kind: "tool", tool: "web_search", text: "Call tool web_search?" },
      { type: "approval", event: "approval_granted", call_id: "call-search", tool: "web_search", text: "Call tool web_search?" },
      { type: "tool_result", call_id: "call-search", tool: "web_search", content: "local result", status: "succeeded" },
      { type: "text", text: "done" },
    ];
    const projected = projectTurnPath(new Map([["session_1:turn_1", turn({
      data: [[
        { role: "user", content: [{ type: "text", text: "hello" }] },
        { role: "assistant", content: items },
      ]],
    })]]), "turn_1");

    expect(projected[1].items?.map((item) => item.type)).toEqual(["tool_call", "approval", "tool_result", "text"]);
    expect(projected[1].items?.[1]).toMatchObject({
      event: "approval_resolved",
      approval_status: "allowed",
      call_id: "call-search",
      tool: "web_search",
    });
    expect(projected[1].decision).toBeUndefined();
  });

  it("projects one pending card and derives a denied approval from its tool result", () => {
    const pendingItems = [
      { type: "tool_call", call_id: "call-search", name: "web_search", arguments: { query: "local" } },
      { type: "approval", event: "decision_requested", decision_id: "dec-search", kind: "tool", call_id: "call-search", tool: "web_search", text: "Call tool web_search?" },
    ];
    const pending = projectTurnPath(new Map([["session_1:turn_1", turn({
      status: "running",
      data: [[
        { role: "user", content: [{ type: "text", text: "hello" }] },
        { role: "assistant", content: pendingItems },
      ]],
    })]]), "turn_1");
    expect(pending[1].items?.filter((item) => item.type === "approval")).toHaveLength(1);
    expect(pending[1].decision).toMatchObject({ decision_id: "dec-search", tool: "web_search" });

    const denied = projectTurnPath(new Map([["session_1:turn_1", turn({
      data: [[
        { role: "user", content: [{ type: "text", text: "hello" }] },
        { role: "assistant", content: [
          ...pendingItems,
          { type: "tool_result", call_id: "call-search", tool: "web_search", content: "denied", status: "failed", failure_code: "user_denied" },
        ] },
      ]],
    })]]), "turn_1");
    expect(denied[1].items?.[1]).toMatchObject({ event: "approval_resolved", approval_status: "denied" });
    expect(denied[1].decision).toBeUndefined();
  });

  it("switches an ancestor version without hiding its descendant", () => {
    const parent = turn({
      current_data_idx: 1,
      data: [
        [{ role: "user", content: [{ type: "text", text: "v1" }] }, { role: "assistant", content: [{ type: "text", text: "a1" }] }],
        [{ role: "user", content: [{ type: "text", text: "v2" }] }, { role: "assistant", content: [{ type: "text", text: "a2" }] }],
      ],
    });
    const child = turn({ id: "turn_2", parent_id: "turn_1", parent_session_id: "session_1", parent_thread_id: "session_1", compactionId: "turn_1", data: [[{ role: "user", content: [{ type: "text", text: "child" }] }, { role: "assistant", content: [{ type: "text", text: "answer" }] }]] });
    const map = new Map([["session_1:turn_1", parent], ["session_1:turn_2", child]]);
    expect(projectTurnPath(map, "turn_2").map((message) => message.content)).toEqual(["v2", "a2", "child", "answer"]);
    parent.current_data_idx = 0;
    expect(projectTurnPath(map, "turn_2").map((message) => message.content)).toEqual(["v1", "a1", "child", "answer"]);
  });

  it("hides compact copied context and exposes only the indicator plus new output", () => {
    const compact = turn({
      id: "turn_compact",
      parent_id: "turn_1",
      parent_session_id: "session_1",
      parent_thread_id: "session_1",
      compactionId: "turn_compact",
      data: [[
        { role: "user", content: [{ type: "text", text: "copied user" }] },
        { role: "assistant", content: [
          { type: "compaction", summary: "summary", kept_item_count: 1 },
          { type: "text", text: "kept" },
          { type: "text", text: "new output" },
        ] },
      ]],
    });
    const projected = projectTurnPath(new Map([["session_1:turn_1", turn()], ["session_1:turn_compact", compact]]), compact.id);
    expect(projected.map((message) => message.content)).toEqual(["hello", "world", "new output"]);
    expect(projected[projected.length - 1]?.events[0]).toMatchObject({ kind: "compaction", message: "上下文已压缩" });
    expect(projected[projected.length - 1]?.items).toEqual([{ type: "text", text: "new output" }]);
    expect(projected[projected.length - 1]?.compactionNotice).toBe(true);
  });

  it("hides a rewound Turn and its descendants only on submission", () => {
    const messages = [
      { id: "u1", role: "user" as const, content: "one", events: [], nodeId: "turn_1" },
      { id: "a1", role: "assistant" as const, content: "answer one", events: [], sourceNodeId: "turn_1" },
      { id: "u2", role: "user" as const, content: "two", events: [], nodeId: "turn_2" },
      { id: "a2", role: "assistant" as const, content: "answer two", events: [], sourceNodeId: "turn_2" },
    ];

    expect(messagesBeforeRewind(messages, "turn_2")).toEqual(messages.slice(0, 2));
    expect(messagesBeforeRewind(messages, "missing")).toBe(messages);
  });
});
