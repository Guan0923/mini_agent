import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import AppSidebar from "./AppSidebar";
import type { Conversation } from "../types";

const conversation: Conversation = {
  id: "c1",
  title: "测试对话",
  messages: [],
  messageCount: 3,
  updatedAt: "2026-08-05T10:20:00Z",
};

function renderSidebar(archivedCount = 0, conversations: Conversation[] = [conversation]) {
  const onNavigate = vi.fn();
  const view = render(
    <AppSidebar
      user={{ id: "u1", email: "user@example.com", legacy_owner: false }}
      conversations={conversations}
      archivedCount={archivedCount}
      currentId={conversation.id}
      page="chat"
      onNew={vi.fn()}
      onSelect={vi.fn()}
      onNavigate={onNavigate}
      onRename={vi.fn().mockResolvedValue(undefined)}
      onArchive={vi.fn().mockResolvedValue(undefined)}
      onDelete={vi.fn()}
      onSignOut={vi.fn()}
    />,
  );
  return { onNavigate, view };
}

describe("AppSidebar utility navigation", () => {
  it("shows the unread archive count and navigates from both utility buttons", async () => {
    const user = userEvent.setup();
    const { onNavigate, view } = renderSidebar(2);

    expect(screen.getByRole("button", { name: "回收站 (2)" })).toBeInTheDocument();
    expect(view.container.querySelector(".sidebar-utility-links")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "回收站 (2)" }));
    await user.click(screen.getByRole("button", { name: "Benchmark" }));
    expect(onNavigate).toHaveBeenNthCalledWith(1, "trash");
    expect(onNavigate).toHaveBeenNthCalledWith(2, "benchmark");
  });

  it("does not render an archive badge when there are no unread archives", () => {
    renderSidebar();
    expect(screen.getByRole("button", { name: "回收站" })).toBeInTheDocument();
    expect(document.querySelector(".ant-badge-count")).not.toBeInTheDocument();
  });
  it("renders history metadata and scrolls only the hovered overflowing item", () => {
    const second: Conversation = { id: "c2", title: "第二个会话", messages: [] };
    const longTitle = "这是一个足够长的历史会话摘要，用于验证悬浮时只滚动当前条目";
    renderSidebar(0, [{ ...conversation, title: longTitle, messageCount: 12 }, second]);

    expect(screen.getByText(/12 条消息/)).toBeInTheDocument();
    expect(document.querySelector(".history-meta")?.textContent).toMatch(/12 条消息 ·/);

    const viewports = Array.from(document.querySelectorAll<HTMLElement>(".history-summary-viewport"));
    const firstText = viewports[0].querySelector<HTMLElement>(".history-summary-text");
    const secondText = viewports[1].querySelector<HTMLElement>(".history-summary-text");
    expect(firstText).not.toBeNull();
    expect(secondText).not.toBeNull();
    Object.defineProperty(viewports[0], "clientWidth", { configurable: true, value: 80 });
    Object.defineProperty(firstText, "scrollWidth", { configurable: true, value: 240 });
    Object.defineProperty(viewports[1], "clientWidth", { configurable: true, value: 160 });
    Object.defineProperty(secondText, "scrollWidth", { configurable: true, value: 160 });

    fireEvent.mouseEnter(viewports[0]);
    expect(viewports[0]).toHaveClass("is-scrolling");
    expect(viewports[1]).not.toHaveClass("is-scrolling");
    fireEvent.mouseLeave(viewports[0]);
    expect(viewports[0]).not.toHaveClass("is-scrolling");
  });
});
