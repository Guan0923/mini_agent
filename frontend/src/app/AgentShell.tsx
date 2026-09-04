import { Button, Drawer, Grid, Layout, Splitter } from "antd";
import { MenuOutlined } from "@ant-design/icons";
import { useEffect, useRef, useState } from "react";
import type { LocalProfile, RightPanelWindow } from "../types";
import type { AgentConfig, ProviderConfig, SidebarThreadSort } from "../api";
import type { ChatRunRequest } from "./types";
import type { ChatMode, Conversation, DisplayMode, Page } from "../types";
import type { ProjectInfo } from "../api";
import type { QueuedMessage } from "./types";
import AppSidebar from "../components/AppSidebar";
import BenchmarkPage from "../pages/BenchmarkPage";
import ChatPage from "../pages/ChatPage";
import TrashPage from "../pages/TrashPage";
import UserSettingsModal from "../components/UserSettingsModal";
import IconAction from "../components/IconAction";
import type { SandboxHealthState } from "./useSandboxHealth";
import RightPanel, { RightPanelLauncher, useRightPanel } from "../components/rightPanel/RightPanel";

export const DEFAULT_RIGHT_PANEL_WIDTH = 420;
export const RIGHT_PANEL_CLOSE_THRESHOLD = 280;

export function rightPanelPreviewWidth(width: number) {
  return Math.max(width, RIGHT_PANEL_CLOSE_THRESHOLD);
}

export function rightPanelResizeOutcome(width: number, savedWidth: number) {
  return width < RIGHT_PANEL_CLOSE_THRESHOLD
    ? { previewWidth: savedWidth || DEFAULT_RIGHT_PANEL_WIDTH, patch: { collapsed: true } as const }
    : { previewWidth: width, patch: { width, collapsed: false } as const };
}

export interface AgentShellProps {
  profile: LocalProfile;
  page: Page;
  current: Conversation | null;
  panelConversations: Record<string, Conversation>;
  activeConversations: Conversation[];
  projects: ProjectInfo[];
  projectsLoaded?: boolean;
  removedProjects: ProjectInfo[];
  projectLoading?: boolean;
  archivedConversations: Conversation[];
  unreadArchivedCount: number;
  modeBySession: Record<string, ChatMode>;
  draftMode: ChatMode;
  displayMode: DisplayMode;
  providerConfig: ProviderConfig | null;
  settingsOpen: boolean;
  setSettingsOpen: (open: boolean) => void;
  onProfileChange: (profile: LocalProfile) => void;
  onNew: (title?: string) => Promise<string>;
  onNewProject: () => Promise<void>;
  onNewProjectConversation: (projectId: string) => Promise<void>;
  onRemoveProject: (projectId: string) => Promise<void>;
  onRenameProject: (projectId: string, name: string) => Promise<void>;
  onChangeProjectPath: (projectId: string) => Promise<void>;
  onRevokeSkillTrust: (projectId: string) => Promise<void>;
  onRestoreProject: (projectId: string) => Promise<void>;
  onSelect: (id: string) => void;
  onNavigate: (page: Page) => void;
  onRename: (id: string, title: string) => Promise<void>;
  onArchive: (id: string) => Promise<void>;
  onDelete: (id: string) => Promise<void>;
  onReorderSidebar: (projectId: string | null, orderedThreadIds: string[]) => Promise<void>;
  onSortSidebar: (projectId: string | null, sortBy: SidebarThreadSort) => Promise<void>;
  onRestore: (id: string) => Promise<void>;
  onProfileUpdate: (profile: { display_name: string; agent_preferences: string }) => Promise<void>;
  onUpdate: (id: string, updater: (conversation: Conversation) => Conversation) => void;
  onModeChange: (mode: ChatMode) => void;
  onPanelModeChange: (id: string, mode: ChatMode) => void;
  onHydratePanelConversation: (window: RightPanelWindow) => Promise<void>;
  onForgetPanelConversation: (windowId: string) => void;
  onEnsureSession: (id: string) => Promise<string>;
  onFork: (conversationId: string, messageId: string) => Promise<void>;
  onRewind: (conversationId: string, messageId: string) => Promise<{ content: string; sessionId: string; sourceNodeId?: string; rewindTurnId?: string } | undefined>;
  onRewindPanel: (conversationId: string, messageId: string) => Promise<{ content: string; sessionId: string; sourceNodeId?: string; rewindTurnId?: string } | undefined>;
  onSelectSession: (sessionId: string) => Promise<string>;
  onReload: (id: string, preferredActiveTurnId?: string) => Promise<void>;
  onReloadPanel: (id: string, preferredActiveTurnId?: string) => Promise<void>;
  onRefresh: () => Promise<void>;
  onRun: (request: ChatRunRequest) => Promise<void>;
  onStopRun: (conversationId: string) => void;
  queuedMessages?: Map<string, QueuedMessage[]>;
  onQueuedMessagesChange?: (conversationId: string, updater: (items: QueuedMessage[]) => QueuedMessage[]) => void;
  onQueuedMessagesRefresh?: (conversationId: string) => Promise<void>;
  onDisplayModeUpdate: (config: AgentConfig) => void;
  onProviderConfigUpdate: (config: ProviderConfig) => void;
  sandboxHealth: SandboxHealthState;
}

