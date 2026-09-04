import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { App as AntApp } from "antd";
import {
  createSession,
  getSettings,
  getSessionNodes,
  listQueuedMessages,
  listSessions,
  updateProfile,
  type ProviderConfig,
} from "../api";
import type { ProjectInfo } from "../api/projects";
import { loadSessionModes, saveSessionModes } from "./sessionModes";
import {
  loadArchiveReadState,
  markArchivedAsRead,
  countUnreadArchived,
  mergeConversationSummaries,
  summaryToConversation,
  ARCHIVE_READ_KEY,
} from "./storage";
import type { ArchiveReadState } from "./storage";
import AgentShell from "./AgentShell";
import { createRunController } from "./runController";
import { effectiveDisplayMode } from "./displayMode";
import { isRuntimeTurnNode } from "./runtime/runtimeNodeNormalization";
import { withLoadedTurns } from "./conversationProjection";
import { createConversationActions } from "./conversationActions";
import { createProjectActions } from "./projectActions";
import { useSandboxHealth } from "./useSandboxHealth";
import { hydrateConversationCatalog } from "./conversationHydration";
import { useQueuedMessages } from "./useQueuedMessages";
import { useSandboxRunLifecycle } from "./useSandboxRunLifecycle";
import { subscribeVisibleSidebarRefresh } from "./sidebarRefresh";
import type {
  ChatMessage,
  ChatMode,
  Conversation,
  Page,
  DisplayMode,
  RuntimeStateNode,
  RuntimeTreeNode,
  RightPanelWindow,
  LocalProfile,
} from "../types";

export const ACTION_ERROR_MESSAGE_KEY = "mini-agent-action-error";

