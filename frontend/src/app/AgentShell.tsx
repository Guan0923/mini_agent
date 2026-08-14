import { Alert, Button, Drawer, Grid, Layout } from "antd";
import { CloseOutlined, MenuOutlined } from "@ant-design/icons";
import { useEffect, useState } from "react";
import type { AuthUser } from "../types";
import type { AgentConfig, ProviderConfig } from "../api";
import type { ChatRunRequest } from "./types";
import type { ChatMode, Conversation, DisplayMode, Page } from "../types";
import type { ProjectInfo } from "../api";
import AppSidebar from "../components/AppSidebar";
import BenchmarkPage from "../pages/BenchmarkPage";
import ChatPage from "../pages/ChatPage";
import TrashPage from "../pages/TrashPage";
import UserSettingsModal from "../components/UserSettingsModal";
import IconAction from "../components/IconAction";

export interface AgentShellProps {
  user: AuthUser | null;
  page: Page;
  current: Conversation | null;
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
  actionError: string | null;
  settingsOpen: boolean;
  setSettingsOpen: (open: boolean) => void;
  onUserUpdate: (patch: Partial<AuthUser>) => void;
  onNew: (title?: string) => Promise<string>;
  onNewProject: () => Promise<void>;
  onNewProjectConversation: (projectId: string) => Promise<void>;
  onRemoveProject: (projectId: string) => Promise<void>;
  onRenameProject: (projectId: string, name: string) => Promise<void>;
  onChangeProjectPath: (projectId: string) => Promise<void>;
  onRestoreProject: (projectId: string) => Promise<void>;
  onSelect: (id: string) => void;
  onNavigate: (page: Page) => void;
  onRename: (id: string, title: string) => Promise<void>;
  onArchive: (id: string) => Promise<void>;
  onDelete: (id: string) => Promise<void>;
  onRestore: (id: string) => Promise<void>;
  onSignOut: () => Promise<void>;
  onProfileUpdate: (profile: { display_name: string; agent_preferences: string }) => Promise<void>;
  onUpdate: (id: string, updater: (conversation: Conversation) => Conversation) => void;
  onModeChange: (mode: ChatMode) => void;
  onEnsureSession: (id: string) => Promise<string>;
  onFork: (conversationId: string, messageId: string) => Promise<void>;
  onRewind: (conversationId: string, messageId: string) => Promise<{ content: string; sessionId: string; sourceNodeId?: string } | undefined>;
  onSelectSession: (sessionId: string) => Promise<string>;
  onReload: (id: string) => Promise<void>;
  onRefresh: () => Promise<void>;
  onRun: (request: ChatRunRequest) => Promise<void>;
  onStopRun: (conversationId: string) => void;
  onClearError: () => void;
  onDisplayModeUpdate: (config: AgentConfig) => void;
  onProviderConfigUpdate: (config: ProviderConfig) => void;
}

export default function AgentShell(props: AgentShellProps) {
  const screens = Grid.useBreakpoint();
  const isMobile = screens.md === false;
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  useEffect(() => {
    if (!isMobile) setMobileSidebarOpen(false);
  }, [isMobile]);
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
      user={props.user}
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
      onSelect={select}
      onNavigate={navigate}
      onRename={props.onRename}
      onArchive={props.onArchive}
      onDelete={props.onDelete}
      onSignOut={async () => {
        closeMobile();
        await props.onSignOut();
      }}
      onProfileUpdate={props.onProfileUpdate}
      onOpenSettings={() => props.setSettingsOpen(true)}
    />
  );
  return (
    <Layout className="app-shell" style={{ minHeight: "100vh", height: "100vh" }}>
      {!isMobile && <Layout.Sider width={280} theme="light" style={{ background: "#fff" }}>{sidebar}</Layout.Sider>}
      {isMobile && <Drawer title="会话列表" placement="left" width={280} open={mobileSidebarOpen} onClose={closeMobile} styles={{ body: { padding: 0 } }}>{sidebar}</Drawer>}
      <Layout style={{ minWidth: 0, minHeight: 0 }}>
        {isMobile && <div className="mobile-sidebar-bar"><Button type="text" icon={<MenuOutlined />} onClick={() => setMobileSidebarOpen(true)} aria-label="打开会话列表">会话列表</Button></div>}
        <Layout.Content className="main" style={{ minHeight: 0 }}>
          {props.actionError && <Alert className="global-error" type="error" showIcon message={props.actionError} action={<IconAction label="关闭错误" icon={<CloseOutlined />} onClick={props.onClearError} />} />}
          {props.page === "chat" ? (
            <ChatPage
              conversation={props.current}
              mode={props.current ? props.modeBySession[props.current.sessionId ?? props.current.id] ?? "agent" : props.draftMode}
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
            />
          ) : props.page === "trash" ? <TrashPage conversations={props.archivedConversations} projects={props.removedProjects} onRestore={props.onRestore} onDelete={props.onDelete} onRestoreProject={props.onRestoreProject} /> : <BenchmarkPage />}
        </Layout.Content>
      </Layout>
      <UserSettingsModal
        open={props.settingsOpen}
        user={props.user}
        onClose={() => props.setSettingsOpen(false)}
        onUserUpdate={props.onUserUpdate}
        activeSessionId={props.current?.sessionId}
        onAgentConfigUpdate={props.onDisplayModeUpdate}
        onProviderConfigUpdate={props.onProviderConfigUpdate}
      />
    </Layout>
  );
}
