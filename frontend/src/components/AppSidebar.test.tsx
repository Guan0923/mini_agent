import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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

const projectConversation: Conversation = {
  id: "project-c1",
  title: "项目对话",
  messages: [],
  messageCount: 1,
  projectId: "project-1",
};

const project = {
  project_id: "project-1",
  name: "示例项目",
  cwd: "C:\\workspace\\example",
  available: true,
  created_at: "2026-08-05T10:20:00Z",
  updated_at: "2026-08-05T10:20:00Z",
  conversation_count: 1,
  session_ids: ["project-c1"],
};

function renderSidebar(archivedCount = 0, conversations: Conversation[] = [conversation]) {
  const onNavigate = vi.fn();
  const onProfileUpdate = vi.fn().mockResolvedValue(undefined);
  const view = render(
    <AppSidebar
      profile={{ display_name: "账户名称", agent_preferences: "" }}
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
      onProfileUpdate={onProfileUpdate}
    />,
  );
  return { onNavigate, onProfileUpdate, view };
}

describe("AppSidebar utility navigation", () => {
  it("renders the project title and collapse control in the sidebar header", async () => {
    const user = userEvent.setup();
    const onToggleCollapse = vi.fn();
    render(
      <AppSidebar
        profile={{ display_name: "账户名称", agent_preferences: "" }}
        conversations={[conversation]}
        archivedCount={0}
        currentId={conversation.id}
        page="chat"
        onNew={vi.fn()}
        onSelect={vi.fn()}
        onNavigate={vi.fn()}
        onRename={vi.fn().mockResolvedValue(undefined)}
        onArchive={vi.fn().mockResolvedValue(undefined)}
        onDelete={vi.fn()}
        collapsed={false}
        onToggleCollapse={onToggleCollapse}
        revealKey={4}
      />,
    );

    const header = document.querySelector(".sidebar-header");
    expect(header).not.toBeNull();
    expect(screen.getByText("Mini-Agent", { selector: ".sidebar-project-title" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "折叠侧边栏" })).toHaveAttribute("aria-expanded", "true");
    expect(document.querySelectorAll(".sidebar-reveal-item")).toHaveLength(6);
    await user.click(screen.getByRole("button", { name: "折叠侧边栏" }));
    expect(onToggleCollapse).toHaveBeenCalledOnce();
  });

  it("uses expand semantics when the sidebar is collapsed", () => {
    render(
      <AppSidebar
        profile={{ display_name: "账户名称", agent_preferences: "" }}
        conversations={[conversation]}
        archivedCount={0}
        currentId={conversation.id}
        page="chat"
        onNew={vi.fn()}
        onSelect={vi.fn()}
        onNavigate={vi.fn()}
        onRename={vi.fn().mockResolvedValue(undefined)}
        onArchive={vi.fn().mockResolvedValue(undefined)}
        onDelete={vi.fn()}
        collapsed
        onToggleCollapse={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: "展开侧边栏" })).toHaveAttribute("aria-expanded", "false");
  });

  it("uses the shared create-button style for new conversations and projects", async () => {
    const user = userEvent.setup();
    const onNew = vi.fn();
    const onNewProject = vi.fn();

    render(
      <AppSidebar
        profile={{ display_name: "账户名称", agent_preferences: "" }}
        conversations={[conversation]}
        archivedCount={0}
        currentId={conversation.id}
        page="chat"
        onNew={onNew}
        onNewProject={onNewProject}
        onSelect={vi.fn()}
        onNavigate={vi.fn()}
        onRename={vi.fn().mockResolvedValue(undefined)}
        onArchive={vi.fn().mockResolvedValue(undefined)}
        onDelete={vi.fn()}
      />,
    );

    const newConversationButton = screen.getByRole("button", { name: "新建对话" });
    const newProjectButton = screen.getByRole("button", { name: "新建项目" });
    expect(newConversationButton).toHaveClass("sidebar-create-button");
    expect(newProjectButton).toHaveClass("sidebar-create-button");
    expect(newConversationButton).not.toHaveClass("ant-btn-lg");
    expect(newProjectButton).not.toHaveClass("ant-btn-lg");

    await user.click(newConversationButton);
    await user.click(newProjectButton);
    expect(onNew).toHaveBeenCalledOnce();
    expect(onNewProject).toHaveBeenCalledOnce();
  });

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

  it("renders project history above ordinary history and keeps the selected project collapsible", async () => {
    const user = userEvent.setup();
    render(
      <AppSidebar
        profile={{ display_name: "账户名称", agent_preferences: "" }}
        conversations={[conversation, projectConversation]}
        projects={[project]}
        archivedCount={0}
        currentId={projectConversation.id}
        page="chat"
        onNew={vi.fn()}
        onSelect={vi.fn()}
        onNavigate={vi.fn()}
        onRename={vi.fn().mockResolvedValue(undefined)}
        onArchive={vi.fn().mockResolvedValue(undefined)}
        onDelete={vi.fn()}
      />,
    );

    const projectHeading = screen.getByText("项目对话", { selector: ".ant-typography" });
    const ordinaryHeading = screen.getByText("无项目对话");
    expect(projectHeading.compareDocumentPosition(ordinaryHeading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    const item = document.querySelector(".ant-collapse-item");
    expect(item).not.toBeNull();
    await waitFor(() => expect(item).toHaveClass("ant-collapse-item-active"));
    await user.click(item?.querySelector(".ant-collapse-header") as HTMLElement);
    await waitFor(() => expect(item).not.toHaveClass("ant-collapse-item-active"));
  });

  it("shows project settings as list buttons and dispatches rename, path, and removal actions", async () => {
    const user = userEvent.setup();
    const onRenameProject = vi.fn().mockResolvedValue(undefined);
    const onChangeProjectPath = vi.fn().mockResolvedValue(undefined);
    const onRemoveProject = vi.fn().mockResolvedValue(undefined);
    render(
      <AppSidebar
        profile={{ display_name: "账户名称", agent_preferences: "" }}
        conversations={[projectConversation]}
        projects={[project]}
        archivedCount={0}
        currentId={null}
        page="chat"
        onNew={vi.fn()}
        onSelect={vi.fn()}
        onNavigate={vi.fn()}
        onRename={vi.fn().mockResolvedValue(undefined)}
        onArchive={vi.fn().mockResolvedValue(undefined)}
        onDelete={vi.fn()}
        onRenameProject={onRenameProject}
        onChangeProjectPath={onChangeProjectPath}
        onRemoveProject={onRemoveProject}
      />,
    );

    await user.click(screen.getByRole("button", { name: "项目设置 示例项目" }));
    expect(screen.getByRole("button", { name: "修改项目名称" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "修改项目路径" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "删除项目" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "修改项目名称" }));
    const nameInput = screen.getByRole("textbox", { name: "项目名称" });
    await user.clear(nameInput);
    await user.type(nameInput, "重命名项目");
    await user.click(screen.getByRole("button", { name: /保.*存/ }));
    await waitFor(() => expect(onRenameProject).toHaveBeenCalledWith("project-1", "重命名项目"));

    await user.click(screen.getByRole("button", { name: "项目设置 示例项目" }));
    await user.click(screen.getByRole("button", { name: "修改项目路径" }));
    await waitFor(() => expect(onChangeProjectPath).toHaveBeenCalledWith("project-1"));

    await user.click(screen.getByRole("button", { name: "项目设置 示例项目" }));
    await user.click(screen.getByRole("button", { name: "删除项目" }));
    await user.click(screen.getByRole("button", { name: /移.*除/ }));
    await waitFor(() => expect(onRemoveProject).toHaveBeenCalledWith("project-1"));
  });

  it("does not render an archive badge when there are no unread archives", () => {
    renderSidebar();
    expect(screen.getByRole("button", { name: "回收站" })).toBeInTheDocument();
    expect(document.querySelector(".ant-badge-count")).not.toBeInTheDocument();
  });
  it("uses only the spinner for a running conversation", () => {
    const running: Conversation = {
      ...conversation,
      messages: [{ id: "assistant-1", role: "assistant", content: "", events: [], running: true }],
    };
    renderSidebar(0, [running]);

    expect(document.querySelector(".ant-spin")).toBeInTheDocument();
    expect(document.querySelector(".ant-badge-status")).not.toBeInTheDocument();
  });
  it("opens the profile card and saves the username and agent preferences", async () => {
    const user = userEvent.setup();
    const { onProfileUpdate } = renderSidebar();

    await user.click(screen.getByRole("button", { name: "个人简介：账户名称" }));
    expect(screen.getByText("个人简介", { selector: ".ant-popover-title" })).toBeInTheDocument();
    await user.clear(screen.getByRole("textbox", { name: "用户名" }));
    await user.type(screen.getByRole("textbox", { name: "用户名" }), "小明");
    await user.type(screen.getByRole("textbox", { name: "Agent 偏好" }), "先给结论，再给步骤");
    await user.click(screen.getByRole("button", { name: "保存" }));

    expect(onProfileUpdate).toHaveBeenCalledWith({
      display_name: "小明",
      agent_preferences: "先给结论，再给步骤",
    });
  });
  it("uses the guest username instead of an email fallback", () => {
    render(
      <AppSidebar
        profile={{ display_name: "本地用户", agent_preferences: "" }}
        conversations={[conversation]}
        archivedCount={0}
        currentId={conversation.id}
        page="chat"
        onNew={vi.fn()}
        onSelect={vi.fn()}
        onNavigate={vi.fn()}
        onRename={vi.fn().mockResolvedValue(undefined)}
        onArchive={vi.fn().mockResolvedValue(undefined)}
        onDelete={vi.fn()}
        onProfileUpdate={vi.fn().mockResolvedValue(undefined)}
      />,
    );
    expect(screen.getByRole("button", { name: "个人简介：本地用户" })).toBeInTheDocument();
  });

  it("shows the guest username when settings are opened from the chat sidebar", () => {
    render(
      <AppSidebar
        profile={{ display_name: "本地用户", agent_preferences: "" }}
        conversations={[conversation]}
        archivedCount={0}
        currentId={conversation.id}
        page="chat"
        onNew={vi.fn()}
        onSelect={vi.fn()}
        onNavigate={vi.fn()}
        onRename={vi.fn().mockResolvedValue(undefined)}
        onArchive={vi.fn().mockResolvedValue(undefined)}
        onDelete={vi.fn()}
        onOpenSettings={vi.fn()}
      />,
    );
    const trigger = screen.getByRole("button", { name: "个人简介：本地用户" });
    expect(trigger).toHaveTextContent("本地用户");
    expect(trigger.querySelector(".profile-trigger-label-text")).toHaveTextContent("本地用户");
  });

  it("truncates long names and scrolls them only while hovered", () => {
    render(
      <AppSidebar
        profile={{ display_name: "这是一个很长的用户名称用于测试悬浮循环滚动", agent_preferences: "" }}
        conversations={[conversation]}
        archivedCount={0}
        currentId={conversation.id}
        page="chat"
        onNew={vi.fn()}
        onSelect={vi.fn()}
        onNavigate={vi.fn()}
        onRename={vi.fn().mockResolvedValue(undefined)}
        onArchive={vi.fn().mockResolvedValue(undefined)}
        onDelete={vi.fn()}
        onOpenSettings={vi.fn()}
      />,
    );
    const viewport = document.querySelector<HTMLElement>(".profile-trigger-label-viewport");
    const text = document.querySelector<HTMLElement>(".profile-trigger-label-text");
    if (!viewport || !text) throw new Error("profile label was not rendered");
    Object.defineProperty(viewport, "clientWidth", { configurable: true, value: 80 });
    Object.defineProperty(text, "scrollWidth", { configurable: true, value: 240 });

    fireEvent.mouseEnter(viewport);
    expect(viewport).toHaveClass("is-scrolling");
    fireEvent.mouseLeave(viewport);
    expect(viewport).not.toHaveClass("is-scrolling");
  });

  it("does not start marquee scrolling for a short name", () => {
    render(
      <AppSidebar
        profile={{ display_name: "小明", agent_preferences: "" }}
        conversations={[conversation]}
        archivedCount={0}
        currentId={conversation.id}
        page="chat"
        onNew={vi.fn()}
        onSelect={vi.fn()}
        onNavigate={vi.fn()}
        onRename={vi.fn().mockResolvedValue(undefined)}
        onArchive={vi.fn().mockResolvedValue(undefined)}
        onDelete={vi.fn()}
        onOpenSettings={vi.fn()}
      />,
    );
    const viewport = document.querySelector<HTMLElement>(".profile-trigger-label-viewport");
    const text = document.querySelector<HTMLElement>(".profile-trigger-label-text");
    if (!viewport || !text) throw new Error("profile label was not rendered");
    Object.defineProperty(viewport, "clientWidth", { configurable: true, value: 160 });
    Object.defineProperty(text, "scrollWidth", { configurable: true, value: 32 });

    fireEvent.mouseEnter(viewport);
    expect(viewport).not.toHaveClass("is-scrolling");
  });
  it("renders history metadata and scrolls only the hovered overflowing item", () => {
    const second: Conversation = { id: "c2", title: "第二个会话", messages: [] };
    const longTitle = "这是一个足够长的历史会话摘要，用于验证悬浮时只滚动当前条目";
    renderSidebar(0, [{
      ...conversation,
      title: longTitle,
      messageCount: 12,
      messages: [{ id: "loaded-message", role: "user", content: "已打开", events: [] }],
    }, second]);

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
