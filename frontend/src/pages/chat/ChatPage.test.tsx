import { App as AntApp, Modal } from "antd";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { projectTurnPath } from "../../app/runtime/runtimeDetailProjection";
import { TURN_PROTOCOL_VERSION } from "../../app/runtime/runtimeNodeNormalization";
import type { QueuedMessage } from "../../app/types";
import {
  compactTurn,
  getSessionNodes,
  listAgentThreadChildren,
  patchRuntimeConfig,
  sendAgentThreadMessage,
  steerTurn,
  streamAgentThread,
  type ProviderConfig,
} from "../../api";
import type {
  ChatMessage,
  ChatMode,
  Conversation,
  ReasoningEffort,
  RuntimeRootNode,
  RuntimeStateNode,
  TodoStatus,
  ToolEvent,
} from "../../types";
import ChatPage, { CHAT_COMPACT_WIDTH, composerAction } from "./ChatPage";

vi.mock("../../api", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../api")>(),
  compactTurn: vi.fn(),
  getSessionNodes: vi.fn(),
  listAgentThreadChildren: vi.fn(),
  patchRuntimeConfig: vi.fn(),
  sendAgentThreadMessage: vi.fn(),
  steerTurn: vi.fn().mockResolvedValue(undefined),
  streamAgentThread: vi.fn(),
}));

function turn(id: string, userText: string, parent?: RuntimeStateNode): RuntimeStateNode {
  return {
    thread_id: "session-rewind",
    parent_thread_id: parent?.thread_id ?? "",
    session_id: "session-rewind",
    parent_session_id: parent?.session_id ?? "",
    id,
    parent_id: parent?.id ?? "",
    version: TURN_PROTOCOL_VERSION,
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
    project_cwd: "",
    timestamp: `2026-08-26T00:00:0${parent ? 1 : 0}Z`,
    status: "success",
    current_data_idx: 0,
    data: [[
      { role: "user", content: [{ type: "text", text: userText, status: "success" }] },
      { role: "assistant", content: [{ type: "text", text: `${userText}-answer`, status: "success" }] },
    ]],
  };
}

function Harness({
  onRun,
  onRewind,
  onReload = vi.fn(),
}: {
  onRun: ReturnType<typeof vi.fn>;
  onRewind: ReturnType<typeof vi.fn>;
  onReload?: ReturnType<typeof vi.fn>;
}) {
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
        onReload={onReload}
        onRun={async (request) => { onRun(request); }}
      />
      <output data-testid="runtime-node-ids">{conversation.runtimeNodes?.map((node) => node.id).join(",")}</output>
      <output data-testid="active-turn-id">{conversation.activeTurnId}</output>
      <output data-testid="visible-message-text">{conversation.messages.map((message) => message.content).join("|")}</output>
    </AntApp>
  );
}

function SubagentHarness({ onRun = vi.fn() }: { onRun?: ReturnType<typeof vi.fn> }) {
  const root = turn("turn-root-agent", "root task");
  root.status = "running";
  const child = {
    ...turn("turn-child-agent", "child task", root),
    thread_id: "thread-child-agent",
    parent_thread_id: root.thread_id,
    status: "running" as const,
  };
  const [conversation, setConversation] = useState<Conversation>({
    id: "session-rewind",
    sessionId: "session-rewind",
    threadId: "session-rewind",
    title: "Agent Threads",
    runtimeNodes: [root, child],
    activeTurnId: root.id,
    lastNodeId: root.id,
    messagesLoaded: true,
    messages: projectTurnPath(
      new Map([[`${root.session_id}:${root.id}`, root]]),
      root.id,
    ),
  });
  return (
    <AntApp>
      <ChatPage
        conversation={conversation}
        agentThreadNavigation
        running
        onUpdate={(_id, updater) => setConversation((current) => updater(current))}
        onNew={async () => conversation.id}
        onNavigate={() => undefined}
        onRun={async (request) => { onRun(request); }}
        onRewind={vi.fn()}
        onFork={vi.fn()}
      />
      <output data-testid="subagent-canonical-thread">{conversation.threadId}</output>
      <output data-testid="subagent-canonical-active">{conversation.activeTurnId}</output>
    </AntApp>
  );
}

