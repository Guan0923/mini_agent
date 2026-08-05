import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import AppSidebar from "./AppSidebar";
import type { Conversation } from "../types";

const conversation: Conversation = { id: "c1", title: "测试对话", messages: [] };

function renderSidebar(archivedCount = 0) {
  const onNavigate = vi.fn();
  const view = render(
    <AppSidebar
      user={{ id: "u1", email: "user@example.com", legacy_owner: false }}
      conversations={[conversation]}
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
});
