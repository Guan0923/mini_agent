import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import ChatPage from "./ChatPage";
import { copyText } from "./chat/messageParts";
import type { ChatMode, Conversation, PermissionMode, ReasoningEffort, RuntimeConfigModel, StreamMessage } from "../types";
import type { ProviderConfig } from "../api";

const mocks = vi.hoisted(() => ({
  streamChat: vi.fn(),
  listSessions: vi.fn(),
  listSkills: vi.fn(),
  listTools: vi.fn(),
  patchRuntimeConfig: vi.fn(),
  searchSessionFiles: vi.fn(),
  uploadSessionFiles: vi.fn(),
  deleteSessionFile: vi.fn(),
  fileReferenceAvailable: vi.fn().mockResolvedValue(true),
  sessionFileContentUrl: vi.fn((sessionId: string, source: string, path: string) => `/files?session=${sessionId}&source=${source}&path=${encodeURIComponent(path)}`),
}));

vi.mock("../api", () => mocks);

const baseConversation = (): Conversation => ({
  id: "conversation-1",
  title: "测试对话",
  messages: [],
});

const initialClipboardDescriptor = Object.getOwnPropertyDescriptor(window.navigator, "clipboard");

function Harness({
  initial = baseConversation(),
  onEnsureSession,
  onFork,
  onRewind,
  onRun,
  onStopRun,
  onConversationUpdate,
  providerConfig,
  ragEnabled = false,
}: {
  initial?: Conversation | null;
  onEnsureSession?: (id: string) => Promise<string>;
  onFork?: (conversationId: string, messageId: string) => Promise<void>;
  onRewind?: (conversationId: string, messageId: string) => Promise<{ content: string; sessionId: string } | string | undefined>;
  onRun?: (request: { conversationId: string; sessionId: string; prompt: string | null; resume: boolean; mode: ChatMode; permissionMode: PermissionMode; reasoningEffort: ReasoningEffort; providerName?: string; model?: RuntimeConfigModel }) => Promise<void>;
  onStopRun?: (conversationId: string) => void;
  onConversationUpdate?: (conversation: Conversation) => void;
  providerConfig?: ProviderConfig | null;
  ragEnabled?: boolean;
}) {
  const [conversation, setConversation] = React.useState<Conversation | null>(initial);

  return (
    <ChatPage
      conversation={conversation}
      providerConfig={providerConfig}
      ragEnabled={ragEnabled}
      onUpdate={(id, updater) => setConversation((current) => {
        if (current?.id !== id) return current;
        const next = updater(current);
        onConversationUpdate?.(next);
        return next;
      })}
      onNew={() => {
        const next = baseConversation();
        next.id = "conversation-new";
        setConversation(next);
        return next.id;
      }}
      onNavigate={() => undefined}
      onEnsureSession={onEnsureSession}
      onFork={onFork}
      onRewind={onRewind}
      onRun={onRun}
      onStopRun={onStopRun}
    />
  );
}

// Keep the test harness import local so the production component remains free of test-only helpers.
import * as React from "react";

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  if (initialClipboardDescriptor) {
    Object.defineProperty(window.navigator, "clipboard", initialClipboardDescriptor);
  } else {
    delete (window.navigator as { clipboard?: Clipboard }).clipboard;
  }
});