function QueueHarness({
  terminalStatus,
  onRun,
  runGate,
  sandboxHealth,
}: {
  terminalStatus: RuntimeStateNode["status"];
  onRun: ReturnType<typeof vi.fn>;
  runGate?: Promise<void>;
  sandboxHealth?: { phase: "checking" | "healthy" | "unhealthy"; detail: string | null };
}) {
  const [node, setNode] = useState(() => {
    const value = turn("turn-running", "running");
    value.status = "running";
    return value;
  });
  const [queued, setQueued] = useState<QueuedMessage[]>([
    {
      id: "queued-1",
      thread_id: "session-rewind",
      content: "第一条",
      references: [{ source: "project", path: "README.md" }],
      state: "pending",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    },
    {
      id: "queued-2",
      thread_id: "session-rewind",
      content: "第二条",
      references: [
        { source: "project", path: "README.md" },
        { source: "upload", path: "notes.txt" },
      ],
      state: "pending",
      created_at: "2026-01-01T00:00:01Z",
      updated_at: "2026-01-01T00:00:01Z",
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
        onQueuedMessagesRefresh={async () => {
          const hasAcknowledgedDelivery = node.data[node.current_data_idx]
            .some((item) => item.role === "user" && typeof item.delivery_id === "string");
          if (hasAcknowledgedDelivery) {
            setQueued((current) => current.filter((item) => item.state !== "dispatched"));
            return;
          }
          const calls = vi.mocked(steerTurn).mock.calls;
          const latest = calls[calls.length - 1];
          const dispatchedIds = new Set(latest?.[2] ?? []);
          setQueued((current) => current.map((item) => dispatchedIds.has(item.id)
            ? { ...item, state: "dispatched" }
            : item));
        }}
        onUpdate={() => undefined}
        onNew={async () => conversation.id}
        onNavigate={() => undefined}
        onEnsureSession={async () => conversation.sessionId!}
        onRun={async (request) => {
          onRun(request);
          const accepted = turn("turn-queued", request.prompt ?? "");
          if (request.queuedDelivery) {
            accepted.data[0][0].delivery_id = request.queuedDelivery.deliveryId;
            const submitted = new Set(request.queuedDelivery.messageIds);
            setQueued((current) => current.filter((item) => !submitted.has(item.id)));
          }
          accepted.status = "running";
          setNode(accepted);
          request.onBaseline?.(accepted);
          if (runGate) {
            await runGate;
            setNode((current) => ({ ...current, status: "success" }));
          }
        }}
        sandboxHealth={sandboxHealth}
      />
      <button type="button" onClick={() => setNode((current) => ({ ...current, status: terminalStatus }))}>
        结束当前 Turn
      </button>
      <button type="button" onClick={() => setQueued((current) => [
        ...current,
        {
          id: "queued-during-submit",
          thread_id: "session-rewind",
          content: "提交期间新增",
          references: [],
          state: "pending",
          created_at: "2026-01-01T00:00:02Z",
          updated_at: "2026-01-01T00:00:02Z",
        },
      ])}>
        提交期间新增队列项
      </button>
      <button type="button" onClick={() => setNode((current) => {
        const data = structuredClone(current.data);
        data[current.current_data_idx].push({
          role: "user",
          delivery_id: "delivery-1",
          content: [{ type: "text", text: "第一条", status: "success" }],
        });
        return { ...current, data };
      })}>
        确认 steering
      </button>
      <output data-testid="queued-count">{queued.length}</output>
    </AntApp>
  );
}

function ConfigHarness({
  providerConfig,
  status = "running",
}: {
  providerConfig?: ProviderConfig;
  status?: RuntimeStateNode["status"];
} = {}) {
  const [mode, setMode] = useState<ChatMode>("agent");
  const [conversation, setConversation] = useState<Conversation>(() => {
    const node = turn("turn-config", "configure");
    node.status = status;
    return {
      id: node.session_id,
      sessionId: node.session_id,
      threadId: node.thread_id,
      title: "config",
      runtimeNodes: [node],
      activeTurnId: node.id,
      lastNodeId: node.id,
      messagesLoaded: true,
      messages: projectTurnPath(new Map([[`${node.session_id}:${node.id}`, node]]), node.id),
    };
  });
  return (
    <AntApp>
      <ChatPage
        conversation={conversation}
        running={status === "running"}
        providerConfig={providerConfig}
        mode={mode}
        onModeChange={setMode}
        onUpdate={(_id, updater) => setConversation((current) => updater(current))}
        onNew={async () => conversation.id}
        onNavigate={() => undefined}
        onEnsureSession={async () => conversation.sessionId!}
        onRun={async () => undefined}
      />
      <output data-testid="selected-mode">{mode}</output>
    </AntApp>
  );
}

function NewConversationTitleHarness({ onRun }: { onRun: ReturnType<typeof vi.fn> }) {
  const [conversation, setConversation] = useState<Conversation>({
    id: "session-title",
    sessionId: "session-title",
    threadId: "session-title",
    title: "新对话",
    runtimeNodes: [],
    messagesLoaded: true,
    messages: [],
  });
  return (
    <AntApp>
      <ChatPage
        conversation={conversation}
        onUpdate={(_id, updater) => setConversation((current) => updater(current))}
        onNew={async () => conversation.id}
        onNavigate={() => undefined}
        onEnsureSession={async () => conversation.sessionId!}
        onRun={async (request) => { onRun(request); }}
      />
      <output data-testid="conversation-title">{conversation.title}</output>
    </AntApp>
  );
}

function scrollMessage(id: string, role: ChatMessage["role"], content: string): ChatMessage {
  return { id, role, content, events: [] };
}

function ScrollHarness({
  conversationId = "session-scroll",
  messages,
}: {
  conversationId?: string;
  messages: ChatMessage[];
}) {
  const conversation: Conversation = {
    id: conversationId,
    sessionId: conversationId,
    threadId: conversationId,
    title: "scroll",
    messagesLoaded: true,
    messages,
  };
  return (
    <AntApp>
      <ChatPage
        conversation={conversation}
        onUpdate={() => undefined}
        onNew={async () => conversation.id}
        onNavigate={() => undefined}
        onEnsureSession={async () => conversation.sessionId!}
        onRun={async () => undefined}
      />
    </AntApp>
  );
}

function TodoHarness({ status, running }: { status: TodoStatus; running: boolean }) {
  const todoEvent: ToolEvent = {
    kind: "tool_call",
    message: "todo_write",
    data: {
      tool: "todo_write",
      call_id: "todo-call",
      arguments: { todos: [{ content: "完成 Todo 面板", status }] },
    },
  };
  const conversation: Conversation = {
    id: "session-todo",
    sessionId: "session-todo",
    threadId: "session-todo",
    title: "todo",
    messagesLoaded: true,
    messages: [{ id: "assistant-todo", role: "assistant", content: "", events: [todoEvent] }],
  };
  return (
    <AntApp>
      <ChatPage
        conversation={conversation}
        running={running}
        onUpdate={() => undefined}
        onNew={async () => conversation.id}
        onNavigate={() => undefined}
        onEnsureSession={async () => conversation.sessionId!}
        onRun={async () => undefined}
      />
    </AntApp>
  );
}

interface ScrollMetrics {
  scrollHeight: number;
  clientHeight: number;
  scrollTop: number;
}

function mockScrollContainer(scrollContainer: HTMLDivElement, metrics: ScrollMetrics) {
  Object.defineProperties(scrollContainer, {
    scrollHeight: { configurable: true, get: () => metrics.scrollHeight },
    clientHeight: { configurable: true, get: () => metrics.clientHeight },
    scrollTop: {
      configurable: true,
      get: () => metrics.scrollTop,
      set: (value: number) => {
        metrics.scrollTop = Math.max(0, Math.min(value, Math.max(0, metrics.scrollHeight - metrics.clientHeight)));
      },
    },
  });
  const scrollTo = vi.fn((options: ScrollToOptions) => {
    if (typeof options.top === "number") scrollContainer.scrollTop = options.top;
    fireEvent.scroll(scrollContainer);
  });
  Object.defineProperty(scrollContainer, "scrollTo", { configurable: true, value: scrollTo });
  return scrollTo;
}

describe("ChatPage bottom anchoring", () => {
  it("shows the return button only beyond the 24px bottom threshold and scrolls smoothly on click", () => {
    render(<ScrollHarness messages={[scrollMessage("assistant-1", "assistant", "answer")]} />);
    const scrollContainer = document.querySelector<HTMLDivElement>("[data-conversation-scroll]")!;
    const metrics = { scrollHeight: 1000, clientHeight: 600, scrollTop: 376 };
    const scrollTo = mockScrollContainer(scrollContainer, metrics);

    fireEvent.scroll(scrollContainer);
    expect(screen.queryByRole("button", { name: "滚动到底部" })).not.toBeInTheDocument();

    metrics.scrollTop = 375;
    fireEvent.scroll(scrollContainer);
    const button = screen.getByRole("button", { name: "滚动到底部" });
    expect(button).toBeVisible();

    fireEvent.click(button);
    expect(scrollTo).toHaveBeenCalledWith({ top: 1000, behavior: "smooth" });
    expect(metrics.scrollTop).toBe(400);
    expect(screen.queryByRole("button", { name: "滚动到底部" })).not.toBeInTheDocument();
  });

  it("follows message growth only while the reader remains at the bottom", () => {
    const firstMessages = [scrollMessage("assistant-1", "assistant", "first")];
    const view = render(<ScrollHarness messages={firstMessages} />);
    const scrollContainer = document.querySelector<HTMLDivElement>("[data-conversation-scroll]")!;
    const metrics = { scrollHeight: 1000, clientHeight: 600, scrollTop: 400 };
    mockScrollContainer(scrollContainer, metrics);
    fireEvent.scroll(scrollContainer);

    metrics.scrollHeight = 1200;
    view.rerender(<ScrollHarness messages={[...firstMessages, scrollMessage("assistant-2", "assistant", "streaming")]} />);
    expect(metrics.scrollTop).toBe(600);

    metrics.scrollTop = 300;
    fireEvent.scroll(scrollContainer);
    expect(screen.getByRole("button", { name: "滚动到底部" })).toBeVisible();

    metrics.scrollHeight = 1400;
    view.rerender(<ScrollHarness messages={[
      ...firstMessages,
      scrollMessage("assistant-2", "assistant", "streaming update"),
      scrollMessage("user-2", "user", "sent while reading above"),
    ]} />);
    expect(metrics.scrollTop).toBe(300);
    expect(screen.getByRole("button", { name: "滚动到底部" })).toBeVisible();
  });

  it("keeps pinned content at the bottom when its rendered height changes", () => {
    const originalResizeObserver = window.ResizeObserver;
    let contentResizeCallback: ResizeObserverCallback | undefined;
    class MockResizeObserver {
      constructor(private readonly callback: ResizeObserverCallback) {}
      observe = (target: Element) => {
        if (target.matches(".chat-scroll-content")) contentResizeCallback = this.callback;
      };
      unobserve() {}
      disconnect() {}
    }
    window.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;
    const view = render(<ScrollHarness messages={[scrollMessage("assistant-1", "assistant", "streaming")]} />);
    try {
      const scrollContainer = document.querySelector<HTMLDivElement>("[data-conversation-scroll]")!;
      const metrics = { scrollHeight: 1000, clientHeight: 600, scrollTop: 400 };
      mockScrollContainer(scrollContainer, metrics);
      fireEvent.scroll(scrollContainer);

      metrics.scrollHeight = 1250;
      act(() => contentResizeCallback?.([], {} as ResizeObserver));
      expect(metrics.scrollTop).toBe(650);
      expect(screen.queryByRole("button", { name: "滚动到底部" })).not.toBeInTheDocument();
    } finally {
      view.unmount();
      window.ResizeObserver = originalResizeObserver;
    }
  });

  it("resets the scroll anchor when switching conversations", () => {
    const view = render(<ScrollHarness conversationId="session-a" messages={[scrollMessage("a", "assistant", "a")]} />);
    const scrollContainer = document.querySelector<HTMLDivElement>("[data-conversation-scroll]")!;
    const metrics = { scrollHeight: 1000, clientHeight: 600, scrollTop: 200 };
    mockScrollContainer(scrollContainer, metrics);
    fireEvent.scroll(scrollContainer);
    expect(screen.getByRole("button", { name: "滚动到底部" })).toBeVisible();

    metrics.scrollHeight = 1200;
    view.rerender(<ScrollHarness conversationId="session-b" messages={[scrollMessage("b", "assistant", "b")]} />);
    expect(metrics.scrollTop).toBe(600);
    expect(screen.queryByRole("button", { name: "滚动到底部" })).not.toBeInTheDocument();
  });
});

describe("ChatPage Todo panel lifecycle", () => {
  it("keeps an incomplete running Todo expanded without a close action", () => {
    render(<TodoHarness status="in_progress" running />);

    expect(screen.getByText("任务清单")).toBeVisible();
    expect(screen.getByText("完成 Todo 面板")).toBeVisible();
    expect(screen.queryByRole("button", { name: "关闭任务清单" })).not.toBeInTheDocument();
    expect(document.querySelector(".composer")).toHaveClass("has-todo");
  });

  it("removes the panel and layout space as soon as every Todo completes", () => {
    const view = render(<TodoHarness status="in_progress" running />);
    expect(screen.getByText("任务清单")).toBeVisible();

    view.rerender(<TodoHarness status="completed" running />);

    expect(screen.queryByText("任务清单")).not.toBeInTheDocument();
    expect(document.querySelector(".composer")).not.toHaveClass("has-todo");
  });

  it("resets a user-open panel to collapsed when its incomplete Turn ends", () => {
    const view = render(<TodoHarness status="in_progress" running />);
    const header = screen.getByText("任务清单").closest(".ant-collapse-header");
    expect(header).not.toBeNull();
    fireEvent.click(header!);
    fireEvent.click(header!);
    expect(screen.getByText("完成 Todo 面板")).toBeVisible();

    view.rerender(<TodoHarness status="in_progress" running={false} />);

    expect(screen.queryByText("完成 Todo 面板")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "关闭任务清单" })).toBeVisible();
  });

  it("offers manual cleanup only after an incomplete Turn ends", () => {
    render(<TodoHarness status="pending" running={false} />);

    expect(screen.getByText("任务清单")).toBeVisible();
    expect(screen.queryByText("完成 Todo 面板")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "关闭任务清单" }));

    expect(screen.queryByText("任务清单")).not.toBeInTheDocument();
    expect(document.querySelector(".composer")).not.toHaveClass("has-todo");
  });
});

