import { afterEach, describe, expect, it, vi } from "vitest";
import { createRunController } from "./runController";
import type { Conversation, RuntimeStateNode, StreamMessage } from "../types";

const mocks = vi.hoisted(() => ({
  streamChat: vi.fn(),
  streamResume: vi.fn(),
}));

vi.mock("../api", () => mocks);

function node(content: string, status: RuntimeStateNode["status"] = "failed"): RuntimeStateNode {
  return {
    session_id: "session-1",
    parent_session_id: "session-1",
    id: "assistant-node",
    parent_id: "user-node",
    version: "0.3.0",
    firstKeptEntryId: "assistant-node",
    compactionIdx: "assistant-node",
    user: "",
    provider_name: "",
    model: { reasoning_effort: "medium", current_model: "demo", context_length: 128000, output_length: 8192, thinking: "enable", temperature: 1 },
    permission_mode: "approval_for_me",
    running_mode: "agent",
    usage: { input_tokens: null, cached_tokens: null, output_tokens: null, reasoning_tokens: null, total_tokens: null },
    cwd: "",
    timestamp: "2026-01-01T00:00:00+00:00",
    status,
    data: {
      type: "message",
      message: { role: "assistant", content: [{ type: "text", text: content }] },
    },
  };
}

function harness() {
  let conversation: Conversation = {
    id: "conversation-1",
    title: "测试",
    sessionId: "session-1",
    messages: [
      { id: "user-1", role: "user", content: "任务", events: [] },
      { id: "assistant-1", role: "assistant", content: "", events: [], running: true },
    ],
  };
  const activeRuns = new Map<string, { controller: AbortController; sessionId: string }>();
  const controller = createRunController({
    activeRuns,
    updateLastMessage: (_id, updater) => {
      const messages = [...conversation.messages];
      messages[messages.length - 1] = updater(messages[messages.length - 1]);
      conversation = { ...conversation, messages };
    },
    updateConversation: (_id, updater) => {
      conversation = updater(conversation);
    },
    rebindRunSession: async () => undefined,
    refreshSessions: async () => undefined,
  });
  return { controller, get conversation() { return conversation; } };
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("RuntimeState streaming projection", () => {
  it("keeps a user cancellation reason when the stream is aborted", async () => {
    const view = harness();
    mocks.streamChat.mockResolvedValue("aborted");

    await view.controller.runConversation({
      conversationId: "conversation-1",
      sessionId: "session-1",
      prompt: "任务",
      resume: false,
      mode: "agent",
      permissionMode: "approval_for_me",
      reasoningEffort: "medium",
    });

    expect(view.conversation.messages[1]).toMatchObject({
      status: "已停止",
      error: "The run was aborted at the user's request.",
      running: false,
    });
  });

  it("renders cumulative node updates without duplicating legacy deltas", async () => {
    const view = harness();
    mocks.streamChat.mockImplementation(async (_prompt: string, onMessage: (message: StreamMessage) => void) => {
      onMessage({ type: "node.create", node: node("") });
      onMessage({ type: "event", kind: "response_delta", message: "旧协议" });
      onMessage({ type: "node.update", node: node("第一段") });
      onMessage({ type: "event", kind: "response_delta", message: "重复片段" });
      expect(view.conversation.messages[1].content).toBe("第一段");
      onMessage({ type: "node.update", node: node("第一段第二段") });
      expect(view.conversation.messages[1].content).toBe("第一段第二段");
      onMessage({ type: "node.delete", node: node("第一段第二段", "success") });
      onMessage({ type: "done", status: "completed", final_answer: "第一段第二段" });
      return "completed" as const;
    });

    await view.controller.runConversation({
      conversationId: "conversation-1",
      sessionId: "session-1",
      prompt: "任务",
      resume: false,
      mode: "agent",
      permissionMode: "approval_for_me",
      reasoningEffort: "medium",
    });

    expect(view.conversation.messages[1]).toMatchObject({
      content: "第一段第二段",
      status: "completed",
      running: false,
    });
  });

  it("clears a stale terminal error when the stream finishes successfully", async () => {
    const view = harness();
    mocks.streamChat.mockImplementation(async (_prompt: string, onMessage: (message: StreamMessage) => void) => {
      onMessage({ type: "node.delete", node: node("中间状态", "abort") });
      expect(view.conversation.messages[1].error).toContain("aborted");
      onMessage({ type: "done", status: "completed", final_answer: "完成" });
      return "completed" as const;
    });

    await view.controller.runConversation({
      conversationId: "conversation-1",
      sessionId: "session-1",
      prompt: "任务",
      resume: false,
      mode: "agent",
      permissionMode: "approval_for_me",
      reasoningEffort: "medium",
    });

    expect(view.conversation.messages[1]).toMatchObject({
      content: "完成",
      status: "completed",
      error: undefined,
    });
  });

  it("keeps the legacy delta fallback when no RuntimeState node arrives", async () => {
    const view = harness();
    mocks.streamChat.mockImplementation(async (_prompt: string, onMessage: (message: StreamMessage) => void) => {
      onMessage({ type: "event", kind: "response_delta", message: "旧" });
      onMessage({ type: "event", kind: "response_delta", data: { content: "协议" } });
      onMessage({ type: "done", status: "completed", final_answer: "旧协议" });
      return "completed" as const;
    });

    await view.controller.runConversation({
      conversationId: "conversation-1",
      sessionId: "session-1",
      prompt: "任务",
      resume: false,
      mode: "agent",
      permissionMode: "approval_for_me",
      reasoningEffort: "medium",
    });

    expect(view.conversation.messages[1].content).toBe("旧协议");
  });

  it("tracks non-assistant nodes without replacing the answer", async () => {
    const view = harness();
    const toolNode: RuntimeStateNode = {
      ...node("工具输出", "success"),
      id: "tool-node",
      data: { type: "compaction", summary: "demo" },
    };
    mocks.streamChat.mockImplementation(async (_prompt: string, onMessage: (message: StreamMessage) => void) => {
      onMessage({ type: "node.update", node: toolNode });
      onMessage({ type: "done", status: "completed", final_answer: "最终答案" });
      return "completed" as const;
    });

    await view.controller.runConversation({
      conversationId: "conversation-1",
      sessionId: "session-1",
      prompt: "任务",
      resume: false,
      mode: "agent",
      permissionMode: "approval_for_me",
      reasoningEffort: "medium",
    });

    expect(view.conversation.messages[1].content).toBe("最终答案");
    expect(view.conversation.lastNodeId).toBe("tool-node");
    expect(view.conversation.runtimeNodes).toEqual([{ ...toolNode, permission_mode: "read_only" }]);
  });
});
