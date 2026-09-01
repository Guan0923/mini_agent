import { afterEach, describe, expect, it, vi } from "vitest";

import { streamAttachedTurn, streamChat } from "./chat";
import { TURN_PROTOCOL_VERSION } from "../../app/runtime/runtimeNodeNormalization";
import type { RuntimeStateNode, StreamMessage } from "../../types";

function turn(status: RuntimeStateNode["status"] = "running"): RuntimeStateNode {
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
    model: {
      reasoning_effort: "medium",
      current_model: "deterministic",
      context_length: 128000,
      output_length: 8192,
      thinking: "enable",
      temperature: 1,
    },
    permission_mode: "read_only",
    running_mode: "agent",
    usage: {
      input_tokens: 0,
      cached_tokens: 0,
      output_tokens: 0,
      reasoning_tokens: 0,
      total_tokens: 0,
    },
    cwd: "C:\\workspace",
    project_cwd: "",
    timestamp: "2026-08-25T00:00:00Z",
    status,
    current_data_idx: 0,
    data: [[
      { role: "user", content: [{ type: "text", text: "hello", status: "success" }] },
      { role: "assistant", content: [] },
    ]],
  };
}

function response(lines: string[]): Response {
  return new Response(lines.map((line) => `data: ${line}\n\n`).join(""), {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

function accepted(): Response {
  return new Response(JSON.stringify({ turn_id: "turn_1", delivery_id: "delivery_1", status: "accepted" }), {
    status: 202,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => vi.unstubAllGlobals());

describe("Turn SSE contract", () => {
  it("accepts one Turn baseline followed by consecutive deltas and the matching terminal", async () => {
    const frames: StreamMessage[] = [];
    const fetchMock = vi.fn().mockResolvedValueOnce(accepted()).mockResolvedValueOnce(response([
      JSON.stringify({ type: "turn.snapshot", revision: 0, turn: turn() }),
      JSON.stringify({ type: "turn.delta", session_id: "session_1", turn_id: "turn_1", revision: 1, patch: { status: "success" } }),
      '<SSE id="turn_1" type="success"></SSE>',
    ]));
    vi.stubGlobal("fetch", fetchMock);
    const onAccepted = vi.fn();

    await expect(streamChat("hello", (frame) => frames.push(frame), new AbortController().signal, {
      sessionId: "session_1",
      threadId: "session_1",
      turnId: "turn_1",
      deliveryId: "delivery-direct",
      onAccepted,
    })).resolves.toBe("completed");
    expect(frames.map((frame) => frame.type)).toEqual(["turn.snapshot", "turn.delta"]);
    expect(onAccepted).toHaveBeenCalledTimes(1);
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toMatchObject({
      delivery_id: "delivery-direct",
    });
  });

  it("reconnects an interrupted stream with Last-Event-ID and accepts the rebased snapshot", async () => {
    const first = new Response([
      "id: 10-0",
      `data: ${JSON.stringify({ type: "turn.snapshot", revision: 0, turn: turn() })}`,
      "",
      "",
    ].join("\n"), { status: 200, headers: { "Content-Type": "text/event-stream" } });
    const completed = new Response([
      "id: 11-0",
      `data: ${JSON.stringify({ type: "turn.snapshot", revision: 0, turn: turn("success") })}`,
      "",
      "id: 11-0",
      'data: <SSE id="turn_1" type="success"></SSE>',
      "",
      "",
    ].join("\n"), { status: 200, headers: { "Content-Type": "text/event-stream" } });
    const frames: StreamMessage[] = [];
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(accepted())
      .mockResolvedValueOnce(first)
      .mockResolvedValueOnce(completed);
    vi.stubGlobal("fetch", fetchMock);

    await expect(streamChat("hello", (frame) => frames.push(frame), new AbortController().signal, {
      sessionId: "session_1",
      turnId: "turn_1",
    })).resolves.toBe("completed");

    expect(frames.map((frame) => frame.type)).toEqual(["turn.snapshot", "turn.snapshot"]);
    expect(fetchMock.mock.calls[2]?.[1]).toEqual(expect.objectContaining({
      headers: expect.objectContaining({ "Last-Event-ID": "10-0" }),
    }));
  });

  it("creates a Turn from queued_delivery without sending a duplicate message body", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(accepted()).mockResolvedValueOnce(response([
      JSON.stringify({ type: "turn.snapshot", revision: 0, turn: turn() }),
      '<SSE id="turn_1" type="success"></SSE>',
    ]));
    vi.stubGlobal("fetch", fetchMock);

    await streamChat("", () => undefined, new AbortController().signal, {
      sessionId: "session_1",
      threadId: "session_1",
      turnId: "turn_1",
      queuedDelivery: { deliveryId: "delivery_1", messageIds: ["message_1", "message_2"] },
    });

    const body = JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body));
    expect(body).toMatchObject({
      queued_delivery: { delivery_id: "delivery_1", message_ids: ["message_1", "message_2"] },
    });
    expect(body).not.toHaveProperty("message");
  });

  it("attaches to a running Turn with GET and the same strict frame contract", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response([
      JSON.stringify({ type: "turn.snapshot", revision: 0, turn: turn() }),
      JSON.stringify({ type: "turn.delta", session_id: "session_1", turn_id: "turn_1", revision: 1, patch: { status: "success" } }),
      '<SSE id="turn_1" type="success"></SSE>',
    ]));
    vi.stubGlobal("fetch", fetchMock);

    await expect(streamAttachedTurn(
      "turn_1",
      () => undefined,
      new AbortController().signal,
    )).resolves.toBe("completed");
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/turns/turn_1/stream"),
      expect.objectContaining({ method: "GET" }),
    );
    expect(fetchMock.mock.calls[0]?.[1]).not.toHaveProperty("credentials");
  });

  it("rejects a stream that ends without a terminal envelope", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(accepted()).mockResolvedValueOnce(response([
      JSON.stringify({ type: "turn.snapshot", revision: 0, turn: turn() }),
    ])));

    await expect(streamChat("hello", () => undefined, new AbortController().signal, {
      sessionId: "session_1",
      turnId: "turn_1",
    })).rejects.toThrow("unexpectedly ended");
  });

  it("requires network terminals to carry the original frontend Turn id", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(accepted()).mockResolvedValueOnce(response([
      JSON.stringify({ type: "turn.snapshot", revision: 0, turn: turn() }),
      '<SSE id="network" type="network"></SSE>',
    ])));

    await expect(streamChat("hello", () => undefined, new AbortController().signal, {
      sessionId: "session_1",
      turnId: "turn_1",
    })).rejects.toThrow("terminal id does not match");
  });

  it("rejects a terminal-only stream and a mismatched first baseline", async () => {
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(accepted())
      .mockResolvedValueOnce(response([
      '<SSE id="turn_1" type="success"></SSE>',
      ])));
    await expect(streamChat("hello", () => undefined, new AbortController().signal, {
      sessionId: "session_1",
      turnId: "turn_1",
    })).rejects.toThrow("without a Turn baseline");

    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(accepted())
      .mockResolvedValueOnce(response([
      JSON.stringify({ type: "turn.snapshot", revision: 0, turn: turn() }),
      '<SSE id="turn_2" type="success"></SSE>',
      ])));
    await expect(streamChat("hello", () => undefined, new AbortController().signal, {
      sessionId: "session_1",
      turnId: "turn_2",
    })).rejects.toThrow("baseline id does not match");
  });

  it("surfaces a matching startup failure before a Turn baseline exists", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(accepted()).mockResolvedValueOnce(response([
      '<SSE id="turn_1" type="failed">Windows Sandbox Broker 未安装或当前不可用。</SSE>',
    ])));

    await expect(streamChat("hello", () => undefined, new AbortController().signal, {
      sessionId: "session_1",
      turnId: "turn_1",
    })).rejects.toThrow("Windows Sandbox Broker 未安装或当前不可用。");
  });

  it("rejects a startup failure for a different Turn", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(accepted()).mockResolvedValueOnce(response([
      '<SSE id="turn_other" type="failed">Sandbox 初始化失败</SSE>',
    ])));

    await expect(streamChat("hello", () => undefined, new AbortController().signal, {
      sessionId: "session_1",
      turnId: "turn_1",
    })).rejects.toThrow("terminal id does not match");
  });
});