describe("ChatPage rewind projection", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.mocked(compactTurn).mockReset();
    vi.mocked(patchRuntimeConfig).mockReset();
  });

  it("keeps the default title while the backend generates the first-message title", async () => {
    const onRun = vi.fn();
    render(<NewConversationTitleHarness onRun={onRun} />);

    await userEvent.type(screen.getByLabelText("聊天输入"), "这是一个超过十八字符的首条用户消息内容");
    await userEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => expect(onRun).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId("conversation-title")).toHaveTextContent("新对话");
  });

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

  it("shows an accessible shimmer while Compact is pending and reloads on success", async () => {
    const user = userEvent.setup();
    const onReload = vi.fn().mockResolvedValue(undefined);
    let resolveCompact!: (value: RuntimeStateNode) => void;
    vi.mocked(compactTurn).mockReturnValue(new Promise((resolve) => { resolveCompact = resolve; }));
    render(<Harness onRun={vi.fn()} onRewind={vi.fn()} onReload={onReload} />);

    await user.type(screen.getByLabelText("聊天输入"), "/compact");
    await user.click(screen.getByRole("button", { name: "发送" }));

    const status = await screen.findByText("正在执行compaction操作中");
    const progress = status.closest(".runtime-compaction-progress");
    expect(progress).not.toBeNull();
    expect(progress).toHaveAttribute("role", "status");
    expect(progress).toHaveAttribute("aria-live", "polite");
    expect(status).toHaveTextContent("正在执行compaction操作中");
    expect(progress?.querySelector(".shimmer-text.is-active")).not.toBeNull();
    expect(vi.mocked(compactTurn)).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText("聊天输入")).toHaveAttribute("contenteditable", "false");
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "发送" }));
    expect(vi.mocked(compactTurn)).toHaveBeenCalledTimes(1);

    resolveCompact(turn("turn-compact", "compact"));
    await waitFor(() => expect(screen.queryByText("正在执行compaction操作中")).toBeNull());
    expect(onReload).toHaveBeenCalledWith("session-rewind", "turn-compact");
    expect(vi.mocked(compactTurn)).toHaveBeenCalledTimes(1);
  });

  it("removes the Compact shimmer and surfaces the request failure", async () => {
    const user = userEvent.setup();
    vi.mocked(compactTurn).mockRejectedValue(new Error("summary provider failed"));
    render(<Harness onRun={vi.fn()} onRewind={vi.fn()} />);

    await user.type(screen.getByLabelText("聊天输入"), "/compact");
    await user.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => expect(screen.queryByText("正在执行compaction操作中")).toBeNull());
    expect(screen.getByText("⚠️ 压缩失败：summary provider failed", { selector: "p" })).toBeVisible();
  });
});

