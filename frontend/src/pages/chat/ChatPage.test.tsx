import { App as AntApp } from "antd";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { projectTurnPath } from "../../app/runtimeDetailProjection";
import { compactTurn } from "../../api";
import type { Conversation, RuntimeStateNode } from "../../types";
import ChatPage from "./ChatPage";

vi.mock("../../api", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../api")>(),
  compactTurn: vi.fn(),
}));

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

describe("ChatPage rewind projection", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.mocked(compactTurn).mockReset();
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