describe("ChatPage run lifecycle", () => {
  it("stops the active stream and removes the thinking indicator immediately", async () => {
    let resolveStream: ((result: "aborted") => void) | undefined;
    mocks.streamChat.mockImplementation(
      (_prompt: string, _onMessage: (message: StreamMessage) => void, signal: AbortSignal) =>
        new Promise<"aborted">((resolve) => {
          resolveStream = resolve;
          signal.addEventListener("abort", () => resolve("aborted"), { once: true });
        }),
    );

    const user = userEvent.setup();
    render(<Harness />);
    await user.type(screen.getByPlaceholderText("输入任务，按 Enter 发送"), "长任务");
    await user.click(screen.getByRole("button", { name: "发送" }));

    expect(await screen.findByRole("status", { name: "思考中" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "停止" }));

    await waitFor(() => expect(screen.queryByRole("status", { name: "思考中" })).not.toBeInTheDocument());
    expect(screen.getByText("已停止")).toBeInTheDocument();
    expect(resolveStream).toBeDefined();
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
  });

  it("keeps streamed partial content after cancellation", async () => {
    mocks.streamChat.mockImplementation(
      (_prompt: string, onMessage: (message: StreamMessage) => void, signal: AbortSignal) => {
        onMessage({ type: "event", kind: "response_delta", data: { content: "已收到的部分" } });
        return new Promise<"aborted">((resolve) =>
          signal.addEventListener("abort", () => resolve("aborted"), { once: true }),
        );
      },
    );

    const user = userEvent.setup();
    render(<Harness />);
    await user.type(screen.getByPlaceholderText("输入任务，按 Enter 发送"), "部分回答");
    await user.click(screen.getByRole("button", { name: "发送" }));
    expect(await screen.findByText("已收到的部分")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "停止" }));
    expect(screen.getByText("已收到的部分")).toBeInTheDocument();
    expect(screen.getByText("已停止")).toBeInTheDocument();
  });

  it("clears running state and renders stream failures", async () => {
    mocks.streamChat.mockRejectedValue(new Error("连接提前关闭"));
    const user = userEvent.setup();
    render(<Harness />);
    await user.type(screen.getByPlaceholderText("输入任务，按 Enter 发送"), "失败任务");
    await user.click(screen.getByRole("button", { name: "发送" }));

    expect(await screen.findByText("⚠️ 连接提前关闭")).toBeInTheDocument();
    expect(screen.queryByRole("status", { name: "思考中" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
  });

  it("can create an empty conversation before the first streamed update", async () => {
    mocks.streamChat.mockImplementation(
      (_prompt: string, onMessage: (message: StreamMessage) => void, signal: AbortSignal) => {
        onMessage({ type: "done", status: "completed", final_answer: "首次回答" });
        return Promise.resolve("completed" as const);
      },
    );

    let latest: Conversation | undefined;
    const user = userEvent.setup();
    render(<Harness initial={null} onConversationUpdate={(conversation) => { latest = conversation; }} />);
    await user.type(screen.getByPlaceholderText("输入任务，按 Enter 发送"), "第一次");
    await user.click(screen.getByRole("button", { name: "发送" }));

    expect(await screen.findByText("首次回答")).toBeInTheDocument();
    expect(latest?.messageCount).toBe(2);
  });

  it("aborts the stream when the chat page is unmounted", async () => {
    let aborted = false;
    mocks.streamChat.mockImplementation(
      (_prompt: string, _onMessage: (message: StreamMessage) => void, signal: AbortSignal) =>
        new Promise<"aborted">((resolve) =>
          signal.addEventListener(
            "abort",
            () => {
              aborted = true;
              resolve("aborted");
            },
            { once: true },
          ),
        ),
    );

    const user = userEvent.setup();
    const view = render(<Harness />);
    await user.type(screen.getByPlaceholderText("输入任务，按 Enter 发送"), "离开页面");
    await user.click(screen.getByRole("button", { name: "发送" }));
    view.unmount();

    await waitFor(() => expect(aborted).toBe(true));
  });

  it("copies raw Markdown source and invokes fork and rewind actions", async () => {
    const onFork = vi.fn().mockResolvedValue(undefined);
    const onRewind = vi.fn().mockResolvedValue("用户原文");
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window.navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    await copyText("用户原文");
    expect(writeText).toHaveBeenCalledWith("用户原文");
    const initial: Conversation = {
      id: "conversation-actions",
      title: "操作测试",
      messages: [
        { id: "user-1", role: "user", content: "用户原文", events: [] },
        { id: "assistant-1", role: "assistant", content: "Agent 原文", events: [], runId: "run-1" },
      ],
    };

    const user = userEvent.setup();
    render(<Harness initial={initial} onFork={onFork} onRewind={onRewind} />);
    const copyButtons = screen.getAllByRole("button", { name: "复制" });
    await user.click(copyButtons[0]);
    expect(screen.getByText("已复制")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Fork" }));
    expect(onFork).toHaveBeenCalledWith("conversation-actions", "assistant-1");
    await user.click(screen.getByRole("button", { name: "回溯" }));
    expect(onRewind).toHaveBeenCalledWith("conversation-actions", "user-1");
    expect(screen.getByPlaceholderText("输入任务，按 Enter 发送")).toHaveValue("用户原文");
  });

  it("passes an existing backend session to streamChat and records stream identifiers", async () => {
    mocks.streamChat.mockImplementation(
      (_prompt: string, onMessage: (message: StreamMessage) => void, _signal: AbortSignal, sessionId?: string) => {
        expect(sessionId).toBe("session-existing");
        onMessage({ type: "done", status: "completed", final_answer: "已完成", session_id: sessionId, run_id: "run-new" });
        return Promise.resolve("completed" as const);
      },
    );
    const initial: Conversation = {
      id: "conversation-session",
      title: "已有会话",
      sessionId: "session-existing",
      messagesLoaded: true,
      messages: [],
    };
    const user = userEvent.setup();
    render(<Harness initial={initial} />);
    await user.type(screen.getByPlaceholderText("输入任务，按 Enter 发送"), "继续提问");
    await user.click(screen.getByRole("button", { name: "发送" }));
    expect(await screen.findByText("已完成")).toBeInTheDocument();
    expect(mocks.streamChat).toHaveBeenCalledWith(
      "继续提问",
      expect.any(Function),
      expect.any(AbortSignal),
      "session-existing",
    );
  });

  it("renders permission and reasoning controls as Ant Design selectors", async () => {
    const user = userEvent.setup();
    render(<Harness />);

    expect(screen.queryByRole("textbox", { name: "提供商" })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "模型" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("combobox", { name: "权限模式" }));
    await user.click(screen.getByRole("option", { name: /完全访问/ }));
    expect(screen.getByText(/完全访问/)).toBeInTheDocument();

    await user.click(screen.getByRole("combobox", { name: "思考等级" }));
    await user.click(screen.getByRole("option", { name: "high" }));
    expect(screen.getAllByText("high", { exact: true }).length).toBeGreaterThan(0);
  });

  it("uses the global RAG setting without a composer knowledge-base selector", async () => {
    mocks.streamChat.mockImplementation(
      (_prompt: string, onMessage: (message: StreamMessage) => void) => {
        onMessage({ type: "done", status: "completed", final_answer: "已完成" });
        return Promise.resolve("completed" as const);
      },
    );
    const user = userEvent.setup();
    render(<Harness ragEnabled />);

    expect(screen.queryByRole("combobox", { name: "知识库模式" })).not.toBeInTheDocument();
    await user.type(screen.getByPlaceholderText("输入任务，按 Enter 发送"), "查询知识库");
    await user.click(screen.getByRole("button", { name: "发送" }));

    expect(await screen.findByText("已完成")).toBeInTheDocument();
    expect(mocks.streamChat).toHaveBeenCalledWith(
      "查询知识库",
      expect.any(Function),
      expect.any(AbortSignal),
      expect.objectContaining({ ragMode: "tool" }),
    );
  });

  it("uses the Provider and model selected in user settings for the next run", async () => {
    const onRun = vi.fn().mockResolvedValue(undefined);
    const configuredProvider: ProviderConfig = {
      id: "provider-settings",
      is_active: true,
      provider_name: "work-openai",
      protocol: "responses",
      base_url: "https://example.test/v1",
      model: "gpt-settings",
      max_tokens: 16000,
      context_size: 128000,
      tokenizer_model: "gpt-settings",
      api_key_configured: true,
    };
    const user = userEvent.setup();
    render(<Harness providerConfig={configuredProvider} onRun={onRun} />);

    await user.type(screen.getByPlaceholderText("输入任务，按 Enter 发送"), "使用设置模型");
    await user.click(screen.getByRole("button", { name: "发送" }));

    expect(onRun).toHaveBeenCalledWith(expect.objectContaining({
      providerName: "work-openai",
      model: expect.objectContaining({
        current_model: "gpt-settings",
        context_length: 128000,
        output_length: 16000,
      }),
    }));
  });

  it("renders when a cached runtime node has no v0.3 model fields", () => {
    const legacyNode = {
      session_id: "session-legacy",
      parent_session_id: "",
      id: "node-legacy",
      parent_id: "",
      version: "0.3.0",
      firstKeptEntryId: "node-legacy",
      compactionIdx: "node-legacy",
      user: "",
      provider_name: "",
      permission_mode: "approval_for_me",
      running_mode: "agent",
      cwd: "",
      timestamp: "2026-08-14T00:00:00+00:00",
      status: "success",
      data: { type: "message", message: { role: "assistant", content: [] } },
    } as unknown as NonNullable<Conversation["runtimeNodes"]>[number];
    const initial: Conversation = {
      ...baseConversation(),
      sessionId: "session-legacy",
      lastNodeId: "node-legacy",
      runtimeNodes: [legacyNode],
    };

    render(<Harness initial={initial} />);

    expect(screen.getByPlaceholderText("输入任务，按 Enter 发送")).toBeInTheDocument();
    expect(screen.getAllByText("medium", { exact: true }).length).toBeGreaterThan(0);
  });

  it("edits a user message in place, rewinds, and starts a replacement run", async () => {
    const onRewind = vi.fn().mockResolvedValue({ content: "用户原文", sessionId: "session-rewound" });
    const onRun = vi.fn().mockResolvedValue(undefined);
    const initial: Conversation = {
      id: "conversation-edit",
      title: "编辑测试",
      sessionId: "session-old",
      messages: [
        { id: "user-edit", role: "user", content: "用户原文", events: [] },
        { id: "assistant-edit", role: "assistant", content: "旧回答", events: [], runId: "run-old" },
      ],
    };

    const user = userEvent.setup();
    render(<Harness initial={initial} onRewind={onRewind} onRun={onRun} />);
    await user.click(screen.getByRole("button", { name: "编辑" }));
    const editor = screen.getByRole("textbox", { name: "编辑用户消息" });
    await user.clear(editor);
    await user.type(editor, "修改后的任务");
    await user.click(screen.getByRole("button", { name: "保存并重新生成" }));

    expect(onRewind).toHaveBeenCalledWith("conversation-edit", "user-edit");
    expect(onRun).toHaveBeenCalledWith(expect.objectContaining({
      conversationId: "conversation-edit",
      sessionId: "session-rewound",
      prompt: "修改后的任务",
      resume: false,
    }));
  });

  it("enters edit mode when the user bubble is clicked", async () => {
    const initial: Conversation = {
      id: "conversation-click-edit",
      title: "点击编辑",
      messages: [{ id: "user-click", role: "user", content: "点击我编辑", events: [] }],
    };
    const user = userEvent.setup();
    render(<Harness initial={initial} onRewind={vi.fn().mockResolvedValue({ content: "点击我编辑", sessionId: "s1" })} />);

    await user.click(screen.getByText("点击我编辑"));
    expect(screen.getByRole("textbox", { name: "编辑用户消息" })).toHaveValue("点击我编辑");
  });
  it("keeps reasoning before the circular send button", () => {
    const { container } = render(<Harness />);
    const box = container.querySelector(".composer-box");
    expect(box).not.toBeNull();
    const labels = Array.from(box!.querySelectorAll<HTMLInputElement>(".composer-settings-controls .ant-select-input"))
      .map((input) => input.getAttribute("aria-label"));
    expect(labels).toEqual(["运行模式", "权限模式", "思考等级"]);
    const send = screen.getByRole("button", { name: "发送" });
    expect(send).toHaveClass("ant-btn-circle", "send-btn");
    expect(send.previousElementSibling).toHaveClass("composer-toolbar");
  });

  it("scrolls the chat container to the bottom from the floating button", async () => {
    const { container } = render(<Harness />);
    const scroll = container.querySelector<HTMLElement>(".chat-scroll");
    if (!scroll) throw new Error("chat scroll container was not rendered");
    Object.defineProperty(scroll, "scrollHeight", { configurable: true, value: 1200 });
    const scrollTo = vi.fn();
    Object.defineProperty(scroll, "scrollTo", { configurable: true, value: scrollTo });

    await userEvent.setup().click(screen.getByRole("button", { name: "滚动到底部" }));

    expect(scrollTo).toHaveBeenCalledWith({ top: 1200, behavior: "smooth" });
  });

  it("mounts the Timeline as a left-edge overlay for the same scroll container", () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function getBoundingClientRect(this: HTMLElement) {
      if (this.classList.contains("chat-scroll")) {
        return { top: 40, left: 100, right: 900, bottom: 640, width: 800, height: 600, x: 100, y: 40, toJSON: () => ({}) };
      }
      if (this.dataset.composerSeat !== undefined) {
        return { top: 650, left: 0, right: 900, bottom: 700, width: 900, height: 50, x: 0, y: 650, toJSON: () => ({}) };
      }
      return { top: 0, left: 0, right: 900, bottom: 700, width: 900, height: 700, x: 0, y: 0, toJSON: () => ({}) };
    });
    const initial: Conversation = {
      id: "conversation-timeline-layout",
      title: "Timeline 布局",
      messages: [
        { id: "user-layout", role: "user", content: "第一轮", events: [] },
        { id: "assistant-layout", role: "assistant", content: "回答", events: [] },
      ],
    };
    const { container } = render(<Harness initial={initial} />);
    const scroll = container.querySelector(".chat-scroll");
    const content = scroll?.querySelector(":scope > .chat-scroll-content");
    const messages = content?.querySelector(":scope > .chat-messages");
    const timeline = container.querySelector("[aria-label='消息时间轴']");

    expect(scroll).toBeInTheDocument();
    expect(content).toBeInTheDocument();
    expect(messages).toBeInTheDocument();
    expect(timeline).toBeInTheDocument();
    expect(timeline?.parentElement).toBe(scroll);
    expect(scroll).toHaveAttribute("data-conversation-scroll");
    expect(container.querySelector("[data-chat-anchor-key='user-layout']")).toBeInTheDocument();
  });

  it("does not mount the Timeline on mobile viewports", () => {
    const originalInnerWidth = window.innerWidth;
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 375 });
    const initial: Conversation = {
      id: "conversation-mobile-timeline",
      title: "移动端布局",
      messages: [{ id: "user-mobile", role: "user", content: "移动消息", events: [] }],
    };

    const { container } = render(<Harness initial={initial} />);

    expect(container.querySelector("[aria-label='消息时间轴']")).toBeNull();
    Object.defineProperty(window, "innerWidth", { configurable: true, value: originalInnerWidth });
  });

  it("renders the session todo panel merged inside the composer", () => {
    const initial: Conversation = {
      id: "conversation-todo",
      title: "任务清单",
      messages: [
        { id: "user-todo", role: "user", content: "做多步任务", events: [] },
        {
          id: "assistant-todo",
          role: "assistant",
          content: "",
          events: [
            {
              kind: "tool_call",
              message: "todo_write",
              data: {
                tool: "todo_write",
                call_id: "call-todo",
                arguments: {
                  todos: [
                    { content: "探索仓库", status: "in_progress" },
                    { content: "写测试", status: "pending" },
                  ],
                },
              },
            },
          ],
        },
      ],
    };
    const { container } = render(<Harness initial={initial} />);

    const header = screen.getByText("任务清单");
    expect(header.closest(".composer")).not.toBeNull();
    expect(header.closest(".todo-panel")).not.toBeNull();
    expect(header.closest(".composer-todo-anchor")).not.toBeNull();
    expect(header.closest(".composer")?.className).toContain("has-todo");
    expect(screen.getByText("0/2 完成")).toBeTruthy();

    fireEvent.click(header.closest(".ant-collapse-header")!);
    expect(screen.getByText("探索仓库")).toBeTruthy();
    expect(screen.getByText("写测试")).toBeTruthy();
  });

  it("hides the todo panel when the conversation has no todo list", () => {
    const { container } = render(<Harness />);

    expect(screen.queryByText("任务清单")).toBeNull();
    expect(container.querySelector(".composer")?.className).not.toContain("has-todo");
    expect(container.querySelector(".composer-todo-anchor")).toBeNull();
  });
});

