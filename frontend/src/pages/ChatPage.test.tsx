import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import ChatPage from "./ChatPage";
import type { ChatMode, Conversation, PermissionMode, ReasoningEffort, StreamMessage } from "../types";

const mocks = vi.hoisted(() => ({
  streamChat: vi.fn(),
  listSessions: vi.fn(),
  listSkills: vi.fn(),
  listTools: vi.fn(),
}));

vi.mock("../api", () => mocks);

const baseConversation = (): Conversation => ({
  id: "conversation-1",
  title: "测试对话",
  messages: [],
});

function Harness({
  initial = baseConversation(),
  onEnsureSession,
  onFork,
  onRewind,
  onRun,
  onStopRun,
}: {
  initial?: Conversation | null;
  onEnsureSession?: (id: string) => Promise<string>;
  onFork?: (conversationId: string, messageId: string) => Promise<void>;
  onRewind?: (conversationId: string, messageId: string) => Promise<{ content: string; sessionId: string } | string | undefined>;
  onRun?: (request: { conversationId: string; sessionId: string; prompt: string | null; resume: boolean; mode: ChatMode; permissionMode: PermissionMode; reasoningEffort: ReasoningEffort }) => Promise<void>;
  onStopRun?: (conversationId: string) => void;
}) {
  const [conversation, setConversation] = React.useState<Conversation | null>(initial);

  return (
    <ChatPage
      conversation={conversation}
      onUpdate={(id, updater) =>
        setConversation((current) => (current?.id === id ? updater(current) : current))
      }
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

    const user = userEvent.setup();
    render(<Harness initial={null} />);
    await user.type(screen.getByPlaceholderText("输入任务，按 Enter 发送"), "第一次");
    await user.click(screen.getByRole("button", { name: "发送" }));

    expect(await screen.findByText("首次回答")).toBeInTheDocument();
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

  it("copies raw message text and invokes fork and rewind actions", async () => {
    const onFork = vi.fn().mockResolvedValue(undefined);
    const onRewind = vi.fn().mockResolvedValue("用户原文");
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

  it("renders permission, display, and reasoning controls as Ant Design selectors", async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(screen.getByRole("combobox", { name: "权限模式" }));
    await user.click(screen.getByRole("option", { name: /完全访问/ }));
    expect(screen.getByText(/完全访问/)).toBeInTheDocument();

    await user.click(screen.getByRole("combobox", { name: "显示级别" }));
    await user.click(screen.getByRole("option", { name: "显示：verbose" }));
    expect(screen.getByText("显示：verbose")).toBeInTheDocument();

    await user.click(screen.getByRole("combobox", { name: "思考等级" }));
    await user.click(screen.getByRole("option", { name: /思考：高/ }));
    expect(screen.getByText(/思考：高/)).toBeInTheDocument();
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
});
