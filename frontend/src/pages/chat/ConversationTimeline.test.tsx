import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChatMessage } from "../../types";
import ConversationTimeline, { buildConversationTurns, conversationTurnId } from "./ConversationTimeline";

function message(id: string, role: ChatMessage["role"], content: string): ChatMessage {
  return { id, role, content, events: [] };
}

function TimelineHarness({ messages }: { messages: readonly ChatMessage[] }) {
  const scrollContainerRef = useRef<HTMLDivElement | null>(null);

  return (
    <div>
      <div className="chat-scroll" ref={scrollContainerRef}>
        {messages.map((item) => (
          <div id={item.role === "user" ? conversationTurnId(item.id) : undefined} key={item.id}>
            {item.content}
          </div>
        ))}
      </div>
      <ConversationTimeline messages={messages} scrollContainerRef={scrollContainerRef} />
    </div>
  );
}

function setTop(element: HTMLElement, top: number) {
  Object.defineProperty(element, "getBoundingClientRect", {
    configurable: true,
    value: () => ({
      bottom: top + 20,
      height: 20,
      left: 0,
      right: 20,
      top,
      width: 20,
      x: 0,
      y: top,
      toJSON: () => ({}),
    }),
  });
  Object.defineProperty(element, "getClientRects", {
    configurable: true,
    value: () => [{ top, left: 0, right: 20, bottom: top + 20, width: 20, height: 20 }],
  });
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("ConversationTimeline", () => {
  it("creates one turn per user message and groups following assistant messages", () => {
    const messages = [
      message("orphan-assistant", "assistant", "独立回答"),
      message("user-1", "user", "第一轮"),
      message("assistant-1a", "assistant", "第一段回答"),
      message("assistant-1b", "assistant", "第二段回答"),
      message("user-2", "user", "第二轮"),
    ];

    const turns = buildConversationTurns(messages);

    expect(turns).toEqual([
      {
        userMessage: messages[1],
        assistantMessages: [messages[2], messages[3]],
      },
      {
        userMessage: messages[4],
        assistantMessages: [],
      },
    ]);
  });

  it("renders nothing for an empty session or assistant-only messages", () => {
    const { rerender } = render(<TimelineHarness messages={[]} />);
    expect(screen.queryByRole("complementary", { name: "对话轮次导航" })).not.toBeInTheDocument();

    rerender(<TimelineHarness messages={[message("assistant-1", "assistant", "独立回答")]} />);
    expect(screen.queryByRole("complementary", { name: "对话轮次导航" })).not.toBeInTheDocument();
  });

  it("updates stable links when turns are added", () => {
    const firstTurn = [message("user-1", "user", "第一轮"), message("assistant-1", "assistant", "回答")];
    const view = render(<TimelineHarness messages={firstTurn} />);

    expect(screen.getAllByRole("link")).toHaveLength(1);
    expect(screen.getByRole("link", { name: "跳转到第 1 轮对话：第一轮" })).toHaveAttribute(
      "href",
      "#chat-turn-user-1",
    );

    view.rerender(
      <TimelineHarness
        messages={[
          ...firstTurn,
          message("user-2", "user", "第二轮"),
          message("assistant-2", "assistant", "回答"),
        ]}
      />,
    );

    expect(screen.getAllByRole("link")).toHaveLength(2);
    expect(screen.getByRole("link", { name: "跳转到第 2 轮对话：第二轮" })).toHaveAttribute(
      "href",
      "#chat-turn-user-2",
    );
  });

  it("shows the raw user text in a bounded, multiline tooltip", async () => {
    const rawText = `第一行\n${"很长的用户输入 ".repeat(40)}`;
    const user = userEvent.setup();
    render(<TimelineHarness messages={[message("user-long", "user", rawText)]} />);

    const link = screen.getByRole("link", { name: /跳转到第 1 轮对话：第一行/ });
    const hitbox = link.querySelector(".conversation-timeline-hitbox");
    expect(hitbox).not.toBeNull();
    await user.hover(hitbox!);

    const tooltip = await screen.findByRole("tooltip");
    expect(tooltip.textContent).toBe(rawText);
    expect(tooltip.firstElementChild).toHaveClass("conversation-timeline-tooltip");
  });

  it("scrolls the chat container on anchor activation without changing the browser hash", async () => {
    vi.useFakeTimers();
    window.history.replaceState({}, "", "/");
    render(
      <TimelineHarness
        messages={[
          message("user-1", "user", "第一轮"),
          message("assistant-1", "assistant", "回答"),
          message("user-2", "user", "第二轮"),
        ]}
      />,
    );

    const scrollContainer = document.querySelector(".chat-scroll") as HTMLDivElement;
    const firstTarget = document.getElementById(conversationTurnId("user-1"))!;
    const secondTarget = document.getElementById(conversationTurnId("user-2"))!;
    setTop(firstTarget, 80);
    setTop(secondTarget, 340);
    scrollContainer.scrollTop = 24;

    const link = screen.getByRole("link", { name: "跳转到第 2 轮对话：第二轮" });
    expect(link.tagName).toBe("A");
    link.focus();
    fireEvent.click(link);

    await act(async () => {
      vi.advanceTimersByTime(600);
    });

    expect(scrollContainer.scrollTop).toBe(332);
    expect(window.location.hash).toBe("");
  });
});
