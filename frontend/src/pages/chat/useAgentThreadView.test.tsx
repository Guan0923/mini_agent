import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { TURN_PROTOCOL_VERSION } from "../../app/runtime/runtimeNodeNormalization";
import type { AgentThreadStreamEvent, Conversation, RuntimeStateNode } from "../../types";
import { useAgentThreadView } from "./useAgentThreadView";

const api = vi.hoisted(() => ({
  getSessionNodes: vi.fn(),
  sendAgentThreadMessage: vi.fn(),
  streamAgentThread: vi.fn(),
}));

vi.mock("../../api", () => api);

interface StreamCall {
  sessionId: string;
  threadId: string;
  onEvent: (event: AgentThreadStreamEvent) => void;
  signal: AbortSignal;
}

const streams: StreamCall[] = [];
let latestView: ReturnType<typeof useAgentThreadView> | null = null;

function turn(
  sessionId: string,
  threadId: string,
  turnId: string,
  options: { parentId?: string; deliveryId?: string; status?: RuntimeStateNode["status"] } = {},
): RuntimeStateNode {
  return {
    thread_id: threadId,
    parent_thread_id: sessionId,
    session_id: sessionId,
    parent_session_id: sessionId,
    id: turnId,
    parent_id: options.parentId ?? "",
    version: TURN_PROTOCOL_VERSION,
    firstKeptItemSize: 8,
    compactionId: turnId,
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
    usage: { input_tokens: 1, cached_tokens: 0, output_tokens: 1, reasoning_tokens: 0, total_tokens: 2 },
    cwd: "C:\\workspace",
    project_cwd: "",
    timestamp: turnId.endsWith("2") ? "2026-08-29T00:00:02Z" : "2026-08-29T00:00:01Z",
    status: options.status ?? "success",
    current_data_idx: 0,
    data: [[
      {
        role: "user",
        ...(options.deliveryId ? { delivery_id: options.deliveryId } : {}),
        content: [{ type: "text", text: options.deliveryId ? "follow up" : "child task", status: "success" }],
      },
      { role: "assistant", content: [{ type: "text", text: "child answer", status: "success" }] },
    ]],
  };
}

const childA1 = turn("session_a", "thread_a_child", "turn_a_1");
const initialConversations: Record<string, Conversation> = {
  session_a: {
    id: "session_a",
    sessionId: "session_a",
    threadId: "session_a",
    title: "A",
    messages: [
      { id: "root-user", role: "user", content: "root task", events: [] },
      { id: "root-assistant", role: "assistant", content: "root answer", events: [] },
    ],
    runtimeNodes: [childA1],
  },
  session_b: {
    id: "session_b",
    sessionId: "session_b",
    threadId: "session_b",
    title: "B",
    messages: [],
    runtimeNodes: [],
  },
  thread_a_fork: {
    id: "thread_a_fork",
    sessionId: "session_a",
    threadId: "thread_a_fork",
    title: "A（分支）",
    messages: [
      { id: "fork-user", role: "user", content: "fork task", events: [] },
      { id: "fork-assistant", role: "assistant", content: "fork answer", events: [] },
    ],
    runtimeNodes: [childA1],
  },
};

function Harness() {
  const [currentId, setCurrentId] = useState("session_a");
  const [conversations, setConversations] = useState(initialConversations);
  const canonical = conversations[currentId];
  const view = useAgentThreadView({
    canonical,
    enabled: true,
    onUpdate: (id, updater) => setConversations((current) => ({
      ...current,
      [id]: updater(current[id]),
    })),
  });
  latestView = view;
  return (
    <div>
      <button onClick={() => view.selectThread("thread_a_child")}>select child A</button>
      <button onClick={() => view.selectThread("session_a")}>select root A</button>
      <button onClick={() => setCurrentId("session_a")}>session A</button>
      <button onClick={() => setCurrentId("thread_a_fork")}>fork A</button>
      <button onClick={() => setCurrentId("session_b")}>session B</button>
      <button onClick={() => setConversations((current) => ({
        ...current,
        session_a: {
          ...current.session_a,
          runtimeNodes: [...(current.session_a.runtimeNodes ?? []), turn("session_a", "session_a", "turn_root_2")],
        },
      }))}>append root Turn</button>
      <output data-testid="canonical-thread">{canonical.threadId}</output>
      <output data-testid="canonical-messages">{canonical.messages.map((message) => message.content).join("|")}</output>
      <output data-testid="view-thread">{view.conversation?.threadId}</output>
      <output data-testid="view-messages">{JSON.stringify(view.conversation?.messages ?? [])}</output>
      <output data-testid="tree-invalidation">{view.treeInvalidation}</output>
    </div>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  streams.length = 0;
  latestView = null;
  api.getSessionNodes.mockImplementation(async (sessionId: string) => (
    sessionId === "session_a" ? [childA1] : []
  ));
  api.streamAgentThread.mockImplementation((
    sessionId: string,
    threadId: string,
    onEvent: (event: AgentThreadStreamEvent) => void,
    signal: AbortSignal,
  ) => {
    streams.push({ sessionId, threadId, onEvent, signal });
    onEvent({ type: "thread.ready", session_id: sessionId, thread_id: threadId });
    return new Promise<"aborted">((resolve) => {
      signal.addEventListener("abort", () => resolve("aborted"), { once: true });
    });
  });
  api.sendAgentThreadMessage.mockResolvedValue({
    delivery_id: "delivery_1",
    accepted: true,
    target_state: "started",
    turn_id: "turn_a_2",
  });
});

