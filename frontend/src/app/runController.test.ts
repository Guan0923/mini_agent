import { afterEach, describe, expect, it, vi } from "vitest";

import { pauseTurn, streamAttachedTurn, streamChat } from "../api";
import type { Conversation, RuntimeStateNode } from "../types";
import { createRunController } from "./runController";
import { TURN_PROTOCOL_VERSION } from "./runtime/runtimeNodeNormalization";

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return {
    ...actual,
    pauseTurn: vi.fn().mockResolvedValue(undefined),
    streamChat: vi.fn(),
    streamAttachedTurn: vi.fn(),
    streamResume: vi.fn(),
    streamRewind: vi.fn(),
  };
});

function turn(): RuntimeStateNode {
  return {
    thread_id: "session_1",
    parent_thread_id: "",
    session_id: "session_1",
    parent_session_id: "",
    id: "turn_1",
    parent_id: "",
    version: TURN_PROTOCOL_VERSION,
    firstKeptItemSize: 8,
    compactionId: "turn_1",
    user: "user_1",
    provider_name: "local",
    model: { reasoning_effort: "medium", current_model: "test", context_length: 4096, output_length: 512, thinking: "enable", temperature: 0 },
    permission_mode: "read_only",
    running_mode: "agent",
    usage: { input_tokens: 0, cached_tokens: 0, output_tokens: 0, reasoning_tokens: 0, total_tokens: 0 },
    cwd: "C:\\workspace",
    project_cwd: "",
    timestamp: "2026-08-26T00:00:00Z",
    status: "running",
    current_data_idx: 0,
    data: [[
      { role: "user", content: [{ type: "text", text: "hello", status: "success" }] },
      { role: "assistant", content: [{ type: "text", text: "", status: "running" }] },
    ]],
  };
}

