import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode, useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ChatMessage } from "../../types";
import ConversationTimeline, { buildTimelineEntries, fmtTime, timelineTextOf } from "./ConversationTimeline";

function message(id: string, role: ChatMessage["role"], content: string, extra: Partial<ChatMessage> = {}): ChatMessage {
  return { id, role, content, events: [], ...extra };
}

function setRect(element: Element, rect: { top: number; left: number; right: number; bottom: number }): void {
  Object.defineProperty(element, "getBoundingClientRect", {
    configurable: true,
    value: () => ({
      ...rect,
      width: rect.right - rect.left,
      height: rect.bottom - rect.top,
      x: rect.left,
      y: rect.top,
      toJSON: () => ({}),
    }),
  });
}

function TimelineHarness({ messages }: { messages: readonly ChatMessage[] }) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  return (
    <div className="chat-content">
      <div className="chat-scroll" ref={scrollRef} data-conversation-scroll>
        {messages.map((item) => (
          <div key={item.id} data-chat-anchor-key={item.id}>
            {item.content}
          </div>
        ))}
        <ConversationTimeline messages={messages} scrollContainerRef={scrollRef} />
      </div>
      <div data-composer-seat />
    </div>
  );
}

beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, "getBoundingClientRect", {
    configurable: true,
    value: function getBoundingClientRect() {
      if (this.matches?.(".chat-scroll")) return { top: 40, left: 100, right: 500, bottom: 640, width: 400, height: 600, x: 100, y: 40, toJSON: () => ({}) };
      if (this.matches?.("[data-composer-seat]")) return { top: 650, left: 0, right: 500, bottom: 700, width: 500, height: 50, x: 0, y: 650, toJSON: () => ({}) };
      return { top: 0, left: 0, right: 500, bottom: 700, width: 500, height: 700, x: 0, y: 0, toJSON: () => ({}) };
    },
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("ConversationTimeline", () => {
  it("folds only the latest 30 non-steering user messages", () => {
    const messages = [
      message("assistant", "assistant", "ignored"),
      ...Array.from({ length: 32 }, (_, index) => message(`user-${index + 1}`, "user", `m${index + 1}`)),
      message("steering", "user", "steer", { timelineSource: "steering" }),
    ];
    const entries = buildTimelineEntries(messages);
    expect(entries).toHaveLength(30);
    expect(entries[0]?.key).toBe("user-3");
    expect(entries[entries.length - 1]?.key).toBe("user-32");
  });

  it("folds target-compatible text and time values", () => {
    expect(timelineTextOf([{ type: "text", text: " a " }, { type: "text", text: "b" }])).toBe("a b");
    expect(timelineTextOf([{ type: "image" }])).toBe("[非文本内容]");
    expect(timelineTextOf([{ type: "text", text: "a" }, { type: "image" }])).toBe("a …");
    expect(timelineTextOf([], "fallback")).toBe("fallback");
    expect(fmtTime(Date.UTC(2026, 0, 2, 3, 4, 5))).toMatch(/^\d{2}:\d{2}:\d{2}$/);
  });

  it("renders ticks, tooltip, Gaussian hover and in-window jump", async () => {
    const scrollIntoView = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: scrollIntoView });
    const user = userEvent.setup();
    render(
      <StrictMode>
        <TimelineHarness
          messages={[
            message("user-1", "user", "first", { timelineSeq: 1, timelineTime: Date.UTC(2026, 0, 2, 3, 4, 5) }),
            message("user-2", "user", "second", { timelineSeq: 2, timelineText: "second preview" }),
          ]}
        />
      </StrictMode>,
    );

    const rows = screen.getAllByRole("button");
    expect(rows).toHaveLength(2);
    await user.hover(rows[1]!);
    const tooltip = screen.getByRole("tooltip");
    expect(tooltip).toHaveTextContent("second preview");
    expect((rows[1]!.firstElementChild as HTMLElement).style.width).toBe("42px");

    const parentWheel = vi.fn();
    document.querySelector(".chat-scroll")?.addEventListener("wheel", parentWheel);
    fireEvent.wheel(tooltip, { deltaY: 160 });
    expect(parentWheel).not.toHaveBeenCalled();

    fireEvent.click(rows[0]!);
    expect(scrollIntoView).toHaveBeenCalledWith({ block: "center", behavior: "smooth" });
  });

  it("does not render without eligible messages", () => {
    const { container } = render(<TimelineHarness messages={[message("a", "assistant", "answer")] } />);
    expect(container.querySelector("[aria-label='消息时间轴']")).not.toBeInTheDocument();
  });
});
