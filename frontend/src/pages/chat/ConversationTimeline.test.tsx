import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode, useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ChatMessage } from "../../types";
import ConversationTimeline, { buildTimelineEntries, fmtTime, timelineTextOf } from "./ConversationTimeline";

let timelineClientHeight = 96;

function message(id: string, role: ChatMessage["role"], content: string, extra: Partial<ChatMessage> = {}): ChatMessage {
  return { id, role, content, events: [], ...extra };
}

function TimelineHarness({ messages, threadId = "thread-1" }: { messages: readonly ChatMessage[]; threadId?: string }) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  return (
    <div className="chat-content">
      <div className="chat-scroll" ref={scrollRef} data-conversation-scroll>
        {messages.map((item) => (
          <div key={item.id} data-chat-anchor-key={item.id}>
            {item.content}
          </div>
        ))}
      </div>
      <ConversationTimeline key={threadId} messages={messages} scrollContainerRef={scrollRef} />
    </div>
  );
}

beforeEach(() => {
  timelineClientHeight = 96;
  Object.defineProperties(HTMLElement.prototype, {
    getBoundingClientRect: {
      configurable: true,
      value: function getBoundingClientRect() {
        if (this.matches?.(".chat-scroll")) return { top: 40, left: 100, right: 500, bottom: 160, width: 400, height: 120, x: 100, y: 40, toJSON: () => ({}) };
        if (this.matches?.(".chat-content")) return { top: 0, left: 0, right: 500, bottom: 200, width: 500, height: 200, x: 0, y: 0, toJSON: () => ({}) };
        return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0, toJSON: () => ({}) };
      },
    },
    clientHeight: {
      configurable: true,
      get() {
        return this.getAttribute?.("aria-label") === "消息时间轴刻度" ? timelineClientHeight : 0;
      },
    },
    scrollHeight: {
      configurable: true,
      get() {
        if (this.getAttribute?.("aria-label") === "消息时间轴刻度") {
          return this.querySelectorAll("button").length * 12;
        }
        if (this.getAttribute?.("role") === "tooltip") return 120;
        return 0;
      },
    },
    offsetHeight: {
      configurable: true,
      get() {
        return this.getAttribute?.("role") === "tooltip" ? 40 : 0;
      },
    },
    offsetTop: {
      configurable: true,
      get() {
        if (this.tagName === "BUTTON" && this.parentElement) {
          return Array.from(this.parentElement.children).indexOf(this) * 12;
        }
        return 0;
      },
    },
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("ConversationTimeline", () => {
  it("keeps every user message, including steering, in transcript order", () => {
    const messages = [
      message("assistant", "assistant", "ignored"),
      message("first", "user", "first despite seq", { timelineSeq: 99 }),
      message("steering", "user", "steer", { timelineSeq: 1, timelineSource: "steering", deliveryId: "delivery-steer" }),
      ...Array.from({ length: 32 }, (_, index) => message(`user-${index + 1}`, "user", `m${index + 1}`)),
    ];

    const entries = buildTimelineEntries(messages);

    expect(entries).toHaveLength(34);
    expect(entries.map((entry) => entry.key)).toEqual([
      "first",
      "steering",
      ...Array.from({ length: 32 }, (_, index) => `user-${index + 1}`),
    ]);
    expect(buildTimelineEntries([
      message("same", "user", "body", { deliveryId: "delivery-1" }),
    ])[0]?.fingerprint).not.toBe(buildTimelineEntries([
      message("same", "user", "body", { deliveryId: "delivery-2" }),
    ])[0]?.fingerprint);
  });

  it("folds target-compatible text and time values", () => {
    expect(timelineTextOf([{ type: "text", text: " a " }, { type: "text", text: "b" }])).toBe("a b");
    expect(timelineTextOf([{ type: "image" }])).toBe("[非文本内容]");
    expect(timelineTextOf([{ type: "text", text: "a" }, { type: "image" }])).toBe("a …");
    expect(timelineTextOf([], "fallback")).toBe("fallback");
    expect(fmtTime(Date.UTC(2026, 0, 2, 3, 4, 5))).toMatch(/^\d{2}:\d{2}:\d{2}$/);
  });

  it("renders ticks, a sibling tooltip, Gaussian hover and in-window jump", async () => {
    const scrollIntoView = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: scrollIntoView });
    const user = userEvent.setup();
    render(
      <StrictMode>
        <TimelineHarness
          messages={[
            message("user-1", "user", "first", { timelineTime: Date.UTC(2026, 0, 2, 3, 4, 5) }),
            message("user-2", "user", "second", { timelineText: "second preview" }),
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
    expect(tooltip.parentElement).toBe(screen.getByRole("navigation", { name: "消息时间轴" }));
    expect(tooltip.parentElement).not.toBe(rows[1]);

    const chatWheel = vi.fn();
    const timelineWheel = vi.fn();
    document.querySelector(".chat-scroll")?.addEventListener("wheel", chatWheel);
    screen.getByRole("region", { name: "消息时间轴刻度" }).addEventListener("wheel", timelineWheel);
    fireEvent.wheel(tooltip, { deltaY: 160 });
    expect(chatWheel).not.toHaveBeenCalled();
    expect(timelineWheel).not.toHaveBeenCalled();

    fireEvent.click(rows[0]!);
    expect(scrollIntoView).toHaveBeenCalledWith({ block: "center", behavior: "smooth" });
  });

  it("starts at the latest tick, follows only true appends from the bottom and resets on Thread switch", () => {
    const initial = Array.from({ length: 10 }, (_, index) => message(`user-${index}`, "user", `m${index}`));
    const { rerender } = render(<TimelineHarness messages={initial} />);
    let viewport = screen.getByRole("region", { name: "消息时间轴刻度" });
    expect(viewport.scrollTop).toBe(120);

    viewport.scrollTop = 10;
    fireEvent.scroll(viewport);
    rerender(<TimelineHarness messages={[...initial, message("user-10", "user", "m10")]} />);
    viewport = screen.getByRole("region", { name: "消息时间轴刻度" });
    expect(viewport.scrollTop).toBe(10);

    viewport.scrollTop = 36;
    fireEvent.scroll(viewport);
    rerender(<TimelineHarness messages={[...initial, message("user-10", "user", "m10"), message("user-11", "user", "m11")]} />);
    viewport = screen.getByRole("region", { name: "消息时间轴刻度" });
    expect(viewport.scrollTop).toBe(144);

    rerender(<TimelineHarness threadId="thread-2" messages={initial} />);
    viewport = screen.getByRole("region", { name: "消息时间轴刻度" });
    expect(viewport.scrollTop).toBe(120);
  });

  it("keeps following a layout resize only when it was already at the bottom", async () => {
    const messages = Array.from({ length: 10 }, (_, index) => message(`user-${index}`, "user", `m${index}`));
    render(<TimelineHarness messages={messages} />);
    const viewport = screen.getByRole("region", { name: "消息时间轴刻度" });
    viewport.scrollTop = 24;
    fireEvent.scroll(viewport);

    timelineClientHeight = 72;
    fireEvent(window, new Event("resize"));
    await waitFor(() => expect(viewport.scrollTop).toBe(120));

    viewport.scrollTop = 10;
    fireEvent.scroll(viewport);
    timelineClientHeight = 60;
    fireEvent(window, new Event("resize"));
    await waitFor(() => expect(viewport.scrollTop).toBe(10));
  });

  it("updates version replacements immediately without following and clears a missing hover", async () => {
    const initial = Array.from({ length: 10 }, (_, index) => message(`user-${index}`, "user", `old-${index}`));
    const { rerender } = render(<TimelineHarness messages={initial} />);
    const viewport = screen.getByRole("region", { name: "消息时间轴刻度" });
    viewport.scrollTop = 24;
    fireEvent.scroll(viewport);
    fireEvent.mouseEnter(screen.getByRole("button", { name: "跳转到消息：old-0" }));
    expect(screen.getByRole("tooltip")).toHaveTextContent("old-0");

    const replacement = [
      message("user-0", "user", "new-0"),
      message("steering", "user", "new steering", { timelineSource: "steering", deliveryId: "delivery-new" }),
      ...Array.from({ length: 9 }, (_, index) => message(`new-${index}`, "user", `new-${index}`)),
    ];
    rerender(<TimelineHarness messages={replacement} />);

    expect(viewport.scrollTop).toBe(24);
    expect(screen.getAllByRole("button")).toHaveLength(11);
    expect(screen.getByRole("tooltip")).toHaveTextContent("new-0");
    expect(screen.getByRole("button", { name: "跳转到消息：new steering" })).toBeInTheDocument();

    rerender(<TimelineHarness messages={replacement.slice(1)} />);
    await waitFor(() => expect(screen.queryByRole("tooltip")).not.toBeInTheDocument());
  });

  it("clamps the first and last tooltip inside the visible overlay", async () => {
    const messages = Array.from({ length: 10 }, (_, index) => message(`user-${index}`, "user", `m${index}`));
    render(<TimelineHarness messages={messages} />);
    const viewport = screen.getByRole("region", { name: "消息时间轴刻度" });
    const rows = screen.getAllByRole("button");

    viewport.scrollTop = 0;
    fireEvent.scroll(viewport);
    fireEvent.mouseEnter(rows[0]!);
    await waitFor(() => expect(screen.getByRole("tooltip")).toHaveStyle({ top: "4px" }));

    viewport.scrollTop = 24;
    fireEvent.scroll(viewport);
    fireEvent.mouseEnter(rows[rows.length - 1]!);
    await waitFor(() => expect(screen.getByRole("tooltip")).toHaveStyle({ top: "52px", maxHeight: "88px" }));
  });

  it("does not render without user messages", () => {
    const { container } = render(<TimelineHarness messages={[message("a", "assistant", "answer")] } />);
    expect(container.querySelector("[aria-label='消息时间轴']")).not.toBeInTheDocument();
  });
});