describe("ChatPage running Turn configuration", () => {
  afterEach(() => {
    Modal.destroyAll();
    vi.mocked(patchRuntimeConfig).mockReset();
  });

  it("optimistically patches mode and disables only that Select while pending", async () => {
    const user = userEvent.setup();
    let resolvePatch!: (node: RuntimeStateNode) => void;
    vi.mocked(patchRuntimeConfig).mockReturnValue(new Promise((resolve) => { resolvePatch = resolve; }));
    render(<ConfigHarness />);

    await user.click(screen.getByRole("combobox", { name: "运行模式" }));
    await user.click(await screen.findByRole("option", { name: /Plan/ }));

    expect(screen.getByTestId("selected-mode")).toHaveTextContent("plan");
    expect(screen.getByRole("combobox", { name: "运行模式" })).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "权限模式" })).not.toBeDisabled();
    expect(screen.getByRole("combobox", { name: "思考等级" })).not.toBeDisabled();
    expect(patchRuntimeConfig).toHaveBeenCalledWith("session-rewind", expect.objectContaining({
      node_id: "turn-config",
      running_mode: "plan",
    }));

    const accepted = turn("turn-config", "configure");
    accepted.status = "running";
    accepted.running_mode = "plan";
    await act(async () => resolvePatch(accepted));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "运行模式" })).not.toBeDisabled());
    expect(screen.getByTestId("selected-mode")).toHaveTextContent("plan");
  });

  it("syncs current Provider model parameters only into a running Turn", async () => {
    const providerConfig: ProviderConfig = {
      id: "provider-local",
      is_active: true,
      provider_name: "local",
      protocol: "chat_completions",
      base_url: "https://example.test/v1",
      model: "configured-model",
      max_tokens: 1536,
      context_size: 65536,
      temperature: 0.7,
      tokenizer_model: "",
      api_key_configured: true,
    };
    vi.mocked(patchRuntimeConfig).mockImplementation(async (_sessionId, patch) => {
      const accepted = turn("turn-config", "configure");
      accepted.status = "running";
      accepted.provider_name = patch.provider_name ?? accepted.provider_name;
      accepted.model = { ...accepted.model, ...patch.model };
      return accepted;
    });

    const runningView = render(<ConfigHarness providerConfig={providerConfig} />);
    await waitFor(() => expect(patchRuntimeConfig).toHaveBeenCalledWith(
      "session-rewind",
      expect.objectContaining({
        node_id: "turn-config",
        provider_name: "local",
        model: expect.objectContaining({
          current_model: "configured-model",
          output_length: 1536,
          context_length: 65536,
          temperature: 0.7,
        }),
      }),
    ));
    runningView.unmount();
    vi.mocked(patchRuntimeConfig).mockClear();

    render(<ConfigHarness providerConfig={providerConfig} status="success" />);
    await waitFor(() => expect(patchRuntimeConfig).not.toHaveBeenCalled());
  });

  it("rolls back only the failed field", async () => {
    const user = userEvent.setup();
    vi.mocked(patchRuntimeConfig).mockRejectedValue(new Error("config write failed"));
    render(<ConfigHarness />);

    await user.click(screen.getByRole("combobox", { name: "运行模式" }));
    await user.click(await screen.findByRole("option", { name: /Plan/ }));

    await waitFor(() => expect(screen.getByRole("combobox", { name: "运行模式" })).not.toBeDisabled());
    expect(screen.getByTestId("selected-mode")).toHaveTextContent("agent");
    expect(screen.getByText(/运行配置更新失败：config write failed/)).toBeVisible();
    expect(screen.getByRole("combobox", { name: "权限模式" })).not.toBeDisabled();
  });

  it("keeps reasoning reconciliation independent from an older mode response", async () => {
    const user = userEvent.setup();
    let resolveMode!: (node: RuntimeStateNode) => void;
    vi.mocked(patchRuntimeConfig).mockImplementation(async (_sessionId, values) => {
      if (values.running_mode) return new Promise((resolve) => { resolveMode = resolve; });
      const accepted = turn("turn-config", "configure");
      accepted.status = "running";
      accepted.model.reasoning_effort = "high";
      return accepted;
    });
    render(<ConfigHarness />);

    await user.click(screen.getByRole("combobox", { name: "运行模式" }));
    await user.click(await screen.findByRole("option", { name: /Plan/ }));
    await user.click(screen.getByRole("combobox", { name: "思考等级" }));
    await user.click(await screen.findByRole("option", { name: "high" }));

    await waitFor(() => expect(patchRuntimeConfig).toHaveBeenCalledWith(
      "session-rewind",
      expect.objectContaining({ model: { reasoning_effort: "high" } }),
    ));
    expect(screen.getByRole("combobox", { name: "思考等级" }).closest(".ant-select")).toHaveTextContent("high");

    const modeAccepted = turn("turn-config", "configure");
    modeAccepted.status = "running";
    modeAccepted.running_mode = "plan";
    await act(async () => resolveMode(modeAccepted));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "运行模式" })).not.toBeDisabled());
    expect(screen.getByRole("combobox", { name: "思考等级" }).closest(".ant-select")).toHaveTextContent("high");
  });

  it("requires Full access confirmation and sends the acknowledgement", async () => {
    const user = userEvent.setup();
    const accepted = turn("turn-config", "configure");
    accepted.status = "running";
    accepted.permission_mode = "full_access";
    vi.mocked(patchRuntimeConfig).mockResolvedValue(accepted);
    render(<ConfigHarness />);

    await user.click(screen.getByRole("combobox", { name: "权限模式" }));
    await user.click(await screen.findByRole("option", { name: "完全访问" }));
    expect((await screen.findAllByText("启用 Full access？")).length).toBeGreaterThan(0);
    await user.click(screen.getByRole("button", { name: /继\s*续/ }));

    await waitFor(() => expect(patchRuntimeConfig).toHaveBeenCalledWith(
      "session-rewind",
      expect.objectContaining({ permission_mode: "full_access", full_access_acknowledged: true }),
    ));
  });
});