describe("ChatPage file references", () => {
  const sessionConversation = (): Conversation => ({
    id: "conversation-files",
    title: "文件会话",
    sessionId: "session-files",
    messages: [],
  });

  it("searches files when typing @ and completes with a reference", async () => {
    mocks.searchSessionFiles.mockResolvedValue([
      { source: "upload", path: "notes.md", name: "notes.md", size: 10, mime: "text/markdown", mtime: "2026-01-01T00:00:00+00:00", is_image: false },
    ]);
    const user = userEvent.setup();
    const { container } = render(<Harness initial={sessionConversation()} />);
    const textarea = screen.getByPlaceholderText("输入任务，按 Enter 发送");
    await user.type(textarea, "请查看 @note");
    await waitFor(() => expect(mocks.searchSessionFiles).toHaveBeenCalledWith("session-files", "note", 20));
    const item = container.querySelector(".file-item")!;
    await user.click(item);

    expect(textarea).toHaveValue("请查看 @notes.md");
    expect(screen.getByLabelText("待发送引用")).toBeInTheDocument();
    expect(screen.getByText("notes.md", { selector: ".composer-reference-path" })).toBeInTheDocument();
  });

  it("inserts quoted tokens for paths with spaces", async () => {
    mocks.searchSessionFiles.mockResolvedValue([
      { source: "project", path: "my notes.txt", name: "my notes.txt", size: 10, mime: "text/plain", mtime: "2026-01-01T00:00:00+00:00", is_image: false },
    ]);
    const user = userEvent.setup();
    const { container } = render(<Harness initial={sessionConversation()} />);
    const textarea = screen.getByPlaceholderText("输入任务，按 Enter 发送");
    await user.type(textarea, "看看 @my");
    await waitFor(() => expect(container.querySelector(".file-item")).not.toBeNull());
    await user.click(container.querySelector(".file-item")!);

    expect(textarea).toHaveValue("看看 @\"my notes.txt\"");
  });

  it("dismisses the file menu with Escape and never opens the command menu", async () => {
    mocks.searchSessionFiles.mockResolvedValue([
      { source: "upload", path: "a.txt", name: "a.txt", size: 1, mime: "text/plain", mtime: "2026-01-01T00:00:00+00:00", is_image: false },
    ]);
    const user = userEvent.setup();
    const { container } = render(<Harness initial={sessionConversation()} />);
    const textarea = screen.getByPlaceholderText("输入任务，按 Enter 发送");
    await user.type(textarea, "@a");
    await waitFor(() => expect(container.querySelector(".file-item")).not.toBeNull());
    await user.keyboard("{Escape}");
    await waitFor(() => expect(container.querySelector(".file-item")).toBeNull());
  });

  it("uploads picked files, shows progress, and sends references with the message", async () => {
    mocks.uploadSessionFiles.mockResolvedValue([
      { source: "upload", path: "shot.png", name: "shot.png", size: 4, mime: "image/png", mtime: "2026-01-01T00:00:00+00:00", is_image: true },
    ]);
    mocks.streamChat.mockImplementation(
      (_prompt: string, onMessage: (message: StreamMessage) => void) => {
        onMessage({ type: "done", status: "completed", final_answer: "收到" });
        return Promise.resolve("completed" as const);
      },
    );
    const user = userEvent.setup();
    const { container } = render(<Harness initial={sessionConversation()} />);
    const textarea = screen.getByPlaceholderText("输入任务，按 Enter 发送");
    const file = new File(["png"], "shot.png", { type: "image/png" });
    await user.upload(container.querySelector('input[type="file"]')!, file);

    expect(mocks.uploadSessionFiles).toHaveBeenCalledWith("session-files", [file], expect.any(Function));
    expect(await screen.findByText("shot.png", { selector: ".composer-upload-name" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("待发送引用")).toBeInTheDocument());

    await user.type(textarea, "分析图片");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(mocks.streamChat).toHaveBeenCalled());
    expect(mocks.streamChat.mock.calls[0][1]).toBeDefined();
  });

  it("removing a completed upload deletes the server file before send", async () => {
    mocks.uploadSessionFiles.mockResolvedValue([
      { source: "upload", path: "temp.txt", name: "temp.txt", size: 3, mime: "text/plain", mtime: "2026-01-01T00:00:00+00:00", is_image: false },
    ]);
    mocks.deleteSessionFile.mockResolvedValue(undefined);
    const user = userEvent.setup();
    const { container } = render(<Harness initial={sessionConversation()} />);
    const file = new File(["abc"], "temp.txt", { type: "text/plain" });
    await user.upload(container.querySelector('input[type="file"]')!, file);
    await waitFor(() => expect(mocks.uploadSessionFiles).toHaveBeenCalled());

    await user.click(screen.getByLabelText("移除 temp.txt"));
    expect(mocks.deleteSessionFile).toHaveBeenCalledWith("session-files", "upload", "temp.txt");
  });

  it("renders message references and marks deleted files as unavailable", async () => {
    mocks.fileReferenceAvailable = vi.fn().mockResolvedValue(false);
    const initial: Conversation = {
      id: "conversation-refs",
      title: "引用会话",
      sessionId: "session-refs",
      messages: [
        {
          id: "user-refs",
          role: "user",
          content: "看看这些文件",
          events: [],
          references: [
            { source: "upload", path: "report.pdf" },
            { source: "project", path: "src/main.py" },
          ],
        },
      ],
    };
    render(<Harness initial={initial} />);

    expect(screen.getByLabelText("消息引用")).toBeInTheDocument();
    expect(await screen.findAllByText("文件不可用")).toHaveLength(2);
    expect(screen.getByText("report.pdf")).toBeInTheDocument();
    expect(screen.getByText("src/main.py")).toBeInTheDocument();
    expect(mocks.fileReferenceAvailable).toHaveBeenCalledTimes(2);
  });

  it("renders available message references as links", async () => {
    mocks.fileReferenceAvailable = vi.fn().mockResolvedValue(true);
    const initial: Conversation = {
      id: "conversation-refs-ok",
      title: "引用会话",
      sessionId: "session-refs-ok",
      messages: [
        {
          id: "user-refs-ok",
          role: "user",
          content: "看看这些文件",
          events: [],
          references: [{ source: "upload", path: "notes.md" }],
        },
      ],
    };
    render(<Harness initial={initial} />);

    const link = await screen.findByRole("link", { name: "引用 notes.md" });
    expect(link).toHaveAttribute("href", "/files?session=session-refs-ok&source=upload&path=notes.md");
    expect(screen.queryByText("文件不可用")).not.toBeInTheDocument();
  });
});