function AgentApp() {
  const { message } = AntApp.useApp();
  const [profile, setProfile] = useState<LocalProfile>({ display_name: "本地用户", agent_preferences: "" });
  const [page, setPage] = useState<Page>("chat");
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [archiveReadState, setArchiveReadState] = useState<ArchiveReadState>(() => loadArchiveReadState());
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [modeBySession, setModeBySession] = useState<Record<string, ChatMode>>(() => loadSessionModes(localStorage));
  const [draftMode, setDraftMode] = useState<ChatMode>("agent");
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [displayMode, setDisplayMode] = useState<DisplayMode>("medium");
  const [providerConfig, setProviderConfig] = useState<ProviderConfig | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [projects, setProjects] = useState<ProjectInfo[]>([]);
  const [removedProjects, setRemovedProjects] = useState<ProjectInfo[]>([]);
  const [projectsLoaded, setProjectsLoaded] = useState(false);
  const [projectLoading, setProjectLoading] = useState(false);
  const activeRunsRef = useRef(new Map<string, import("./types").ActiveRun>());
  const refreshPromiseRef = useRef<Promise<void> | null>(null);
  const sandboxHealth = useSandboxHealth();
  const [panelConversations, setPanelConversations] = useState<Record<string, Conversation>>({});

  const refreshSessions = useCallback((): Promise<void> => {
    if (refreshPromiseRef.current) return refreshPromiseRef.current;
    const request = Promise.resolve().then(() => listSessions("all")).then((summaries) => {
      setConversations((previous) => mergeConversationSummaries(
        previous,
        summaries,
        new Set(activeRunsRef.current.keys()),
      ));
    });
    const tracked = request.finally(() => {
      if (refreshPromiseRef.current === tracked) refreshPromiseRef.current = null;
    });
    refreshPromiseRef.current = tracked;
    return tracked;
  }, []);

  useEffect(() => {
    if (!actionError) return;
    void message.error({ content: actionError, key: ACTION_ERROR_MESSAGE_KEY });
    setActionError(null);
  }, [actionError, message]);

  useEffect(() => {
    let active = true;
    void getSettings()
      .then((settings) => {
        if (active) {
          setDisplayMode(effectiveDisplayMode(settings.agent_config.display_mode));
          setProviderConfig(settings.provider_config?.id ? settings.provider_config : null);
          setProfile({
            display_name: settings.profile.display_name.trim() || "本地用户",
            agent_preferences: settings.profile.agent_preferences,
          });
        }
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    localStorage.setItem(ARCHIVE_READ_KEY, JSON.stringify(archiveReadState));
  }, [archiveReadState]);

  useEffect(() => {
    saveSessionModes(localStorage, modeBySession);
  }, [modeBySession]);

  useEffect(() => {
    const abortAllRuns = () => {
      for (const run of activeRunsRef.current.values()) run.controller.abort();
      activeRunsRef.current.clear();
    };
    window.addEventListener("pagehide", abortAllRuns);
    return () => {
      window.removeEventListener("pagehide", abortAllRuns);
      abortAllRuns();
    };
  }, []);

  useEffect(() => {
    let disposed = false;
    void hydrateConversationCatalog().then((catalog) => {
      if (disposed) return;
      setProjects(catalog.projects);
      setRemovedProjects(catalog.removedProjects);
      setProjectsLoaded(true);
      setConversations(catalog.conversations);
    });
    return () => {
      disposed = true;
    };
  }, []);

  useEffect(() => {
    return subscribeVisibleSidebarRefresh(refreshSessions);
  }, [refreshSessions]);

  const visibleProjectIds = useMemo(() => new Set(projects.map((project) => project.project_id)), [projects]);
  const activeConversations = useMemo(
    () => conversations.filter((conversation) => (!conversation.projectId || visibleProjectIds.has(conversation.projectId)) && !conversation.archivedAt && !conversation.deletedAt),
    [conversations, visibleProjectIds],
  );
  const archivedConversations = useMemo(
    () => conversations.filter((conversation) => !conversation.projectId && Boolean(conversation.archivedAt) && !conversation.deletedAt),
    [conversations, visibleProjectIds],
  );
  const unreadArchivedCount = useMemo(
    () => countUnreadArchived(archivedConversations, archiveReadState),
    [archiveReadState, archivedConversations],
  );

  useEffect(() => {
    if (page === "trash") setArchiveReadState((previous) => markArchivedAsRead(previous, archivedConversations));
  }, [archivedConversations, page]);
  const current = activeConversations.find((conversation) => conversation.id === currentId) ?? activeConversations[0] ?? null;

  // Removing a project can hide the currently selected conversation. Keep
  // the chat page in a deterministic empty/ordinary state instead of letting
  // the fallback silently select an unrelated project session.
  useEffect(() => {
    if (currentId && !activeConversations.some((conversation) => conversation.id === currentId)) {
      setCurrentId(activeConversations.find((conversation) => !conversation.projectId)?.id ?? null);
    }
  }, [activeConversations, currentId]);

  useEffect(() => {
    if (current && current.sessionId && (!current.messagesLoaded || !current.runtimeNodes)) {
      let disposed = false;
      void getSessionNodes(current.sessionId)
        .then((nodes) => {
          if (disposed) return;
          updateConversation(current.id, (conversation) => withLoadedTurns(conversation, nodes));
        })
        .catch((error) => {
          if (!disposed) setActionError(String((error as Error).message ?? error));
        });
      return () => {
        disposed = true;
      };
    }
    return undefined;
  }, [current?.id, current?.sessionId, current?.messagesLoaded, current?.runtimeNodes]);

  function updateConversation(id: string, updater: (conversation: Conversation) => Conversation) {
    setConversations((previous) => previous.map((conversation) => (conversation.id === id ? updater(conversation) : conversation)));
    setPanelConversations((previous) => {
      const conversation = previous[id];
      return conversation ? { ...previous, [id]: updater(conversation) } : previous;
    });
  }

  const {
    queuedMessages,
    setQueuedMessages,
    updateQueuedMessages,
    refreshQueuedMessages,
  } = useQueuedMessages({
    current,
    conversations,
    panelConversations,
    onError: setActionError,
  });

  function updateLastMessage(id: string, updater: (message: ChatMessage) => ChatMessage) {
    updateConversation(id, (conversation) => {
      const messages = [...conversation.messages];
      const index = messages.length - 1;
      if (index < 0 || messages[index].role !== "assistant") return conversation;
      messages[index] = updater(messages[index]);
      return { ...conversation, messages };
    });
  }

  async function rebindRunSession(conversationId: string, sessionId: string): Promise<void> {
    if (panelConversations[conversationId]) return;
    try {
      const summaries = await listSessions("active");
      const summary = summaries.find((item) => item.session_id === sessionId);
      if (!summary) return;
      updateConversation(conversationId, (conversation) => summaryToConversation(summary, conversation));
    } catch {
      // The stream result remains usable even when a summary refresh is unavailable.
    }
  }

  async function recoverConversation(conversationId: string, sessionId: string, turnId?: string): Promise<void> {
    let nodes: RuntimeTreeNode[] = [];
    for (let attempt = 0; attempt < 40; attempt += 1) {
      nodes = await getSessionNodes(sessionId);
      const target = turnId ? nodes.find((node) => node.id === turnId) : undefined;
      if (!target || !isRuntimeTurnNode(target) || target.status !== "running") break;
      await new Promise<void>((resolve) => globalThis.setTimeout(resolve, 50));
    }
    updateConversation(conversationId, (conversation) => withLoadedTurns(conversation, nodes));
  }

  const hydratePanelConversation = useCallback(async (window: RightPanelWindow): Promise<void> => {
    if (window.kind !== "side_chat" || !window.thread_id || !window.anchor_turn_id) return;
    const nodes = await getSessionNodes(window.session_id);
    setPanelConversations((previous) => {
      const existing = previous[window.id];
      const base: Conversation = existing ?? {
        id: window.id,
        title: window.title,
        sessionId: window.session_id,
        threadId: window.thread_id!,
        hiddenBeforeTurnId: window.anchor_turn_id!,
        messages: [],
        messagesLoaded: false,
      };
      return {
        ...previous,
        [window.id]: withLoadedTurns({
          ...base,
          title: window.title,
          hiddenBeforeTurnId: window.anchor_turn_id!,
        }, nodes),
      };
    });
    const items = await listQueuedMessages(window.thread_id).catch(() => []);
    setQueuedMessages((previous) => new Map(previous).set(window.id, items));
  }, []);

  const forgetPanelConversation = useCallback((windowId: string) => {
    setPanelConversations((previous) => {
      if (!previous[windowId]) return previous;
      const next = { ...previous };
      delete next[windowId];
      return next;
    });
    setQueuedMessages((previous) => {
      if (!previous.has(windowId)) return previous;
      const next = new Map(previous);
      next.delete(windowId);
      return next;
    });
  }, []);

  async function rewindPanelConversation(id: string, messageId: string) {
    const source = panelConversations[id];
    if (!source?.sessionId) return undefined;
    const message = source.messages.find((item) => item.id === messageId);
    if (!message || message.role !== "user" || !message.nodeId) return undefined;
    return {
      content: message.content,
      sessionId: source.sessionId,
      threadId: source.threadId,
      sourceNodeId: message.nodeId,
      rewindTurnId: message.nodeId,
    };
  }

  async function reloadPanelConversation(id: string, preferredActiveTurnId?: string): Promise<void> {
    const source = panelConversations[id];
    if (!source?.sessionId) throw new Error("侧聊不存在");
    const nodes = await getSessionNodes(source.sessionId);
    updateConversation(id, (conversation) => withLoadedTurns(conversation, nodes, preferredActiveTurnId));
  }

  const { runConversation, stopConversation } = createRunController({
    activeRuns: activeRunsRef.current,
    updateLastMessage,
    rebindRunSession,
    refreshSessions: () => refreshSessions(),
    updateConversation,
    recoverConversation,
  });

  useSandboxRunLifecycle({
    sandboxHealth,
    activeRunsRef,
    conversations,
    panelConversations,
    current,
    setConversations,
    setPanelConversations,
    updateConversation,
    runConversation,
  });

  async function runConversationWithSandbox(request: import("./types").ChatRunRequest): Promise<void> {
    if (sandboxHealth.phase !== "healthy") {
      throw new Error(sandboxHealth.phase === "checking"
        ? "正在检查沙箱 Broker，暂时无法运行 Agent。"
        : `沙箱 Broker 不可用：${sandboxHealth.detail ?? "健康检查未通过。"}`);
    }
    await runConversation(request);
  }

  async function ensureSession(id: string): Promise<string> {
    const conversation = conversations.find((item) => item.id === id);
    if (!conversation) throw new Error("会话不存在");
    if (conversation.sessionId) {
      if (!conversation.messagesLoaded) {
        const nodes = await getSessionNodes(conversation.sessionId);
        updateConversation(id, (currentConversation) => withLoadedTurns(currentConversation, nodes));
      }
      return conversation.sessionId;
    }
    const summary = await createSession(conversation.title, conversation.clientId ?? conversation.id);
    updateConversation(id, (currentConversation) => summaryToConversation(summary, currentConversation));
    return summary.session_id;
  }

  async function newConversation(title?: string): Promise<string> {
    const empty = activeConversations.find(
      (conversation) =>
        !conversation.projectId &&
        conversation.messages.length === 0 &&
        (conversation.messageCount ?? 0) === 0,
    );
    if (empty && !title) {
      setCurrentId(empty.id);
      setPage("chat");
      return empty.id;
    }
    const clientId = crypto.randomUUID();
    const summary = await createSession(title?.trim() || "新对话", clientId);
    const conversation = summaryToConversation(summary, {
      id: summary.session_id,
      clientId,
      title: title?.trim() || "新对话",
      messages: [],
      messagesLoaded: true,
      runtimeNodes: [],
    });
    setModeBySession((currentModes) => ({ ...currentModes, [summary.session_id]: draftMode }));
    setConversations((previous) => [conversation, ...previous]);
    setCurrentId(conversation.id);
    setPage("chat");
    return conversation.id;
  }

  const {
    newProject,
    newProjectConversation,
    removeProjectFromSidebar,
    renameProjectFromSidebar,
    changeProjectPathFromSidebar,
    revokeProjectSkillTrustFromSidebar,
    restoreProjectFromTrash,
  } = createProjectActions({
    projectLoading,
    activeConversations,
    setProjectLoading,
    setProjects,
    setRemovedProjects,
    setConversations,
    setCurrentId,
    setPage,
    setActionError,
    refreshSessions,
  });

  const {
    renameConversation,
    archiveConversation,
    deleteConversation,
    restoreConversation,
    forkConversation,
    rewindConversation,
    reloadConversation,
    useSession,
  } = createConversationActions({
    conversations,
    activeConversations,
    currentId,
    ensureSession,
    updateConversation,
    setConversations,
    setCurrentId,
    setPage,
    setActionError,
  });

  function setConversationMode(conversation: Conversation | null, mode: ChatMode) {
    if (!conversation) {
      setDraftMode(mode);
      return;
    }
    const key = conversation.threadId ?? conversation.sessionId ?? conversation.id;
    setModeBySession((currentModes) => ({ ...currentModes, [key]: mode }));
  }

  return (
    <AgentShell
      profile={profile}
      page={page}
      current={current}
      panelConversations={panelConversations}
      activeConversations={activeConversations}
      projects={projects}
      projectsLoaded={projectsLoaded}
      removedProjects={removedProjects}
      projectLoading={projectLoading}
      archivedConversations={archivedConversations}
      unreadArchivedCount={unreadArchivedCount}
      modeBySession={modeBySession}
      draftMode={draftMode}
      displayMode={displayMode}
      providerConfig={providerConfig}
      settingsOpen={settingsOpen}
      setSettingsOpen={setSettingsOpen}
      onProfileChange={setProfile}
      onNew={newConversation}
      onNewProject={newProject}
      onNewProjectConversation={newProjectConversation}
      onRemoveProject={removeProjectFromSidebar}
      onRenameProject={renameProjectFromSidebar}
      onChangeProjectPath={changeProjectPathFromSidebar}
      onRevokeSkillTrust={revokeProjectSkillTrustFromSidebar}
      onRestoreProject={restoreProjectFromTrash}
      onSelect={(id) => { setCurrentId(id); setPage("chat"); }}
      onNavigate={setPage}
      onRename={renameConversation}
      onArchive={archiveConversation}
      onDelete={deleteConversation}
      onRestore={restoreConversation}
      onProfileUpdate={async (profile) => {
        const updated = await updateProfile(profile);
        setProfile(updated);
      }}
      onUpdate={updateConversation}
      onModeChange={(mode) => setConversationMode(current, mode)}
      onPanelModeChange={(id, mode) => setConversationMode(panelConversations[id] ?? null, mode)}
      onHydratePanelConversation={hydratePanelConversation}
      onForgetPanelConversation={forgetPanelConversation}
      onEnsureSession={ensureSession}
      onFork={forkConversation}
      onRewind={rewindConversation}
      onRewindPanel={rewindPanelConversation}
      onSelectSession={useSession}
      onReload={reloadConversation}
      onReloadPanel={reloadPanelConversation}
      onRefresh={refreshSessions}
      onRun={runConversationWithSandbox}
      onStopRun={stopConversation}
      queuedMessages={queuedMessages}
      onQueuedMessagesChange={updateQueuedMessages}
      onQueuedMessagesRefresh={refreshQueuedMessages}
      onDisplayModeUpdate={(config) => setDisplayMode(effectiveDisplayMode(config.display_mode))}
      onProviderConfigUpdate={setProviderConfig}
      sandboxHealth={sandboxHealth}
    />
  );
}

export default AgentApp;
