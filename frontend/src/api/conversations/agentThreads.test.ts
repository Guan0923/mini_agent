import { afterEach, describe, expect, it, vi } from "vitest";

import { TURN_PROTOCOL_VERSION } from "../../app/runtime/runtimeNodeNormalization";
import type { AgentThreadStreamEvent, RuntimeStateNode } from "../../types";
import { sendAgentThreadMessage, streamAgentThread } from "./agentThreads";

function turn(): RuntimeStateNode {
  return {
    thread_id: "thread_child",
    parent_thread_id: "session_1",
    session_id: "session_1",
    parent_session_id: "session_1",
    id: "turn_child",
    parent_id: "turn_root",
    version: TURN_PROTOCOL_VERSION,
    firstKeptItemSize: 8,
    compactionId: "turn_child",
    user: "user_1",
    provider_name: "local",
    model: {
      reasoning_effort: "medium",
      current_model: "fake",
      context_length: 128_000,
      output_length: 1_024,
      thinking: "enable",
      temperature: 0,
    },
    permission_mode: "read_only",
    running_mode: "agent",
    usage: { input_tokens: 0, cached_tokens: 0, output_tokens: 0, reasoning_tokens: 0, total_tokens: 0 },
    cwd: "C:\\workspace",
    project_cwd: "",
    timestamp: "2026-08-29T00:00:00Z",
    status: "running",
    current_data_idx: 0,
    data: [[
      { role: "user", delivery_id: "delivery_1", content: [{ type: "text", text: "hello", status: "success" }] },
      { role: "assistant", content: [] },
    ]],
  };
}

afterEach(() => vi.unstubAllGlobals());

describe("Agent Thread API", () => {
  it("sends references and next-Turn runtime configuration without a source id", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      delivery_id: "delivery_1",
      accepted: true,
      target_state: "started",
      turn_id: "turn_child",
    }), { status: 202, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    await sendAgentThreadMessage("thread_child", {
      sessionId: "session_1",
      content: "inspect",
      references: [{ source: "project", path: "README.md" }],
      mode: "plan",
      permissionMode: "workspace_write",
      providerName: "local",
      model: turn().model,
    });

    const body = JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body));
    expect(body).toMatchObject({
      session_id: "session_1",
      content: "inspect",
      references: [{ source: "project", path: "README.md" }],
      running_mode: "plan",
      permission_mode: "workspace_write",
      provider_name: "local",
    });
    expect(body).not.toHaveProperty("source_thread_id");
  });

  it("keeps parsing snapshots, deltas, and terminals after a heartbeat", async () => {
    const payloads = [
      { type: "thread.ready", session_id: "session_1", thread_id: "thread_child" },
      { type: "turn.snapshot", revision: 0, turn: turn() },
      { type: "turn.delta", session_id: "session_1", turn_id: "turn_child", revision: 1, patch: { status: "success" } },
      { type: "turn.terminal", session_id: "session_1", thread_id: "thread_child", turn_id: "turn_child", status: "success" },
    ];
    const body = `: heartbeat\n\n${payloads.map((item) => `data: ${JSON.stringify(item)}\n\n`).join("")}`;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    })));
    const events: AgentThreadStreamEvent[] = [];

    await expect(streamAgentThread(
      "session_1",
      "thread_child",
      (event) => events.push(event),
      new AbortController().signal,
    )).resolves.toBe("ended");
    expect(events.map((event) => event.type)).toEqual([
      "thread.ready",
      "turn.snapshot",
      "turn.delta",
      "turn.terminal",
    ]);
  });

  it("accepts and advances the standard Redis Stream reconnect cursor", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(
      'id: 42-0\ndata: {"type":"thread.ready","session_id":"session_1","thread_id":"thread_child"}\n\n',
      { status: 200, headers: { "Content-Type": "text/event-stream" } },
    ));
    vi.stubGlobal("fetch", fetchMock);
    const onCursor = vi.fn();

    await streamAgentThread(
      "session_1",
      "thread_child",
      vi.fn(),
      new AbortController().signal,
      "41-0",
      onCursor,
    );

    expect(fetchMock).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({
      cache: "no-store",
      headers: { "Last-Event-ID": "41-0" },
    }));
    expect(onCursor).toHaveBeenCalledWith("42-0");
  });
});
