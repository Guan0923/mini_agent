import { useEffect, useMemo, useRef, useState } from "react";
import {
  createSession,
  getSettings,
  getSessionNodes,
  listQueuedMessages,
  listSessions,
  pauseTurn,
  updateProfile,
  type ProviderConfig,
  type SessionInfo,
} from "../api";
import { listProjects, type ProjectInfo } from "../api/projects";
import { loadSessionModes, saveSessionModes } from "./sessionModes";
import { loadArchiveReadState, loadConversations, markArchivedAsRead, countUnreadArchived, summaryToConversation, STORAGE_KEY, ARCHIVE_READ_KEY } from "./storage";
import type { ArchiveReadState } from "./storage";
import AgentShell from "./AgentShell";
import { createRunController } from "./runController";
import type { QueuedMessage } from "./types";
import { effectiveDisplayMode } from "./displayMode";
import { isRuntimeTurnNode } from "./runtime/runtimeNodeNormalization";
import { withLoadedTurns } from "./conversationProjection";
import { createConversationActions } from "./conversationActions";
import { createProjectActions } from "./projectActions";
import { useSandboxHealth } from "./useSandboxHealth";
import type {
  ChatMessage,
  ChatMode,
  Conversation,
  Page,
  DisplayMode,
  RuntimeStateNode,
  RuntimeTreeNode,
  LocalProfile,
} from "../types";

