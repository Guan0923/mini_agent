import { App as AntApp } from "antd";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { projectTurnPath } from "../../app/runtimeDetailProjection";
import type { QueuedMessage } from "../../app/types";
import type { Conversation, RuntimeStateNode } from "../../types";
import ChatPage from "./ChatPage";

function turn(id: string, userText: string, parent?: RuntimeStateNode): RuntimeStateNode {
  return {
    thread_id: "session-rewind",
    parent_thread_id: parent?.thread_id ?? "",
    session_id: "session-rewind",
    parent_session_id: parent?.session_id ?? "",
    id,
    parent_id: parent?.id ?? "",
    version: "0.0.1",
    firstKeptItemSize: 8,
    compactionId: id,
    user: "user-1",
    provider_name: "local",
    model: {
      reasoning_effort: "medium",
      current_model: "test",
      context_length: 4096,
      output_length: 512,
      thinking: "enable",
      temperature: 0,
    },
    permission_mode: "read_only",
    running_mode: "agent",
    usage: { input_tokens: 1, cached_tokens: 0, output_tokens: 1, reasoning_tokens: 0, total_tokens: 2 },
    cwd: "C:\\workspace",
    timestamp: `2026-08-26T00:00:0${parent ? 1 : 0}Z`,
    status: "success",
    current_data_idx: 0,
    data: [[
      { role: "user", content: [{ type: "text", text: userText }] },
      { role: "assistant", content: [{ type: "text", text: `${userText}-answer` }] },
    ]],
  };
}

function Harness({ onRun, onRewind }: { onRun: ReturnType<typeof vi.fn>; onRewind: ReturnType<typeof vi.fn> }) {
  const root = turn("turn-root", "root");
  const target = turn("turn-target", "target", root);
  const descendant = turn("turn-descendant", "descendant", target);
  const nodes = [root, target, descendant];
  const map = new Map(nodes.map((node) => [`${node.session_id}:${node.id}`, node] as const));
  const [conversation, setConversation] = useState<Conversation>({
    id: "session-rewind",
    sessionId: "session-rewind",
    threadId: "session-rewind",
    title: "rewind",
    runtimeNodes: nodes,
    activeTurnId: descendant.id,
    lastNodeId: descendant.id,
    messagesLoaded: true,
    messages: projectTurnPath(map, descendant.id),
  });

  return (
    <AntApp>
      <ChatPage
        conversation={conversation}
        onUpdate={(_id, updater) => setConversation((current) => updater(current))}
        onNew={async () => conversation.id}
        onNavigate={() => undefined}
        onEnsureSession={async () => conversation.sessionId!}
        onRewind={onRewind}
        onRun={async (request) => { onRun(request); }}
      />
      <output data-testid="runtime-node-ids">{conversation.runtimeNodes?.map((node) => node.id).join(",")}</output>
      <output data-testid="active-turn-id">{conversation.activeTurnId}</output>
      <output data-testid="visible-message-text">{conversation.messages.map((message) => message.content).join("|")}</output>
    </AntApp>
  );
}

function QueueHarness({
  terminalStatus,
  onRun,
  runGate,
}: {
  terminalStatus: RuntimeStateNode["status"];
  onRun: ReturnType<typeof vi.fn>;
  runGate?: Promise<void>;
}) {
  const [node, setNode] = useState(() => {
    const value = turn("turn-running", "running");
    value.status = "running";
    return value;
  });
  const [queued, setQueued] = useState<QueuedMessage[]>([
    {
      id: "queued-1",
      content: "第一条",
      references: [{ source: "project", path: "README.md" }],
    },
    {
      id: "queued-2",
      content: "第二条",
      references: [
        { source: "project", path: "README.md" },
        { source: "upload", path: "notes.txt" },
      ],
    },
  ]);
  const conversation: Conversation = {
    id: "session-rewind",
    sessionId: "session-rewind",
    threadId: "session-rewind",
    title: "queue",
    runtimeNodes: [node],
    activeTurnId: node.id,
    lastNodeId: node.id,
    messagesLoaded: true,
    messages: projectTurnPath(new Map([[`${node.session_id}:${node.id}`, node]]), node.id),
  };
  return (
    <AntApp>
      <ChatPage
        conversation={conversation}
        running={node.status === "running"}
        queuedMessages={queued}
        onQueuedMessagesChange={(_conversationId, updater) => setQueued((current) => updater(current))}
        onUpdate={() => undefined}
        onNew={async () => conversation.id}
        onNavigate={() => undefined}
        onEnsureSession={async () => conversation.sessionId!}
        onRun={async (request) => {
          onRun(request);
          const accepted = turn("turn-queued", request.prompt ?? "");
          accepted.status = "running";
          setNode(accepted);
          request.onBaseline?.(accepted);
          if (runGate) {
            await runGate;
            setNode((current) => ({ ...current, status: "success" }));
          }
        }}
      />
      <button type="button" onClick={() => setNode((current) => ({ ...current, status: terminalStatus }))}>
        结束当前 Turn
      </button>
      <button type="button" onClick={() => setQueued((current) => [
        ...current,
        { id: "queued-during-submit", content: "提交期间新增" },
      ])}>
        提交期间新增队列项
      </button>
      <output data-testid="queued-count">{queued.length}</output>
    </AntApp>
  );
}

