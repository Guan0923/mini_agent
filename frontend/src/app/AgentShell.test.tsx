import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Grid } from "antd";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import AgentShell, {
  DEFAULT_RIGHT_PANEL_WIDTH,
  RIGHT_PANEL_CLOSE_THRESHOLD,
  rightPanelPreviewWidth,
  rightPanelResizeOutcome,
  type AgentShellProps,
} from "./AgentShell";

const rightPanelApi = vi.hoisted(() => ({
  getRightPanel: vi.fn(),
  updateRightPanel: vi.fn(),
  createSideChat: vi.fn(),
  createPanelTerminal: vi.fn(),
  renameRightPanelWindow: vi.fn(),
  closeRightPanelWindow: vi.fn(),
}));

vi.mock("../api", async (importOriginal) => ({
  ...await importOriginal<typeof import("../api")>(),
  ...rightPanelApi,
}));

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
  rightPanelApi.getRightPanel.mockResolvedValue({
    state: { session_id: "session", width: 420, collapsed: false, active_window_id: null },
    windows: [],
    capabilities: { terminal_available: true, terminal_unavailable_reason: null },
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

function makeProps(overrides: Partial<AgentShellProps> = {}): AgentShellProps {
  return {
    profile: { display_name: "本地用户", agent_preferences: "" },
    page: "chat",
    current: null,
    panelConversations: {},
    activeConversations: [],
    projects: [],
    removedProjects: [],
    archivedConversations: [],
    unreadArchivedCount: 0,
    modeBySession: {},
    draftMode: "agent",
    displayMode: "medium",
    providerConfig: null,
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
    onReorderSidebar: vi.fn().mockResolvedValue(undefined),
    onSortSidebar: vi.fn().mockResolvedValue(undefined),
    onRestore: vi.fn().mockResolvedValue(undefined),
    onProfileUpdate: vi.fn().mockResolvedValue(undefined),
    onUpdate: vi.fn(),
    onModeChange: vi.fn(),
    onPanelModeChange: vi.fn(),
    onHydratePanelConversation: vi.fn().mockResolvedValue(undefined),
    onForgetPanelConversation: vi.fn(),
    onEnsureSession: vi.fn().mockResolvedValue("session"),
    onFork: vi.fn().mockResolvedValue(undefined),
    onRewind: vi.fn().mockResolvedValue(undefined),
    onRewindPanel: vi.fn().mockResolvedValue(undefined),
    onSelectSession: vi.fn().mockResolvedValue("session"),
    onReload: vi.fn().mockResolvedValue(undefined),
    onReloadPanel: vi.fn().mockResolvedValue(undefined),
    onRefresh: vi.fn().mockResolvedValue(undefined),
    onRun: vi.fn().mockResolvedValue(undefined),
    onStopRun: vi.fn(),
    onDisplayModeUpdate: vi.fn(),
    onProviderConfigUpdate: vi.fn(),
    sandboxHealth: {
      phase: "healthy",
      installed: true,
      code: null,
      detail: null,
      checking: false,
      repairing: false,
      reinstalling: false,
      check: vi.fn().mockResolvedValue({ installed: true, healthy: true }),
      repair: vi.fn().mockResolvedValue(undefined),
      reinstall: vi.fn().mockResolvedValue(undefined),
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

describe("AgentShell right panel sizing", () => {
  it("uses 420px by default and collapses only below 280px", () => {
    expect(DEFAULT_RIGHT_PANEL_WIDTH).toBe(420);
    expect(RIGHT_PANEL_CLOSE_THRESHOLD).toBe(280);
    expect(rightPanelPreviewWidth(279)).toBe(280);
    expect(rightPanelPreviewWidth(280)).toBe(280);
    expect(rightPanelPreviewWidth(560)).toBe(560);
    expect(rightPanelResizeOutcome(279, 560)).toEqual({
      previewWidth: 560,
      patch: { collapsed: true },
    });
    expect(rightPanelResizeOutcome(280, 560)).toEqual({
      previewWidth: 280,
      patch: { width: 280, collapsed: false },
    });
  });

  it("renders the controlled desktop Splitter at the persisted width", async () => {
    const current = {
      id: "main",
      title: "main",
      sessionId: "session",
      threadId: "session",
      activeTurnId: "turn-main",
      lastNodeId: "turn-main",
      messages: [],
      messagesLoaded: true,
      runtimeNodes: [{ id: "turn-main", session_id: "session", thread_id: "session", cwd: "C:\\work" }],
    } as AgentShellProps["current"];
    const { container } = render(<AgentShell {...makeProps({ current })} />);

    await waitFor(() => expect(container.querySelector(".ant-splitter")).toBeInTheDocument());
    const panels = container.querySelectorAll<HTMLElement>(".ant-splitter-panel");
    expect(panels).toHaveLength(2);
    expect(panels[1].style.flexBasis).toBe("420px");
  });

  it("uses a full-width Drawer on mobile and hides the closed-edge launcher while open", async () => {
    vi.mocked(Grid.useBreakpoint).mockReturnValue({ md: false } as ReturnType<typeof Grid.useBreakpoint>);
    const current = {
      id: "main",
      title: "main",
      sessionId: "session",
      threadId: "session",
      activeTurnId: "turn-main",
      lastNodeId: "turn-main",
      messages: [],
      messagesLoaded: true,
      runtimeNodes: [{ id: "turn-main", session_id: "session", thread_id: "session", cwd: "C:\\work" }],
    } as AgentShellProps["current"];
    const { container } = render(<AgentShell {...makeProps({ current })} />);

    await waitFor(() => expect(document.querySelector(".ant-drawer-open")).toBeInTheDocument());
    expect(container.querySelector(".ant-splitter")).not.toBeInTheDocument();
    expect(document.querySelector<HTMLElement>(".ant-drawer-content-wrapper")?.style.width).toBe("100%");
    expect(screen.queryByLabelText("打开右侧边栏")).not.toBeInTheDocument();
  });
});