function AgentApp() {
  const [profile, setProfile] = useState<LocalProfile>({ display_name: "本地用户", agent_preferences: "" });
  const [page, setPage] = useState<Page>("chat");
  const storageKey = STORAGE_KEY;
  const [conversations, setConversations] = useState<Conversation[]>(() => loadConversations(storageKey));
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
  const pausedForSandboxOutageRef = useRef(new Set<string>());
  const sandboxHealth = useSandboxHealth();
  const [queuedMessages, setQueuedMessages] = useState<Map<string, QueuedMessage[]>>(() => new Map());

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
    localStorage.setItem(storageKey, JSON.stringify(conversations));
  }, [conversations, storageKey]);

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

    async function hydrate() {
      const local = loadConversations(storageKey);
      let summaries: SessionInfo[] = [];
      let projectSessionIds = new Set<string>();
      try {
        const [active, archived, deleted, activeProjects, removedProjectItems] = await Promise.all([
          listSessions("active").catch(() => []),
          listSessions("archived").catch(() => []),
          listSessions("deleted").catch(() => []),
          listProjects("active").catch(() => []),
          listProjects("removed").catch(() => []),
        ]);
        summaries = [...active, ...archived, ...deleted];
        setProjects(activeProjects);
        setRemovedProjects(removedProjectItems);
        projectSessionIds = new Set(
          [...activeProjects, ...removedProjectItems].flatMap((project) => project.session_ids ?? []),
        );
      } catch {
        // The local cache remains usable while the backend is unavailable.
      }
      if (disposed) return;
      setProjectsLoaded(true);

      // The browser cache can predate project metadata.  The project index is
      // authoritative for session membership, so use its binding list to
      // prevent a hidden project session from being re-imported as an
      // ordinary local conversation.
      const byClient = new Map<string, SessionInfo>();
      const bySession = new Map<string, SessionInfo>();
      const deletedByClient = new Map<string, SessionInfo>();
      const deletedBySession = new Map<string, SessionInfo>();
      for (const summary of summaries) {
        bySession.set(summary.thread_id ?? summary.session_id, summary);
        if (summary.deleted_at) {
          deletedBySession.set(summary.thread_id ?? summary.session_id, summary);
          if (summary.client_id) deletedByClient.set(summary.client_id, summary);
        } else {
          if (summary.client_id) byClient.set(summary.client_id, summary);
        }
      }

      const merged: Conversation[] = [];
      for (const conversation of local) {
        const exactDeleted = deletedBySession.get(conversation.threadId ?? conversation.sessionId ?? conversation.id);
        if (exactDeleted) continue;
        const summary =
          bySession.get(conversation.threadId ?? conversation.sessionId ?? conversation.id) ??
          byClient.get(conversation.clientId ?? conversation.id);
        if (summary) {
          if (summary.deleted_at) continue;
          merged.push(summaryToConversation(summary, conversation));
          bySession.delete(summary.thread_id ?? summary.session_id);
          continue;
        }

        if (deletedByClient.has(conversation.clientId ?? conversation.id)) continue;

        if (conversation.sessionId && projectSessionIds.has(conversation.sessionId)) continue;

        // A stale browser copy of a project conversation must never be
        // re-imported as an ordinary local conversation.
        if (conversation.projectId || conversation.localOnly) continue;

        if (conversation.messages.length > 0) {
          try {
            const imported = await createSession(conversation.title, conversation.clientId ?? conversation.id);
            merged.push(summaryToConversation(imported, conversation));
            bySession.delete(imported.thread_id ?? imported.session_id);
            continue;
          } catch {
            // Preserve the legacy conversation and retry on the next operation.
          }
        }
        merged.push(conversation);
      }

      for (const summary of bySession.values()) {
        if (summary.deleted_at) continue;
        merged.push(summaryToConversation(summary));
      }
      if (!disposed) setConversations(merged);
    }

    void hydrate();
    return () => {
      disposed = true;
    };
  }, []);

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

  useEffect(() => {
    if (!current?.id || !current.threadId) return;
    let active = true;
    void listQueuedMessages(current.threadId)
      .then((items) => {
        if (!active) return;
        setQueuedMessages((previous) => {
          const next = new Map(previous);
          next.set(current.id, items);
          return next;
        });
      })
      .catch((error) => {
        if (active) setActionError(String((error as Error).message ?? error));
      });
    return () => { active = false; };
  }, [current?.id, current?.threadId]);

  function updateConversation(id: string, updater: (conversation: Conversation) => Conversation) {
    setConversations((previous) => previous.map((conversation) => (conversation.id === id ? updater(conversation) : conversation)));
  }

  function updateQueuedMessages(conversationId: string, updater: (items: QueuedMessage[]) => QueuedMessage[]) {
    setQueuedMessages((previous) => {
      const queues = new Map(previous);
      const next = updater(previous.get(conversationId) ?? []);
      if (next.length > 0) queues.set(conversationId, next);
      else queues.delete(conversationId);
      return queues;
    });
  }

  async function refreshQueuedMessages(conversationId: string): Promise<void> {
    const target = conversations.find((item) => item.id === conversationId);
    if (!target?.threadId) return;
    const items = await listQueuedMessages(target.threadId);
    setQueuedMessages((previous) => {
      const next = new Map(previous);
      next.set(conversationId, items);
      return next;
    });
  }

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

  const { runConversation, stopConversation } = createRunController({
    activeRuns: activeRunsRef.current,
    updateLastMessage,
    rebindRunSession,
    refreshSessions: () => refreshSessions(),
    updateConversation,
    recoverConversation,
  });

  useEffect(() => {
    if (sandboxHealth.phase === "healthy") {
      pausedForSandboxOutageRef.current.clear();
      return;
    }
    if (sandboxHealth.phase !== "unhealthy") return;
    const runningTurnIds = new Set<string>();
    const turnSessions = new Map<string, { conversationId: string; sessionId: string }>();
    for (const active of activeRunsRef.current.values()) {
      if (active.turnId) runningTurnIds.add(active.turnId);
    }
    for (const conversation of conversations) {
      for (const node of conversation.runtimeNodes ?? []) {
        if (isRuntimeTurnNode(node) && node.status === "running") {
          runningTurnIds.add(node.id);
          if (conversation.sessionId) {
            turnSessions.set(node.id, { conversationId: conversation.id, sessionId: conversation.sessionId });
          }
        }
      }
    }
    if (turnSessions.size > 0) {
      setConversations((previous) => previous.map((conversation) => {
        const nodes = conversation.runtimeNodes;
        if (!nodes?.some((node) => runningTurnIds.has(node.id))) return conversation;
        return withLoadedTurns(conversation, nodes.map((node) => (
          isRuntimeTurnNode(node) && runningTurnIds.has(node.id) ? { ...node, status: "paused" } : node
        )));
      }));
    }
    for (const turnId of runningTurnIds) {
      if (pausedForSandboxOutageRef.current.has(turnId)) continue;
      pausedForSandboxOutageRef.current.add(turnId);
      void pauseTurn(turnId).catch(async () => {
        pausedForSandboxOutageRef.current.delete(turnId);
        const target = turnSessions.get(turnId);
        if (!target) return;
        try {
          const nodes = await getSessionNodes(target.sessionId);
          updateConversation(target.conversationId, (conversation) => withLoadedTurns(conversation, nodes));
        } catch {
          // The next health transition or session reload retries reconciliation.
        }
      });
    }
  }, [conversations, sandboxHealth.phase]);

  useEffect(() => {
    if (
      sandboxHealth.phase !== "healthy"
      || !current?.sessionId
      || !current.runtimeNodes
      || activeRunsRef.current.has(current.id)
    ) return;
    const activeTurn = current.runtimeNodes.find(
      (node): node is RuntimeStateNode => isRuntimeTurnNode(node)
        && node.id === current.activeTurnId
        && node.status === "running",
    );
    if (!activeTurn) return;
    void runConversation({
      conversationId: current.id,
      sessionId: current.sessionId,
      threadId: activeTurn.thread_id,
      turnId: activeTurn.id,
      prompt: null,
      resume: false,
      attach: true,
      mode: activeTurn.running_mode,
      permissionMode: activeTurn.permission_mode,
      reasoningEffort: activeTurn.model.reasoning_effort,
      providerName: activeTurn.provider_name,
      model: activeTurn.model,
      sourceNodeId: activeTurn.id,
    });
  }, [current?.id, current?.sessionId, current?.activeTurnId, current?.runtimeNodes, sandboxHealth.phase]);

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

  async function refreshSessions(): Promise<void> {
    const summaries = await listSessions("active");
    setConversations((previous) => {
      const next = [...previous];
      for (const summary of summaries) {
        const index = next.findIndex((item) =>
          item.threadId === (summary.thread_id ?? summary.session_id)
          || Boolean(summary.client_id && item.clientId === summary.client_id),
        );
        if (index >= 0) next[index] = summaryToConversation(summary, next[index]);
        else next.push(summaryToConversation(summary));
      }
      return next;
    });
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
    const key = conversation.sessionId ?? conversation.id;
    setModeBySession((currentModes) => ({ ...currentModes, [key]: mode }));
  }

  return (
    <AgentShell
      profile={profile}
      page={page}
      current={current}
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
      actionError={actionError}
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
      onEnsureSession={ensureSession}
      onFork={forkConversation}
      onRewind={rewindConversation}
      onSelectSession={useSession}
      onReload={reloadConversation}
      onRefresh={refreshSessions}
      onRun={runConversationWithSandbox}
      onStopRun={stopConversation}
      queuedMessages={queuedMessages}
      onQueuedMessagesChange={updateQueuedMessages}
      onQueuedMessagesRefresh={refreshQueuedMessages}
      onClearError={() => setActionError(null)}
      onDisplayModeUpdate={(config) => setDisplayMode(effectiveDisplayMode(config.display_mode))}
      onProviderConfigUpdate={setProviderConfig}
      sandboxHealth={sandboxHealth}
    />
  );
}

export default AgentApp;
