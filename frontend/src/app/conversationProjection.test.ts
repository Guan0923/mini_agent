import { describe, expect, it } from "vitest";
import type { Conversation, RuntimeStateNode, RuntimeTreeNode } from "../types";
import { withLoadedTurns } from "./conversationProjection";

const turn = (
  id: string,
  parentId: string,
  threadId: string,
  prompt: string,
): RuntimeStateNode => ({
  id,
  session_id: "session",
  thread_id: threadId,
  parent_id: parentId,
  parent_session_id: "session",
  parent_thread_id: parentId === "root" ? "session" : threadId,
  version: "1",
  firstKeptItemSize: 0,
  compactionId: id,
  user: "",
  provider_name: "provider",
  model: {
    current_model: "model",
    context_length: 8192,
    output_length: 1024,
    thinking: "disable",
    temperature: 0,
    reasoning_effort: "medium",
  },
  permission_mode: "read_only",
  running_mode: "agent",
  usage: {
    input_tokens: null,
    output_tokens: null,
    total_tokens: null,
    cached_tokens: null,
    reasoning_tokens: null,
  },
  cwd: "C:\\work",
  timestamp: `2026-08-29T00:00:0${id === "anchor" ? 1 : 2}+00:00`,
  status: "success",
  current_data_idx: 0,
  data: [[
    { role: "user", content: [{ type: "text", text: prompt, status: "success" }] },
    { role: "assistant", content: [{ type: "text", text: `${prompt}-answer`, status: "success" }] },
  ]],
});

describe("side-chat conversation projection", () => {
  it("keeps the hidden anchor as context but starts visible Chat at its child Turn", () => {
    const nodes: RuntimeTreeNode[] = [
      { id: "root", session_id: "session", thread_id: "session" },
      turn("anchor", "root", "thread-side", "copied-context"),
      turn("child", "anchor", "thread-side", "visible-question"),
    ];
    const conversation: Conversation = {
      id: "window",
      title: "侧聊 1",
      sessionId: "session",
      threadId: "thread-side",
      hiddenBeforeTurnId: "anchor",
      activeTurnId: "child",
      lastNodeId: "child",
      messages: [],
      messagesLoaded: false,
    };

    const projected = withLoadedTurns(conversation, nodes, "child");

    expect(projected.runtimeNodes).toEqual(nodes);
    expect(projected.messages.map((message) => message.content)).toEqual(["visible-question", "visible-question-answer"]);
    expect(projected.messages.every((message) => message.id.startsWith("child:message:"))).toBe(true);
  });

  it("selects the latest side-chat leaf during a cold hydration", () => {
    const nodes: RuntimeTreeNode[] = [
      { id: "root", session_id: "session", thread_id: "session" },
      turn("anchor", "root", "thread-side", "copied-context"),
      turn("child", "anchor", "thread-side", "persisted-question"),
    ];
    const conversation: Conversation = {
      id: "window",
      title: "侧聊 1",
      sessionId: "session",
      threadId: "thread-side",
      hiddenBeforeTurnId: "anchor",
      messages: [],
      messagesLoaded: false,
    };

    const projected = withLoadedTurns(conversation, nodes);

    expect(projected.activeTurnId).toBe("child");
    expect(projected.lastNodeId).toBe("child");
    expect(projected.messages.map((message) => message.content)).toEqual([
      "persisted-question",
      "persisted-question-answer",
    ]);
  });
});
