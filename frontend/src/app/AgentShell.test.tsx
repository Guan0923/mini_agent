import { fireEvent, render, screen } from "@testing-library/react";
import { Grid } from "antd";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import AgentShell, { type AgentShellProps } from "./AgentShell";

vi.mock("../components/AppSidebar", () => ({
  default: (props: { collapsed?: boolean; onToggleCollapse?: () => void }) => (
    <div data-testid="mock-sidebar">
      {!props.collapsed ? <button type="button" aria-label="折叠侧边栏" onClick={props.onToggleCollapse}>侧边栏</button> : null}
    </div>
  ),
}));

vi.mock("../pages/ChatPage", () => ({ default: () => <div data-testid="chat-page" /> }));
vi.mock("../pages/TrashPage", () => ({ default: () => <div data-testid="trash-page" /> }));
vi.mock("../pages/BenchmarkPage", () => ({ default: () => <div data-testid="benchmark-page" /> }));
vi.mock("../components/UserSettingsModal", () => ({ default: () => null }));

beforeEach(() => {
  vi.spyOn(Grid, "useBreakpoint").mockReturnValue({ md: true } as ReturnType<typeof Grid.useBreakpoint>);
});

afterEach(() => {
  vi.restoreAllMocks();
});

function makeProps(overrides: Partial<AgentShellProps> = {}): AgentShellProps {
  return {
    profile: { display_name: "本地用户", agent_preferences: "" },
    page: "chat",
    current: null,
    activeConversations: [],
    projects: [],
    removedProjects: [],
    archivedConversations: [],
    unreadArchivedCount: 0,
    modeBySession: {},
    draftMode: "agent",
    displayMode: "medium",
    providerConfig: null,
    actionError: null,
    settingsOpen: false,
    setSettingsOpen: vi.fn(),
    onProfileChange: vi.fn(),
    onNew: vi.fn().mockResolvedValue("new"),
    onNewProject: vi.fn().mockResolvedValue(undefined),
    onNewProjectConversation: vi.fn().mockResolvedValue(undefined),
    onRemoveProject: vi.fn().mockResolvedValue(undefined),
    onRenameProject: vi.fn().mockResolvedValue(undefined),
    onChangeProjectPath: vi.fn().mockResolvedValue(undefined),
    onRevokeSkillTrust: vi.fn().mockResolvedValue(undefined),
    onRestoreProject: vi.fn().mockResolvedValue(undefined),
    onSelect: vi.fn(),
    onNavigate: vi.fn(),
    onRename: vi.fn().mockResolvedValue(undefined),
    onArchive: vi.fn().mockResolvedValue(undefined),
    onDelete: vi.fn().mockResolvedValue(undefined),
    onRestore: vi.fn().mockResolvedValue(undefined),
    onProfileUpdate: vi.fn().mockResolvedValue(undefined),
    onUpdate: vi.fn(),
    onModeChange: vi.fn(),
    onEnsureSession: vi.fn().mockResolvedValue("session"),
    onFork: vi.fn().mockResolvedValue(undefined),
    onRewind: vi.fn().mockResolvedValue(undefined),
    onSelectSession: vi.fn().mockResolvedValue("session"),
    onReload: vi.fn().mockResolvedValue(undefined),
    onRefresh: vi.fn().mockResolvedValue(undefined),
    onRun: vi.fn().mockResolvedValue(undefined),
    onStopRun: vi.fn(),
    onClearError: vi.fn(),
    onDisplayModeUpdate: vi.fn(),
    onProviderConfigUpdate: vi.fn(),
    sandboxHealth: {
      phase: "healthy",
      installed: true,
      code: null,
      detail: null,
      checking: false,
      repairing: false,
      check: vi.fn().mockResolvedValue({ installed: true, healthy: true }),
      repair: vi.fn().mockResolvedValue(undefined),
    },
    ...overrides,
  };
}

describe("AgentShell sidebar collapse", () => {
  it("collapses the desktop sidebar to zero width and restores it", () => {
    const { container } = render(<AgentShell {...makeProps()} />);
    const sider = container.querySelector(".ant-layout-sider");

    expect(sider).not.toBeNull();
    expect(sider).not.toHaveClass("ant-layout-sider-collapsed");
    fireEvent.click(screen.getByRole("button", { name: "折叠侧边栏" }));

    expect(container.querySelector(".app-shell--sidebar-collapsed")).toBeInTheDocument();
    expect(container.querySelector(".ant-layout-sider")).toHaveClass("ant-layout-sider-collapsed");
    expect(screen.getByRole("button", { name: "展开侧边栏" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "展开侧边栏" }));
    expect(container.querySelector(".app-shell--sidebar-collapsed")).not.toBeInTheDocument();
  });

  it("does not persist collapse state in browser storage", () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    render(<AgentShell {...makeProps()} />);
    fireEvent.click(screen.getByRole("button", { name: "折叠侧边栏" }));

    expect(setItem).not.toHaveBeenCalledWith(expect.stringContaining("sidebar"), expect.anything());
    setItem.mockRestore();
  });
});
