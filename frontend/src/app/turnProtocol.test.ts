import { describe, expect, it } from "vitest";
import type { Conversation, RuntimeNodeFrame, RuntimeRootNode, RuntimeStateNode } from "../types";
import { integrateRuntimeNodeUpdates, messagesBeforeRewind, projectTurnPath, pruneTurnDescendants } from "./runtimeDetailProjection";
import { applyRuntimeNodeFrame, runtimeNodeAccumulator } from "./runtimeNodeReducer";
import { isRuntimeRootNode, normalizeRuntimeNode } from "./runtimeNodeNormalization";

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
  it("accepts the strict three-field root and skips it during projection", () => {
    const root: RuntimeRootNode = { session_id: "session_1", thread_id: "session_1", id: "turn_root" };
    expect(isRuntimeRootNode(normalizeRuntimeNode(root))).toBe(true);
    expect(() => normalizeRuntimeNode({ ...root, status: "success" } as unknown as RuntimeRootNode)).toThrow("Unsupported Turn version");

    const first = turn({ parent_id: root.id, parent_session_id: root.session_id, parent_thread_id: root.thread_id });
    const fork = turn({
      id: "turn_fork",
      thread_id: "thread_fork",
      parent_id: root.id,
      parent_session_id: root.session_id,
      parent_thread_id: root.thread_id,
      data: [[
        { role: "user", content: [{ type: "text", text: "forked" }] },
        { role: "assistant", content: [{ type: "text", text: "branch" }] },
      ]],
    });
    const map = new Map([
      ["session_1:turn_root", root],
      ["session_1:turn_1", first],
      ["session_1:turn_fork", fork],
    ]);
    expect(projectTurnPath(map, first.id).map((message) => message.content)).toEqual(["hello", "world"]);
    expect(projectTurnPath(map, fork.id).map((message) => message.content)).toEqual(["forked", "branch"]);
    expect(projectTurnPath(map, root.id)).toEqual([]);
  });

  it("reconstructs the shared root when the first SSE snapshot only contains a Turn", () => {
    const conversation: Conversation = { id: "session_1", title: "x", messages: [], runtimeNodes: [] };
    const first = turn({
      parent_id: "turn_root",
      parent_session_id: "session_1",
      parent_thread_id: "session_1",
    });

    const created = integrateRuntimeNodeUpdates(conversation, [first], first.id, true);

    expect(created.runtimeNodes).toEqual([
      { session_id: "session_1", thread_id: "session_1", id: "turn_root" },
      first,
    ]);
    expect(created.messages.map((message) => message.content)).toEqual(["hello", "world"]);
    expect(created.activeTurnId).toBe(first.id);
  });

  it("still rejects a missing non-root ancestor", () => {
    const root: RuntimeRootNode = { session_id: "session_1", thread_id: "session_1", id: "turn_root" };
    const first = turn({
      parent_id: root.id,
      parent_session_id: root.session_id,
      parent_thread_id: root.thread_id,
    });
    const conversation: Conversation = {
      id: "session_1",
      title: "x",
      messages: [],
      runtimeNodes: [root, first],
    };
    const child = turn({
      id: "turn_child",
      parent_id: "turn_missing",
      parent_session_id: "session_1",
      parent_thread_id: "session_1",
    });

    expect(() => integrateRuntimeNodeUpdates(conversation, [child], child.id, true)).toThrow(
      "Turn ancestry is incomplete",
    );
  });

  it("applies exact text deltas without rebuilding the existing user message", () => {
    const conversation: Conversation = { id: "session_1", title: "x", messages: [], runtimeNodes: [] };
    const accumulator = runtimeNodeAccumulator();
    const baseline = applyRuntimeNodeFrame(accumulator, {
      type: "turn.snapshot",
      revision: 0,
      turn: turn({
        status: "running",
        data: [[{ role: "user", content: [{ type: "text", text: "hello" }] }, { role: "assistant", content: [{ type: "text", text: "" }] }]],
      }),
    });
    const created = integrateRuntimeNodeUpdates(conversation, [baseline], baseline.id, true);
    const originalUser = created.messages[0];
    const updatedTurn = applyRuntimeNodeFrame(accumulator, {
      type: "turn.delta",
      session_id: "session_1",
      turn_id: "turn_1",
      revision: 1,
      patch: { status: "success" },
      operations: [{ op: "append_text", data_idx: 0, message_idx: 1, item_idx: 0, delta: "done" }],
    });
    const updated = integrateRuntimeNodeUpdates(created, [updatedTurn], updatedTurn.id, false);
    expect(updated.runtimeNodes).toHaveLength(1);
    expect(updated.messages.map((message) => message.content)).toEqual(["hello", "done"]);
    expect(updated.messages[0]).toBe(originalUser);
  });

  it("projects every same-Turn Message and updates the latest assistant incrementally", () => {
    const accumulator = runtimeNodeAccumulator();
    const baseline = applyRuntimeNodeFrame(accumulator, {
      type: "turn.snapshot",
      revision: 0,
      turn: turn({ status: "running" }),
    });
    let conversation: Conversation = { id: "session_1", title: "multi", messages: [], runtimeNodes: [] };
    conversation = integrateRuntimeNodeUpdates(conversation, [baseline], baseline.id, true);
    const withUser = applyRuntimeNodeFrame(accumulator, {
      type: "turn.delta",
      session_id: "session_1",
      turn_id: "turn_1",
      revision: 1,
      operations: [{
        op: "append_message",
        data_idx: 0,
        message_idx: 2,
        message: { role: "user", steering_id: "steer_1", content: [{ type: "text", text: "redirect" }] },
      }],
    });
    conversation = integrateRuntimeNodeUpdates(conversation, [withUser], withUser.id, true);
    const withAssistant = applyRuntimeNodeFrame(accumulator, {
      type: "turn.delta",
      session_id: "session_1",
      turn_id: "turn_1",
      revision: 2,
      operations: [{
        op: "append_message",
        data_idx: 0,
        message_idx: 3,
        message: { role: "assistant", content: [] },
      }],
    });
    conversation = integrateRuntimeNodeUpdates(conversation, [withAssistant], withAssistant.id, true);
    const withText = applyRuntimeNodeFrame(accumulator, {
      type: "turn.delta",
      session_id: "session_1",
      turn_id: "turn_1",
      revision: 3,
      operations: [{
        op: "append_item",
        data_idx: 0,
        message_idx: 3,
        item_idx: 0,
        item: { type: "text", text: "new answer" },
      }],
    });
    conversation = integrateRuntimeNodeUpdates(conversation, [withText], withText.id, false);

    expect(conversation.messages.map((message) => [message.role, message.content])).toEqual([
      ["user", "hello"],
      ["assistant", "world"],
      ["user", "redirect"],
      ["assistant", "new answer"],
    ]);
    expect(conversation.messages[2].timelineSource).toBe("steering");
  });

  it("rejects missing baselines, revision gaps, and forbidden patches", () => {
    const delta: RuntimeNodeFrame = {
      type: "turn.delta",
      session_id: "session_1",
      turn_id: "turn_1",
      revision: 1,
      operations: [{ op: "append_item", data_idx: 0, message_idx: 1, item_idx: 0, item: { type: "text", text: "x" } }],
    };
    expect(() => applyRuntimeNodeFrame(runtimeNodeAccumulator(), delta)).toThrow("before its baseline");

    const accumulator = runtimeNodeAccumulator();
    applyRuntimeNodeFrame(accumulator, { type: "turn.snapshot", revision: 0, turn: turn({ status: "running" }) });
    expect(() => applyRuntimeNodeFrame(accumulator, { ...delta, revision: 2 } as RuntimeNodeFrame)).toThrow("not consecutive");
    expect(() => applyRuntimeNodeFrame(accumulator, {
      ...delta,
      patch: { data: [] },
    } as unknown as RuntimeNodeFrame)).toThrow("cannot patch data");
  });

  it("rejects invalid patch values, malformed Items, and empty deltas", () => {
    const accumulator = runtimeNodeAccumulator();
    applyRuntimeNodeFrame(accumulator, { type: "turn.snapshot", revision: 0, turn: turn({ status: "running" }) });
    expect(() => applyRuntimeNodeFrame(accumulator, {
      type: "turn.delta",
      session_id: "session_1",
      turn_id: "turn_1",
      revision: 1,
      patch: { status: "invalid" },
    } as unknown as RuntimeNodeFrame)).toThrow("patch value is invalid");

    expect(() => applyRuntimeNodeFrame(accumulator, {
      type: "turn.delta",
      session_id: "session_1",
      turn_id: "turn_1",
      revision: 1,
      operations: [{ op: "append_item", data_idx: 0, message_idx: 1, item_idx: 1, item: {} }],
    } as RuntimeNodeFrame)).toThrow("item delta is invalid");

    expect(() => applyRuntimeNodeFrame(accumulator, {
      type: "turn.delta",
      session_id: "session_1",
      turn_id: "turn_1",
      revision: 1,
    })).toThrow("must contain a patch or operation");
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

  it("prunes only same-Thread descendants when rewind is submitted", () => {
    const root = turn();
    const target = turn({
      id: "turn_target",
      parent_id: root.id,
      parent_session_id: root.session_id,
      parent_thread_id: root.thread_id,
      current_data_idx: 1,
      data: [
        [{ role: "user", content: [{ type: "text", text: "target-v1" }] }, { role: "assistant", content: [{ type: "text", text: "answer-v1" }] }],
        [{ role: "user", content: [{ type: "text", text: "target-v2" }] }, { role: "assistant", content: [{ type: "text", text: "answer-v2" }] }],
      ],
    });
    const descendant = turn({
      id: "turn_descendant",
      parent_id: target.id,
      parent_session_id: target.session_id,
      parent_thread_id: target.thread_id,
    });
    const grandchild = turn({
      id: "turn_grandchild",
      parent_id: descendant.id,
      parent_session_id: descendant.session_id,
      parent_thread_id: descendant.thread_id,
    });
    const sibling = turn({
      id: "turn_sibling",
      parent_id: root.id,
      parent_session_id: root.session_id,
      parent_thread_id: root.thread_id,
    });
    const otherThread = turn({
      id: "turn_other_thread",
      thread_id: "thread_other",
      parent_thread_id: root.thread_id,
    });

    const pruned = pruneTurnDescendants(
      [root, target, descendant, grandchild, sibling, otherThread],
      target.id,
    );
    expect(pruned.map((node) => node.id)).toEqual([
      root.id,
      target.id,
      sibling.id,
      otherThread.id,
    ]);

    const map = new Map(pruned.map((node) => [`${node.session_id}:${node.id}`, node] as const));
    expect(projectTurnPath(map, target.id).map((message) => message.content)).toEqual([
      "hello",
      "world",
      "target-v2",
      "answer-v2",
    ]);
    target.current_data_idx = 0;
    expect(projectTurnPath(map, target.id).map((message) => message.content)).toEqual([
      "hello",
      "world",
      "target-v1",
      "answer-v1",
    ]);
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