describe("ChatPage rewind projection", () => {
  afterEach(() => vi.restoreAllMocks());

  it("prunes descendants only when the edited message is submitted for rewind", async () => {
    const nativeGetComputedStyle = window.getComputedStyle.bind(window);
    vi.spyOn(window, "getComputedStyle").mockImplementation((element, pseudoElement) => {
      const style = nativeGetComputedStyle(element, pseudoElement);
      if (!(element instanceof HTMLTextAreaElement)) return style;
      return new Proxy(style, {
        get(target, property, receiver) {
          if (property !== "getPropertyValue") return Reflect.get(target, property, receiver);
          return (name: string) => {
            const value = target.getPropertyValue(name);
            if (value) return value;
            if (name === "box-sizing") return "border-box";
            if (["padding-bottom", "padding-top", "border-bottom-width", "border-top-width"].includes(name)) {
              return "0px";
            }
            return value;
          };
        },
      });
    });
    const onRun = vi.fn();
    const onRewind = vi.fn().mockResolvedValue({
      content: "target",
      sessionId: "session-rewind",
      sourceNodeId: "turn-target",
      rewindTurnId: "turn-target",
    });
    const { container } = render(<Harness onRun={onRun} onRewind={onRewind} />);

    await waitFor(() => expect(container.querySelectorAll(".user-bubble")).toHaveLength(3));
    const targetBubble = container.querySelectorAll<HTMLElement>(".user-bubble")[1];
    fireEvent.click(targetBubble);

    expect(screen.getByTestId("runtime-node-ids")).toHaveTextContent(
      "turn-root,turn-target,turn-descendant",
    );
    expect(screen.getByRole("textbox", { name: "编辑用户消息" })).toHaveValue("target");

    fireEvent.click(screen.getByRole("button", { name: "保存并重新生成" }));
    await waitFor(() => expect(onRun).toHaveBeenCalledTimes(1));

    expect(onRun).toHaveBeenCalledWith(expect.objectContaining({
      rewindTurnId: "turn-target",
      sourceNodeId: undefined,
      prompt: "target",
    }));
    expect(screen.getByTestId("runtime-node-ids")).toHaveTextContent("turn-root,turn-target");
    expect(screen.getByTestId("active-turn-id")).toHaveTextContent("turn-target");
    expect(screen.getByTestId("visible-message-text")).toHaveTextContent("root|root-answer|target");
    expect(screen.getByTestId("visible-message-text")).not.toHaveTextContent("descendant");
  });
});

describe("ChatPage queued message flushing", () => {
  it.each(["success", "paused", "failed"] as const)(
    "merges the persisted queue after a %s terminal",
    async (terminalStatus) => {
      const onRun = vi.fn();
      render(<QueueHarness terminalStatus={terminalStatus} onRun={onRun} />);

      fireEvent.click(screen.getByRole("button", { name: "结束当前 Turn" }));
      await waitFor(() => expect(onRun).toHaveBeenCalledTimes(1));

      expect(onRun).toHaveBeenCalledWith(expect.objectContaining({
        prompt: "第一条\n\n第二条",
        sourceNodeId: "turn-running",
        waitForActiveRun: true,
        references: [
          { source: "project", path: "README.md" },
          { source: "upload", path: "notes.txt" },
        ],
      }));
      await waitFor(() => expect(screen.getByTestId("queued-count")).toHaveTextContent("0"));
    },
  );

  it("keeps items added during submission for the next Turn", async () => {
    let releaseRun!: () => void;
    const runGate = new Promise<void>((resolve) => { releaseRun = resolve; });
    const onRun = vi.fn();
    render(<QueueHarness terminalStatus="success" onRun={onRun} runGate={runGate} />);

    fireEvent.click(screen.getByRole("button", { name: "结束当前 Turn" }));
    await waitFor(() => expect(onRun).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByTestId("queued-count")).toHaveTextContent("0"));

    fireEvent.click(screen.getByRole("button", { name: "提交期间新增队列项" }));
    expect(screen.getByTestId("queued-count")).toHaveTextContent("1");
    expect(onRun).toHaveBeenCalledTimes(1);

    await act(async () => releaseRun());
    await waitFor(() => expect(onRun).toHaveBeenCalledTimes(2));
    expect(onRun.mock.calls[1][0]).toEqual(expect.objectContaining({
      prompt: "提交期间新增",
      waitForActiveRun: true,
    }));
  });
});