describe("ChatPage queued message flushing", () => {
  it("blocks Agent controls and shows a temporary non-persisted failure bubble", async () => {
    const onRun = vi.fn();
    render(
      <QueueHarness
        terminalStatus="success"
        onRun={onRun}
        sandboxHealth={{ phase: "unhealthy", detail: "Broker service stopped" }}
      />,
    );

    expect(document.querySelector(".sandbox-health-failure")).toHaveTextContent("沙箱 Broker 不可用：Broker service stopped");
    expect(screen.getByLabelText("聊天输入")).toHaveAttribute("contenteditable", "false");
    expect(screen.getByRole("combobox", { name: "运行模式" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "发送第 1 条待发送消息" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "暂停" })).toBeDisabled();
    expect(screen.queryByText("沙箱 Broker 不可用：Broker service stopped", { selector: ".message.user *" })).toBeNull();
    expect(onRun).not.toHaveBeenCalled();
  });

  it.each(["success", "failed"] as const)(
    "merges the persisted queue after a %s terminal",
    async (terminalStatus) => {
      const onRun = vi.fn();
      render(<QueueHarness terminalStatus={terminalStatus} onRun={onRun} />);

      fireEvent.click(screen.getByRole("button", { name: "结束当前 Turn" }));
      await waitFor(() => expect(onRun).toHaveBeenCalledTimes(1));

      expect(onRun).toHaveBeenCalledWith(expect.objectContaining({
        prompt: null,
        sourceNodeId: "turn-running",
        waitForActiveRun: true,
        queuedDelivery: {
          deliveryId: expect.any(String),
          messageIds: ["queued-1", "queued-2"],
        },
      }));
      await waitFor(() => expect(screen.getByTestId("queued-count")).toHaveTextContent("0"));
    },
  );

  it("keeps the queue local when a Turn becomes paused", async () => {
    const onRun = vi.fn();
    render(<QueueHarness terminalStatus="paused" onRun={onRun} />);

    fireEvent.click(screen.getByRole("button", { name: "结束当前 Turn" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "继续" })).toBeEnabled());
    expect(onRun).not.toHaveBeenCalled();
    expect(screen.getByTestId("queued-count")).toHaveTextContent("2");
  });

  it("sends one queued entry to the running Turn and waits for SSE acknowledgement", async () => {
    render(<QueueHarness terminalStatus="success" onRun={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "发送第 1 条待发送消息" }));
    await waitFor(() => expect(vi.mocked(steerTurn)).toHaveBeenCalledWith(
      "turn-running",
      expect.any(String),
      ["queued-1"],
    ));
    expect(screen.getByTestId("queued-count")).toHaveTextContent("2");
    expect(screen.getByRole("button", { name: "发送第 1 条待发送消息" })).toBeDisabled();
    expect(screen.getByText(/发送中/)).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "确认 steering" }));
    await waitFor(() => expect(screen.getByTestId("queued-count")).toHaveTextContent("1"));
  });

  it("uses Pause to merge all unsent entries into one steering input", async () => {
    render(<QueueHarness terminalStatus="success" onRun={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "暂停" }));
    await waitFor(() => expect(vi.mocked(steerTurn)).toHaveBeenCalledWith(
      "turn-running",
      expect.any(String),
      ["queued-1", "queued-2"],
    ));
    expect(screen.getAllByText(/发送中/)).toHaveLength(2);
  });

  it("keeps both draft and queue entry when edit is blocked", async () => {
    const user = userEvent.setup();
    render(<QueueHarness terminalStatus="success" onRun={vi.fn()} />);

    await user.type(screen.getByLabelText("聊天输入"), "existing draft");
    await user.click(screen.getByRole("button", { name: "编辑第 1 条待发送消息" }));

    expect(await screen.findByText("输入框有内容，无法修改队列消息")).toBeVisible();
    expect(screen.getByLabelText("聊天输入")).toHaveTextContent("existing draft");
    expect(screen.getByTestId("queued-count")).toHaveTextContent("2");
  });

  it("moves an editable queue entry back into an empty composer", async () => {
    const user = userEvent.setup();
    render(<QueueHarness terminalStatus="success" onRun={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: "编辑第 1 条待发送消息" }));
    expect(screen.getByLabelText("聊天输入")).toHaveTextContent("第一条");
    expect(screen.getByTestId("queued-count")).toHaveTextContent("2");
    expect(screen.getByRole("button", { name: "发送" })).toBeEnabled();
  });

  it("creates a child Turn for a paused Turn with a draft", async () => {
    const user = userEvent.setup();
    const onRun = vi.fn();
    render(<QueueHarness terminalStatus="paused" onRun={onRun} />);
    await user.click(screen.getByRole("button", { name: "结束当前 Turn" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "继续" })).toBeEnabled());
    await user.type(screen.getByLabelText("聊天输入"), "new child input");
    await user.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => expect(onRun).toHaveBeenCalledTimes(1));
    expect(onRun).toHaveBeenCalledWith(expect.objectContaining({
      prompt: "new child input",
      resume: false,
      sourceNodeId: "turn-running",
      waitForActiveRun: true,
    }));
  });

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
      prompt: null,
      waitForActiveRun: true,
      queuedDelivery: expect.objectContaining({ messageIds: ["queued-during-submit"] }),
    }));
  });
});