describe("useAgentThreadView", () => {
  it("invalidates the current root tree when its canonical Turn set changes", async () => {
    render(<Harness />);
    expect(screen.getByTestId("tree-invalidation")).toHaveTextContent("0");

    fireEvent.click(screen.getByRole("button", { name: "append root Turn" }));

    await waitFor(() => expect(screen.getByTestId("tree-invalidation")).toHaveTextContent("1"));
  });

  it("isolates and remembers selection by Sidebar root while aborting the old Thread stream", async () => {
    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "select child A" }));

    await waitFor(() => expect(api.streamAgentThread).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId("view-thread")).toHaveTextContent("thread_a_child");
    expect(screen.getByTestId("view-messages")).toHaveTextContent("child task");
    expect(screen.getByTestId("canonical-thread")).toHaveTextContent("session_a");
    expect(screen.getByTestId("canonical-messages")).toHaveTextContent("root task|root answer");

    fireEvent.click(screen.getByRole("button", { name: "fork A" }));
    await waitFor(() => expect(streams[0].signal.aborted).toBe(true));
    expect(screen.getByTestId("view-thread")).toHaveTextContent("thread_a_fork");
    expect(screen.getByTestId("view-messages")).toHaveTextContent("fork task");
    expect(api.streamAgentThread).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "session B" }));
    expect(screen.getByTestId("view-thread")).toHaveTextContent("session_b");

    fireEvent.click(screen.getByRole("button", { name: "session A" }));
    await waitFor(() => expect(api.streamAgentThread).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId("view-thread")).toHaveTextContent("thread_a_child");
  });

  it("shows a pending delivery and reconciles it with the canonical SSE user message", async () => {
    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "select child A" }));
    await waitFor(() => expect(streams).toHaveLength(1));

    await act(async () => {
      await latestView!.sendMessage({
        content: "follow up",
        references: [{ source: "project", path: "README.md" }],
        mode: "plan",
        permissionMode: "workspace_write",
        providerName: "local",
        model: childA1.model,
      });
    });
    expect(screen.getByTestId("view-messages")).toHaveTextContent('"pending":true');
    expect(api.sendAgentThreadMessage).toHaveBeenCalledWith(
      "thread_a_child",
      expect.objectContaining({
        sessionId: "session_a",
        references: [{ source: "project", path: "README.md" }],
        mode: "plan",
        permissionMode: "workspace_write",
      }),
    );

    const canonical = turn("session_a", "thread_a_child", "turn_a_2", {
      parentId: childA1.id,
      deliveryId: "delivery_1",
    });
    await act(async () => {
      streams[0].onEvent({ type: "turn.snapshot", revision: 0, turn: canonical });
      streams[0].onEvent({
        type: "turn.terminal",
        session_id: "session_a",
        thread_id: "thread_a_child",
        turn_id: canonical.id,
        status: "success",
      });
    });

    await waitFor(() => expect(screen.getByTestId("view-messages")).not.toHaveTextContent('"pending":true'));
    const rendered = screen.getByTestId("view-messages").textContent ?? "";
    expect(rendered.match(/"deliveryId":"delivery_1"/g)).toHaveLength(1);
    expect(screen.getByTestId("canonical-thread")).toHaveTextContent("session_a");
    expect(screen.getByTestId("canonical-messages")).toHaveTextContent("root task|root answer");
    expect(screen.getByTestId("tree-invalidation")).toHaveTextContent("1");
    expect(streams[0].signal.aborted).toBe(false);
  });

  it("reloads history and reconnects after an unexpected idle stream end", async () => {
    api.streamAgentThread
      .mockImplementationOnce(async (
        sessionId: string,
        threadId: string,
        onEvent: (event: AgentThreadStreamEvent) => void,
      ) => {
        onEvent({ type: "thread.ready", session_id: sessionId, thread_id: threadId });
        return "ended";
      })
      .mockImplementationOnce((
        sessionId: string,
        threadId: string,
        onEvent: (event: AgentThreadStreamEvent) => void,
        signal: AbortSignal,
      ) => {
        onEvent({ type: "thread.ready", session_id: sessionId, thread_id: threadId });
        return new Promise<"aborted">((resolve) => {
          signal.addEventListener("abort", () => resolve("aborted"), { once: true });
        });
      });
    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "select child A" }));

    await waitFor(() => expect(api.streamAgentThread).toHaveBeenCalledTimes(2), { timeout: 2_000 });
    expect(api.getSessionNodes.mock.calls.filter(([sessionId]) => sessionId === "session_a").length).toBeGreaterThanOrEqual(2);
  });
});