export default function AgentShell(props: AgentShellProps) {
  const screens = Grid.useBreakpoint();
  const isMobile = screens.md === false;
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [chatRevealKey, setChatRevealKey] = useState(0);
  const [previewPanelWidth, setPreviewPanelWidth] = useState(DEFAULT_RIGHT_PANEL_WIDTH);
  const rawPanelWidthRef = useRef(DEFAULT_RIGHT_PANEL_WIDTH);
  const panel = useRightPanel(
    props.current?.sessionId,
    props.current?.activeTurnId ?? props.current?.lastNodeId,
    props.onHydratePanelConversation,
    props.onForgetPanelConversation,
  );
  useEffect(() => {
    if (isMobile) setSidebarCollapsed(false);
    else setMobileSidebarOpen(false);
  }, [isMobile]);
  useEffect(() => {
    if (props.page === "chat") setChatRevealKey((current) => current + 1);
  }, [props.page]);
  useEffect(() => {
    const width = panel.payload?.state.width || DEFAULT_RIGHT_PANEL_WIDTH;
    rawPanelWidthRef.current = width;
    setPreviewPanelWidth(width);
  }, [props.current?.sessionId, panel.payload?.state.width]);
  const closeMobile = () => setMobileSidebarOpen(false);
  const navigate = (page: Page) => {
    props.onNavigate(page);
    closeMobile();
  };
  const select = (id: string) => {
    props.onSelect(id);
    closeMobile();
  };
  const create = async (title?: string) => {
    const id = await props.onNew(title);
    closeMobile();
    return id;
  };
  const createProject = async () => {
    await props.onNewProject();
    closeMobile();
  };
  const createProjectConversation = async (projectId: string) => {
    await props.onNewProjectConversation(projectId);
    closeMobile();
  };
  const useSession = async (sessionId: string) => {
    const id = await props.onSelectSession(sessionId);
    closeMobile();
    return id;
  };
  const sidebar = (
    <AppSidebar
      profile={props.profile}
      conversations={props.activeConversations}
      projects={props.projects}
      projectsLoaded={props.projectsLoaded}
      projectLoading={props.projectLoading}
      archivedCount={props.unreadArchivedCount}
      currentId={props.current?.id ?? null}
      page={props.page}
      onNew={create}
      onNewProject={createProject}
      onNewProjectConversation={createProjectConversation}
      onRemoveProject={props.onRemoveProject}
      onRenameProject={props.onRenameProject}
      onChangeProjectPath={props.onChangeProjectPath}
      onRevokeSkillTrust={props.onRevokeSkillTrust}
      onSelect={select}
      onNavigate={navigate}
      onRename={props.onRename}
      onArchive={props.onArchive}
      onDelete={props.onDelete}
      onReorder={props.onReorderSidebar}
      onSort={props.onSortSidebar}
      onProfileUpdate={props.onProfileUpdate}
      onOpenSettings={() => props.setSettingsOpen(true)}
      collapsed={sidebarCollapsed}
      onToggleCollapse={() => {
        if (isMobile) closeMobile();
        else setSidebarCollapsed((current) => !current);
      }}
      revealKey={chatRevealKey}
    />
  );
  const sourceTurnId = props.current?.activeTurnId ?? props.current?.lastNodeId;
  const sourceTurn = props.current?.runtimeNodes?.find((node) => node.id === sourceTurnId);
  const sourceAvailable = Boolean(props.current?.sessionId && sourceTurnId);
  const terminalReason = !sourceAvailable
    ? "当前没有可用 Turn"
    : !sourceTurn || !("cwd" in sourceTurn) || !sourceTurn.cwd
      ? "当前 Turn 没有可用 cwd"
      : panel.payload?.capabilities.terminal_unavailable_reason ?? "配置的终端当前不可用";
  const terminalAvailable = sourceAvailable
    && Boolean(sourceTurn && "cwd" in sourceTurn && sourceTurn.cwd)
    && panel.payload?.capabilities.terminal_available === true;
  const mainContent = (
    <>
      {props.page === "chat" ? (
        <ChatPage
          conversation={props.current}
          agentThreadNavigation
          mode={props.current ? props.modeBySession[props.current.threadId ?? props.current.sessionId ?? props.current.id] ?? "agent" : props.draftMode}
          displayMode={props.displayMode}
          providerConfig={props.providerConfig}
          onModeChange={props.onModeChange}
          onUpdate={props.onUpdate}
          onNew={create}
          onNavigate={navigate}
          onEnsureSession={props.onEnsureSession}
          onFork={props.onFork}
          onRewind={props.onRewind}
          onSelectSession={useSession}
          onReload={props.onReload}
          onRefresh={props.onRefresh}
          running={Boolean(props.current?.messages.some((message) => message.running))}
          onRun={props.onRun}
          onStopRun={props.onStopRun}
          queuedMessages={props.queuedMessages?.get(props.current?.id ?? "") ?? []}
          onQueuedMessagesChange={props.onQueuedMessagesChange}
          onQueuedMessagesRefresh={props.onQueuedMessagesRefresh}
          sandboxHealth={props.sandboxHealth}
        />
      ) : props.page === "trash" ? <TrashPage conversations={props.archivedConversations} projects={props.removedProjects} onRestore={props.onRestore} onDelete={props.onDelete} onRestoreProject={props.onRestoreProject} /> : <BenchmarkPage />}
    </>
  );
  const renderSideChat = (window: RightPanelWindow) => {
    const conversation = props.panelConversations[window.id] ?? null;
    return (
      <div className="right-panel-side-chat">
        <ChatPage
          conversation={conversation}
          mode={conversation ? props.modeBySession[conversation.threadId ?? conversation.id] ?? "agent" : "agent"}
          displayMode={props.displayMode}
          providerConfig={props.providerConfig}
          onModeChange={(mode) => props.onPanelModeChange(window.id, mode)}
          onUpdate={props.onUpdate}
          onNew={create}
          onNavigate={navigate}
          onEnsureSession={async () => window.session_id}
          onRewind={props.onRewindPanel}
          onSelectSession={useSession}
          onReload={props.onReloadPanel}
          onRefresh={() => props.onHydratePanelConversation(window)}
          running={Boolean(conversation?.messages.some((message) => message.running))}
          onRun={props.onRun}
          onStopRun={props.onStopRun}
          queuedMessages={props.queuedMessages?.get(window.id) ?? []}
          onQueuedMessagesChange={props.onQueuedMessagesChange}
          onQueuedMessagesRefresh={props.onQueuedMessagesRefresh}
          sandboxHealth={props.sandboxHealth}
        />
      </div>
    );
  };
  const rightPanel = <RightPanel controller={panel} sourceAvailable={sourceAvailable} terminalAvailable={terminalAvailable} terminalReason={terminalReason} renderSideChat={renderSideChat} />;
  const panelOpen = props.page === "chat" && Boolean(props.current?.sessionId) && panel.payload?.state.collapsed === false;
  return (
    <Layout className={`app-shell${sidebarCollapsed && !isMobile ? " app-shell--sidebar-collapsed" : ""}`} style={{ minHeight: "100vh", height: "100vh" }}>
      {!isMobile && <Layout.Sider id="chat-sidebar" width={280} collapsed={sidebarCollapsed} collapsedWidth={0} trigger={null} theme="light" style={{ background: "#f4f7f8", boxShadow: "4px 0 12px rgba(0, 0, 0, 0.08)", zIndex: 1 }}>{sidebar}</Layout.Sider>}
      {isMobile && <Drawer title="会话列表" placement="left" size={280} open={mobileSidebarOpen} onClose={closeMobile} styles={{ body: { padding: 0 } }}>{sidebar}</Drawer>}
      <Layout style={{ minWidth: 0, minHeight: 0 }}>
        {sidebarCollapsed && !isMobile ? <Button className="sidebar-reopen-button" type="default" size="small" onClick={() => setSidebarCollapsed(false)} aria-label="展开侧边栏" aria-expanded={false} aria-controls="chat-sidebar" icon={<MenuOutlined />} /> : null}
        {isMobile && <div className="mobile-sidebar-bar"><Button type="text" icon={<MenuOutlined />} onClick={() => setMobileSidebarOpen(true)} aria-label="打开会话列表">会话列表</Button></div>}
        <Layout.Content className="main" style={{ minHeight: 0 }}>
          {!isMobile && panelOpen ? (
            <Splitter
              style={{ width: "100%", height: "100%" }}
              onResize={(sizes) => {
                const width = Number(sizes[1]) || 0;
                rawPanelWidthRef.current = width;
                setPreviewPanelWidth(rightPanelPreviewWidth(width));
              }}
              onResizeEnd={(sizes) => {
                const reportedWidth = Number(sizes[1]) || 0;
                const width = rawPanelWidthRef.current || reportedWidth;
                const outcome = rightPanelResizeOutcome(width, panel.payload?.state.width ?? DEFAULT_RIGHT_PANEL_WIDTH);
                panel.setLayout(outcome.patch);
                rawPanelWidthRef.current = outcome.previewWidth;
                setPreviewPanelWidth(outcome.previewWidth);
              }}
            >
              <Splitter.Panel min={0}>{mainContent}</Splitter.Panel>
              <Splitter.Panel size={previewPanelWidth} min={0} max="100%">
                <div className="right-panel-shell">{rightPanel}</div>
              </Splitter.Panel>
            </Splitter>
          ) : mainContent}
          {!isMobile && props.page === "chat" && props.current?.sessionId && !panelOpen ? <RightPanelLauncher controller={panel} /> : null}
          {isMobile && props.page === "chat" && props.current?.sessionId && !panelOpen ? <RightPanelLauncher controller={panel} /> : null}
          {isMobile && props.current?.sessionId ? (
            <Drawer
              title="右侧边栏"
              placement="right"
              size="100%"
              open={panelOpen}
              onClose={() => panel.setLayout({ collapsed: true })}
              styles={{ body: { padding: 0, overflow: "hidden" } }}
            >
              <div className="right-panel-shell">{rightPanel}</div>
            </Drawer>
          ) : null}
        </Layout.Content>
      </Layout>
      <UserSettingsModal
        open={props.settingsOpen}
        profile={props.profile}
        onClose={() => props.setSettingsOpen(false)}
        onProfileChange={props.onProfileChange}
        activeSessionId={props.current?.sessionId}
        onAgentConfigUpdate={props.onDisplayModeUpdate}
        onProviderConfigUpdate={props.onProviderConfigUpdate}
        sandboxHealth={props.sandboxHealth}
      />
    </Layout>
  );
}