describe("ChatPage Trace navigation", () => {
  function renderConversation(conversation: Conversation) {
    return (
      <AntApp>
        <ChatPage
          conversation={conversation}
          onUpdate={() => undefined}
          onNew={async () => conversation.id}
          onNavigate={() => undefined}
          onEnsureSession={async () => conversation.sessionId!}
          onRun={async () => undefined}
        />
      </AntApp>
    );
  }

  it("hides the toolbar until the current Thread has an ordinary Turn", () => {
    const empty: Conversation = {
      id: "session-empty",
      sessionId: "session-empty",
      threadId: "session-empty",
      title: "新对话",
      runtimeNodes: [],
      messagesLoaded: true,
      messages: [],
    };
    const syntheticRoot: RuntimeRootNode = {
      session_id: "session-empty",
      thread_id: "session-empty",
      id: "turn-synthetic-root",
    };
    const { rerender } = render(renderConversation(empty));

    expect(screen.queryByRole("navigation", { name: "主内容视图" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("聊天输入")).toBeInTheDocument();

    rerender(renderConversation({ ...empty, runtimeNodes: [syntheticRoot] }));
    expect(screen.queryByRole("navigation", { name: "主内容视图" })).not.toBeInTheDocument();

    const node = turn("turn-first", "first");
    const populated = {
      ...empty,
      id: node.session_id,
      sessionId: node.session_id,
      threadId: node.thread_id,
      runtimeNodes: [node],
      activeTurnId: node.id,
      lastNodeId: node.id,
      messages: projectTurnPath(new Map([[`${node.session_id}:${node.id}`, node]]), node.id),
    };
    rerender(renderConversation(populated));

    expect(screen.getByRole("navigation", { name: "主内容视图" })).toBeInTheDocument();
    expect(screen.getByTitle(node.thread_id)).toHaveClass("trace-toolbar-thread-id");
  });

  it("returns to Chat when switching from Trace to an empty conversation", async () => {
    const populatedNode = turn("turn-populated", "populated");
    const populated: Conversation = {
      id: populatedNode.session_id,
      sessionId: populatedNode.session_id,
      threadId: populatedNode.thread_id,
      title: "populated",
      runtimeNodes: [populatedNode],
      activeTurnId: populatedNode.id,
      lastNodeId: populatedNode.id,
      messagesLoaded: true,
      messages: projectTurnPath(
        new Map([[`${populatedNode.session_id}:${populatedNode.id}`, populatedNode]]),
        populatedNode.id,
      ),
    };
    const empty: Conversation = {
      id: "session-empty",
      sessionId: "session-empty",
      threadId: "session-empty",
      title: "新对话",
      runtimeNodes: [],
      messagesLoaded: true,
      messages: [],
    };
    const { rerender } = render(renderConversation(populated));
    fireEvent.click(screen.getByRole("button", { name: "Trace" }));
    expect(screen.queryByLabelText("聊天输入")).not.toBeInTheDocument();

    rerender(renderConversation(empty));
    expect(screen.queryByRole("navigation", { name: "主内容视图" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("聊天输入")).toBeInTheDocument();

    const firstNode = {
      ...turn("turn-empty-first", "first"),
      session_id: empty.sessionId!,
      thread_id: empty.threadId!,
    };
    rerender(renderConversation({
      ...empty,
      runtimeNodes: [firstNode],
      activeTurnId: firstNode.id,
      lastNodeId: firstNode.id,
      messages: projectTurnPath(new Map([[`${firstNode.session_id}:${firstNode.id}`, firstNode]]), firstNode.id),
    }));

    await waitFor(() => expect(screen.getByRole("button", { name: "Chat" })).toHaveAttribute("aria-pressed", "true"));
    expect(screen.getByLabelText("聊天输入")).toBeInTheDocument();
  });

  it("shows the text toolbar and hides the Composer in Trace view", async () => {
    render(<Harness onRun={vi.fn()} onRewind={vi.fn()} />);

    expect(screen.getByRole("button", { name: "Thread" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Chat" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Trace" }));

    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Chat" }));
    expect(screen.getByRole("textbox")).toBeInTheDocument();
  });

  it("opens Trace through /trace without dispatching a chat run", async () => {
    const user = userEvent.setup();
    const onRun = vi.fn();
    render(<Harness onRun={onRun} onRewind={vi.fn()} />);

    await user.type(screen.getByLabelText("聊天输入"), "/trace");
    await user.click(screen.getByRole("button", { name: "发送" }));

    expect(onRun).not.toHaveBeenCalled();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Trace" })).toHaveAttribute("aria-pressed", "true");
  });

  it("keeps the Thread dropdown scoped to the current thread", async () => {
    const user = userEvent.setup();
    render(<Harness onRun={vi.fn()} onRewind={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: "Thread" }));

    expect(await screen.findByRole("menuitem", { name: "session-rewind" })).toBeInTheDocument();
    expect(screen.getAllByRole("menuitem")).toHaveLength(1);
  });

  it("switches the toolbar and runtime controls only below 700px", async () => {
    const user = userEvent.setup();
    const originalResizeObserver = window.ResizeObserver;
    let chatResizeCallback: ResizeObserverCallback | undefined;
    class MockResizeObserver {
      constructor(private readonly callback: ResizeObserverCallback) {}
      observe = (target: Element) => {
        if (target.matches(".chat-page")) chatResizeCallback = this.callback;
      };
      unobserve() {}
      disconnect() {}
    }
    window.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;
    vi.mocked(patchRuntimeConfig).mockImplementation(async (_sessionId, patch) => {
      const accepted = turn("turn-config", "configure");
      accepted.status = "running";
      accepted.running_mode = patch.running_mode ?? "agent";
      accepted.permission_mode = patch.permission_mode ?? "read_only";
      accepted.model.reasoning_effort = (patch.model?.reasoning_effort as ReasoningEffort | undefined) ?? "medium";
      return accepted;
    });
    const view = render(<ConfigHarness />);
    try {
      expect(CHAT_COMPACT_WIDTH).toBe(700);
      expect(screen.getByRole("combobox", { name: "运行模式" })).toBeInTheDocument();

      act(() => chatResizeCallback?.([
        { contentRect: { width: 699 } } as ResizeObserverEntry,
      ], {} as ResizeObserver));

      expect(screen.queryByRole("combobox", { name: "运行模式" })).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Thread" }).querySelector(".anticon-branches")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Chat" }).querySelector(".anticon-comment")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Trace" }).querySelector(".anticon-node-index")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "运行模式：Agent" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "权限模式：只读" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "思考等级：中" })).toBeInTheDocument();

      for (const [name, tooltip] of [
        ["Thread", "Thread：session-rewind"],
        ["Chat", "Chat"],
        ["Trace", "Trace"],
        ["运行模式：Agent", "运行模式：Agent"],
        ["权限模式：只读", "权限模式：只读"],
        ["思考等级：中", "思考等级：中"],
      ] as const) {
        const visibleTooltip = () => Array.from(document.querySelectorAll<HTMLElement>('[role="tooltip"]'))
          .find((element) => !element.closest(".ant-tooltip")?.classList.contains("ant-tooltip-hidden"));
        const button = screen.getByRole("button", { name });
        await user.hover(button);
        await waitFor(() => expect(visibleTooltip()).toHaveTextContent(tooltip));
        await user.unhover(button);
        await waitFor(() => expect(visibleTooltip()).toBeUndefined());
      }

      await user.click(screen.getByRole("button", { name: "运行模式：Agent" }));
      expect(await screen.findByRole("menuitem", { name: "Agent" })).toHaveClass("ant-dropdown-menu-item-selected");
      await user.click(screen.getByRole("menuitem", { name: "Plan" }));
      await waitFor(() => expect(patchRuntimeConfig).toHaveBeenCalledWith(
        "session-rewind",
        expect.objectContaining({ running_mode: "plan" }),
      ));

      await user.click(screen.getByRole("button", { name: "权限模式：只读" }));
      expect(await screen.findByRole("menuitem", { name: "只读" })).toHaveClass("ant-dropdown-menu-item-selected");
      await user.click(screen.getByRole("menuitem", { name: "工作区读写" }));
      await waitFor(() => expect(patchRuntimeConfig).toHaveBeenCalledWith(
        "session-rewind",
        expect.objectContaining({ permission_mode: "workspace_write" }),
      ));

      await user.click(screen.getByRole("button", { name: "思考等级：中" }));
      expect(await screen.findByRole("menuitem", { name: "中（medium）" })).toHaveClass("ant-dropdown-menu-item-selected");
      await user.click(screen.getByRole("menuitem", { name: "高（high）" }));
      await waitFor(() => expect(patchRuntimeConfig).toHaveBeenCalledWith(
        "session-rewind",
        expect.objectContaining({ model: { reasoning_effort: "high" } }),
      ));

      act(() => chatResizeCallback?.([
        { contentRect: { width: 700 } } as ResizeObserverEntry,
      ], {} as ResizeObserver));

      expect(screen.getByRole("combobox", { name: "运行模式" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Thread" })).toHaveTextContent("Thread");
    } finally {
      view.unmount();
      window.ResizeObserver = originalResizeObserver;
      vi.mocked(patchRuntimeConfig).mockReset();
    }
  });
});

describe("ChatPage Agent Thread navigation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getSessionNodes).mockResolvedValue([]);
    vi.mocked(listAgentThreadChildren).mockImplementation(async (_sessionId, threadId) => (
      threadId === "session-rewind"
        ? [{
            thread_id: "thread-child-agent",
            thread_path: "/root/worker",
            thread_task: "child task",
            thread_status: "opening",
          }]
        : []
    ));
    vi.mocked(streamAgentThread).mockImplementation((_sessionId, _threadId, onEvent, signal) => {
      onEvent({ type: "thread.ready", session_id: "session-rewind", thread_id: "thread-child-agent" });
      return new Promise<"aborted">((resolve) => {
        signal.addEventListener("abort", () => resolve("aborted"), { once: true });
      });
    });
    vi.mocked(sendAgentThreadMessage).mockResolvedValue({
      delivery_id: "delivery-child",
      accepted: true,
      target_state: "running",
      turn_id: "turn-child-agent",
    });
  });

  async function selectChild(user: ReturnType<typeof userEvent.setup>) {
    await user.click(screen.getByRole("button", { name: "Thread" }));
    const tree = await screen.findByRole("tree", { name: "Agent Thread 树" });
    const root = within(tree).getByText("root").closest('[role="treeitem"]')!;
    fireEvent.click(root.querySelector(".ant-tree-switcher")!);
    await user.click(await within(tree).findByText("worker · opening"));
    await waitFor(() => expect(streamAgentThread).toHaveBeenCalledTimes(1));
  }

  it("keeps Chat, Trace, and the send-only Composer on the selected Subagent", async () => {
    const user = userEvent.setup();
    const onRun = vi.fn();
    render(<SubagentHarness onRun={onRun} />);
    expect(screen.getByRole("button", { name: "暂停" })).toBeInTheDocument();

    await selectChild(user);
    expect(screen.getByTitle("thread-child-agent")).toBeInTheDocument();
    expect(screen.getByText("child task")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "暂停" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "编辑" })).not.toBeInTheDocument();
    expect(screen.getByTestId("subagent-canonical-thread")).toHaveTextContent("session-rewind");
    expect(screen.getByTestId("subagent-canonical-active")).toHaveTextContent("turn-root-agent");

    await user.click(screen.getByRole("button", { name: "Trace" }));
    expect(screen.getByTitle("thread-child-agent")).toBeInTheDocument();
    expect(screen.queryByLabelText("聊天输入")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Chat" }));

    await user.type(screen.getByLabelText("聊天输入"), "follow up");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(sendAgentThreadMessage).toHaveBeenCalledWith(
      "thread-child-agent",
      expect.objectContaining({
        sessionId: "session-rewind",
        content: "follow up",
      }),
    ));
    expect(onRun).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Thread" }));
    await user.click(await screen.findByText("root"));
    await waitFor(() => expect(screen.getByRole("button", { name: "暂停" })).toBeInTheDocument());
  });

  it("restores the Subagent draft and displays the API failure", async () => {
    vi.mocked(sendAgentThreadMessage).mockRejectedValueOnce(new Error("redis offline"));
    const user = userEvent.setup();
    render(<SubagentHarness />);
    await selectChild(user);

    const composer = screen.getByLabelText("聊天输入");
    await user.type(composer, "keep this draft");
    await user.click(screen.getByRole("button", { name: "发送" }));

    expect(await screen.findByText("Agent 消息发送失败：redis offline")).toBeInTheDocument();
    expect(composer).toHaveTextContent("keep this draft");
  });

  it("keeps the non-navigation Thread control from loading the Agent tree", async () => {
    const user = userEvent.setup();
    render(<Harness onRun={vi.fn()} onRewind={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: "Thread" }));
    expect(screen.queryByRole("tree", { name: "Agent Thread 树" })).not.toBeInTheDocument();
    expect(listAgentThreadChildren).not.toHaveBeenCalled();
  });
});

describe("ChatPage composer action matrix", () => {
  it.each([
    ["running", true, "send", false],
    ["running", false, "pause", false],
    ["paused", true, "send", false],
    ["paused", false, "resume", false],
    ["success", true, "send", false],
    ["success", false, "send", true],
    ["failed", true, "send", false],
    ["failed", false, "send", true],
    [undefined, true, "send", false],
    [undefined, false, "send", true],
  ] as const)("derives %s × draft=%s", (status, hasDraft, mode, disabled) => {
    expect(composerAction(status, hasDraft)).toEqual({ mode, disabled });
  });

  it("disables every action while uploads are in progress", () => {
    expect(composerAction("running", true, true)).toEqual({ mode: "send", disabled: true });
    expect(composerAction("paused", false, true)).toEqual({ mode: "resume", disabled: true });
  });
});