function request() {
  return {
    conversationId: "conversation_1",
    sessionId: "session_1",
    threadId: "session_1",
    turnId: "turn_1",
    prompt: "hello",
    resume: false,
    mode: "agent" as const,
    permissionMode: "read_only" as const,
    reasoningEffort: "medium" as const,
  };
}

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("run controller incremental batching", () => {
  it("reports admission rejection without clearing an unaccepted composer draft", async () => {
    vi.mocked(streamChat).mockRejectedValue(new Error("message_queue_unavailable"));
    const onAccepted = vi.fn();
    const onAdmissionRejected = vi.fn();
    const controller = createRunController({
      activeRuns: new Map(),
      updateLastMessage: vi.fn(),
      rebindRunSession: vi.fn().mockResolvedValue(undefined),
      refreshSessions: vi.fn().mockResolvedValue(undefined),
      updateConversation: vi.fn(),
      recoverConversation: vi.fn().mockResolvedValue(undefined),
    });

    await controller.runConversation({ ...request(), onAccepted, onAdmissionRejected });

    expect(onAccepted).not.toHaveBeenCalled();
    expect(onAdmissionRejected).toHaveBeenCalledTimes(1);
  });

  it("commits multiple SSE frames in one animation-frame state update", async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    vi.mocked(streamChat).mockImplementation(async (_prompt, onMessage) => {
      onMessage({ type: "turn.snapshot", revision: 0, turn: turn() });
      onMessage({
        type: "turn.delta",
        session_id: "session_1",
        turn_id: "turn_1",
        revision: 1,
        operations: [{ op: "append_text", data_idx: 0, message_idx: 1, item_idx: 0, delta: "world" }],
      });
      await gate;
      return "completed";
    });

    let animationFrame: FrameRequestCallback | undefined;
    vi.stubGlobal("requestAnimationFrame", vi.fn((callback: FrameRequestCallback) => {
      animationFrame = callback;
      return 1;
    }));
    vi.stubGlobal("cancelAnimationFrame", vi.fn());

    let conversation: Conversation = { id: "conversation_1", title: "x", messages: [], runtimeNodes: [] };
    const updateConversation = vi.fn((_id: string, updater: (value: Conversation) => Conversation) => {
      conversation = updater(conversation);
    });
    const controller = createRunController({
      activeRuns: new Map(),
      updateLastMessage: vi.fn(),
      rebindRunSession: vi.fn().mockResolvedValue(undefined),
      refreshSessions: vi.fn().mockResolvedValue(undefined),
      updateConversation,
      recoverConversation: vi.fn().mockResolvedValue(undefined),
    });

    const running = controller.runConversation(request());
    expect(updateConversation).not.toHaveBeenCalled();
    expect(animationFrame).toBeTypeOf("function");
    animationFrame?.(0);
    expect(updateConversation).toHaveBeenCalledTimes(1);
    expect(conversation.messages.map((message) => message.content)).toEqual(["hello", "world"]);
    release();
    await running;
    expect(updateConversation).toHaveBeenCalledTimes(1);
  });

  it("forces a full path projection on the next frame for an SSE current_data_idx patch", async () => {
    let releaseSnapshot!: () => void;
    let releasePatch!: () => void;
    const snapshotGate = new Promise<void>((resolve) => { releaseSnapshot = resolve; });
    const patchGate = new Promise<void>((resolve) => { releasePatch = resolve; });
    vi.mocked(streamChat).mockImplementation(async (_prompt, onMessage) => {
      const versioned = turn();
      versioned.data = [
        [
          { role: "user", content: [{ type: "text", text: "old user", status: "success" }] },
          { role: "assistant", content: [{ type: "text", text: "old answer", status: "success" }] },
        ],
        [
          { role: "user", content: [{ type: "text", text: "new user", status: "success" }] },
          { role: "assistant", content: [{ type: "text", text: "new answer", status: "success" }] },
          { role: "user", delivery_id: "steering-delivery", content: [{ type: "text", text: "new steering", status: "success" }] },
          { role: "assistant", content: [{ type: "text", text: "steering answer", status: "running" }] },
        ],
      ];
      onMessage({ type: "turn.snapshot", revision: 0, turn: versioned });
      await snapshotGate;
      onMessage({
        type: "turn.delta",
        session_id: versioned.session_id,
        turn_id: versioned.id,
        revision: 1,
        patch: { current_data_idx: 1 },
      });
      await patchGate;
      return "completed";
    });

    const frames: FrameRequestCallback[] = [];
    vi.stubGlobal("requestAnimationFrame", vi.fn((callback: FrameRequestCallback) => {
      frames.push(callback);
      return frames.length;
    }));
    vi.stubGlobal("cancelAnimationFrame", vi.fn());

    let conversation: Conversation = { id: "conversation_1", title: "x", messages: [], runtimeNodes: [] };
    const updateConversation = vi.fn((_id: string, updater: (value: Conversation) => Conversation) => {
      conversation = updater(conversation);
    });
    const controller = createRunController({
      activeRuns: new Map(),
      updateLastMessage: vi.fn(),
      rebindRunSession: vi.fn().mockResolvedValue(undefined),
      refreshSessions: vi.fn().mockResolvedValue(undefined),
      updateConversation,
      recoverConversation: vi.fn().mockResolvedValue(undefined),
    });

    const running = controller.runConversation(request());
    expect(frames).toHaveLength(1);
    frames.shift()?.(0);
    expect(conversation.messages.map((message) => message.content)).toEqual(["old user", "old answer"]);

    releaseSnapshot();
    await vi.waitFor(() => expect(frames).toHaveLength(1));
    frames.shift()?.(1);
    expect(conversation.messages.map((message) => message.content)).toEqual([
      "new user",
      "new answer",
      "new steering",
      "steering answer",
    ]);
    expect(conversation.messages.filter((message) => message.role === "user").map((message) => message.timelineSource))
      .toEqual(["user", "steering"]);

    releasePatch();
    await running;
  });

  it("rebases the same Turn from a repeated authoritative reconnect snapshot", async () => {
    vi.mocked(streamChat).mockImplementation(async (_prompt, onMessage) => {
      onMessage({ type: "turn.snapshot", revision: 0, turn: turn() });
      const rebased = turn();
      rebased.data[0][1].content = [{ type: "text", text: "reconnected", status: "running" }];
      onMessage({ type: "turn.snapshot", revision: 0, turn: rebased });
      onMessage({
        type: "turn.delta",
        session_id: "session_1",
        turn_id: "turn_1",
        revision: 1,
        operations: [{ op: "append_text", data_idx: 0, message_idx: 1, item_idx: 0, delta: " stream" }],
      });
      return "completed";
    });
    let conversation: Conversation = { id: "conversation_1", title: "x", messages: [], runtimeNodes: [] };
    const updateConversation = vi.fn((_id: string, updater: (value: Conversation) => Conversation) => {
      conversation = updater(conversation);
    });
    const recoverConversation = vi.fn().mockResolvedValue(undefined);
    const controller = createRunController({
      activeRuns: new Map(),
      updateLastMessage: vi.fn(),
      rebindRunSession: vi.fn().mockResolvedValue(undefined),
      refreshSessions: vi.fn().mockResolvedValue(undefined),
      updateConversation,
      recoverConversation,
    });

    await controller.runConversation(request());

    expect(recoverConversation).not.toHaveBeenCalled();
    expect(conversation.messages[conversation.messages.length - 1]?.content).toBe("reconnected stream");
  });

  it("pauses and reloads the authoritative session after a protocol error", async () => {
    vi.mocked(streamChat).mockImplementation(async (_prompt, onMessage) => {
      onMessage({
        type: "turn.delta",
        session_id: "session_1",
        turn_id: "turn_1",
        revision: 1,
        operations: [{
          op: "append_item",
          data_idx: 0,
          message_idx: 1,
          item_idx: 0,
          item: { type: "text", text: "bad", status: "running" },
        }],
      });
      return "completed";
    });
    const recoverConversation = vi.fn().mockResolvedValue(undefined);
    const controller = createRunController({
      activeRuns: new Map(),
      updateLastMessage: vi.fn(),
      rebindRunSession: vi.fn().mockResolvedValue(undefined),
      refreshSessions: vi.fn().mockResolvedValue(undefined),
      updateConversation: vi.fn(),
      recoverConversation,
    });

    await controller.runConversation(request());

    expect(pauseTurn).toHaveBeenCalledWith("turn_1");
    expect(recoverConversation).toHaveBeenCalledWith("conversation_1", "session_1", "turn_1");
  });

  it("attaches to an existing Turn without pausing it", async () => {
    vi.mocked(streamAttachedTurn).mockImplementation(async (_turnId, onMessage) => {
      onMessage({ type: "turn.snapshot", revision: 0, turn: turn() });
      onMessage({
        type: "turn.delta",
        session_id: "session_1",
        turn_id: "turn_1",
        revision: 1,
        patch: { status: "success" },
      });
      return "completed";
    });
    const onBaseline = vi.fn();
    const controller = createRunController({
      activeRuns: new Map(),
      updateLastMessage: vi.fn(),
      rebindRunSession: vi.fn().mockResolvedValue(undefined),
      refreshSessions: vi.fn().mockResolvedValue(undefined),
      updateConversation: vi.fn(),
      recoverConversation: vi.fn().mockResolvedValue(undefined),
    });

    await controller.runConversation({ ...request(), attach: true, prompt: null, onBaseline });

    expect(streamAttachedTurn).toHaveBeenCalledWith(
      "turn_1",
      expect.any(Function),
      expect.any(AbortSignal),
      "session_1",
    );
    expect(onBaseline).toHaveBeenCalledTimes(1);
    expect(pauseTurn).not.toHaveBeenCalled();
  });

  it("reloads the final Turn when an attached terminal races the last delta", async () => {
    vi.mocked(streamAttachedTurn).mockImplementation(async (_turnId, onMessage) => {
      onMessage({ type: "turn.snapshot", revision: 0, turn: turn() });
      return "completed";
    });
    const recoverConversation = vi.fn().mockResolvedValue(undefined);
    const updateLastMessage = vi.fn();
    const controller = createRunController({
      activeRuns: new Map(),
      updateLastMessage,
      rebindRunSession: vi.fn().mockResolvedValue(undefined),
      refreshSessions: vi.fn().mockResolvedValue(undefined),
      updateConversation: vi.fn(),
      recoverConversation,
    });

    await controller.runConversation({ ...request(), attach: true, prompt: null });

    expect(recoverConversation).toHaveBeenCalledWith("conversation_1", "session_1", "turn_1");
    expect(updateLastMessage).not.toHaveBeenCalled();
    expect(pauseTurn).not.toHaveBeenCalled();
  });

  it("waits for the current run to release before starting a queued run", async () => {
    let releaseFirst!: () => void;
    const firstGate = new Promise<void>((resolve) => { releaseFirst = resolve; });
    vi.mocked(streamChat)
      .mockImplementationOnce(async () => {
        await firstGate;
        return "completed";
      })
      .mockResolvedValueOnce("completed");
    const activeRuns = new Map();
    const controller = createRunController({
      activeRuns,
      updateLastMessage: vi.fn(),
      rebindRunSession: vi.fn().mockResolvedValue(undefined),
      refreshSessions: vi.fn().mockResolvedValue(undefined),
      updateConversation: vi.fn(),
      recoverConversation: vi.fn().mockResolvedValue(undefined),
    });

    const first = controller.runConversation(request());
    const second = controller.runConversation({
      ...request(),
      turnId: "turn_2",
      prompt: "queued",
      waitForActiveRun: true,
    });
    await Promise.resolve();
    expect(streamChat).toHaveBeenCalledTimes(1);

    releaseFirst();
    await first;
    await second;
    expect(streamChat).toHaveBeenCalledTimes(2);
    expect(vi.mocked(streamChat).mock.calls[1][0]).toBe("queued");
  });
});
