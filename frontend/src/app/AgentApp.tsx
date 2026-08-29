import { useEffect, useMemo, useRef, useState } from "react";
import {
  archiveSession,
  createSession,
  deleteSession,
  forkTurn,
  getSettings,
  getSessionNodes,
  listSessions,
  renameSession,
  restoreSession,
  updateProfile,
  type ProviderConfig,
  type SessionInfo,
} from "../api";
import { changeProjectPath, createProject, createProjectSession, listProjects, removeProject, renameProject, restoreProject, revokeProjectSkillTrust, type ProjectInfo } from "../api/projects";
import { useAuth } from "../auth/AuthProvider";
import { loadSessionModes, saveSessionModes } from "./sessionModes";
import { loadArchiveReadState, loadConversations, markArchivedAsRead, countUnreadArchived, summaryToConversation, STORAGE_KEY, ARCHIVE_READ_KEY } from "./storage";
import type { ArchiveReadState } from "./storage";
import AgentShell from "./AgentShell";
import { createRunController } from "./runController";
import type { QueuedMessage } from "./types";
import { loadQueuedMessages, saveQueuedMessages } from "./queuedMessages";
import { effectiveDisplayMode } from "./displayMode";
import { projectTurnPath } from "./runtimeDetailProjection";
import type {
  ChatMessage,
  ChatMode,
  Conversation,
  Page,
  DisplayMode,
  RuntimeStateNode,
} from "../types";

function withLoadedTurns(
  conversation: Conversation,
  nodes: RuntimeStateNode[],
  preferredActiveTurnId?: string,
): Conversation {
  const threadId = conversation.threadId ?? conversation.sessionId;
  const threadNodes = nodes.filter((node) => node.thread_id === threadId);
  const selected = threadNodes.find((node) => node.id === (preferredActiveTurnId ?? conversation.activeTurnId));
  const parentIds = new Set(threadNodes.map((node) => node.parent_id).filter(Boolean));
  const leaves = threadNodes
    .filter((node) => !parentIds.has(node.id))
    .sort((left, right) => left.timestamp.localeCompare(right.timestamp));
  const fallback = leaves[leaves.length - 1];
  const activeTurnId = selected?.id ?? fallback?.id;
  const map = new Map(nodes.map((node) => [`${node.session_id}:${node.id}`, node] as const));
  return {
    ...conversation,
    runtimeNodes: nodes,
    activeTurnId,
    lastNodeId: activeTurnId,
    messages: activeTurnId ? projectTurnPath(map, activeTurnId) : [],
    messagesLoaded: true,
  };
}

function AgentApp() {
  const { user, setUser, signOut } = useAuth();
  const [page, setPage] = useState<Page>("chat");
  const storageKey = `${STORAGE_KEY}:${user?.id ?? "anonymous"}`;
  const [conversations, setConversations] = useState<Conversation[]>(() => loadConversations(storageKey));
  const [archiveReadState, setArchiveReadState] = useState<ArchiveReadState>(() => loadArchiveReadState(user?.id));
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
  const [queuedMessages, setQueuedMessages] = useState(
    () => loadQueuedMessages(localStorage, user?.id),
  );

  useEffect(() => {
    if (!user?.id) {
      setDisplayMode("medium");
      setProviderConfig(null);
      return undefined;
    }
    let active = true;
    void getSettings()
      .then((settings) => {
        if (active) {
          setDisplayMode(effectiveDisplayMode(settings.agent_config.display_mode));
          setProviderConfig(settings.provider_config?.id ? settings.provider_config : null);
          if (user) {
            const profileName = settings.profile.display_name.trim()
              || user.display_name?.trim()
              || (user.kind === "guest" ? "游客用户" : "用户");
            setUser({
              ...user,
              display_name: profileName,
              agent_preferences: settings.profile.agent_preferences,
            });
          }
        }
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, [user?.id]);

  useEffect(() => {
    localStorage.setItem(storageKey, JSON.stringify(conversations));
  }, [conversations, storageKey]);

  useEffect(() => {
    if (!user?.id) return;
    localStorage.setItem(`${ARCHIVE_READ_KEY}:${user.id}`, JSON.stringify(archiveReadState));
  }, [archiveReadState, user?.id]);

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
      // ordinary cloud-synced conversation.
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
        // re-imported as an ordinary cloud-synced conversation.
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

  function updateConversation(id: string, updater: (conversation: Conversation) => Conversation) {
    setConversations((previous) => previous.map((conversation) => (conversation.id === id ? updater(conversation) : conversation)));
  }

  function updateQueuedMessages(conversationId: string, updater: (items: QueuedMessage[]) => QueuedMessage[]) {
    setQueuedMessages((previous) => {
      const queues = new Map(previous);
      const next = updater(previous.get(conversationId) ?? []);
      if (next.length > 0) queues.set(conversationId, next);
      else queues.delete(conversationId);
      saveQueuedMessages(localStorage, user?.id, queues);
      return queues;
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
    let nodes: RuntimeStateNode[] = [];
    for (let attempt = 0; attempt < 40; attempt += 1) {
      nodes = await getSessionNodes(sessionId);
      const target = turnId ? nodes.find((node) => node.id === turnId) : undefined;
      if (!target || target.status !== "running") break;
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
    if (!current?.sessionId || !current.runtimeNodes || activeRunsRef.current.has(current.id)) return;
    const activeTurn = current.runtimeNodes.find(
      (node) => node.id === current.activeTurnId && node.status === "running",
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
  }, [current?.id, current?.sessionId, current?.activeTurnId, current?.runtimeNodes]);

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

  async function newProject(): Promise<void> {
    if (projectLoading) return;
    setProjectLoading(true);
    setActionError(null);
    try {
      const result = await createProject();
      if (!result) return;
      const conversation = summaryToConversation(result.session, {
        id: result.session.session_id,
        clientId: result.session.client_id ?? result.session.session_id,
        title: result.session.title || "新对话",
        messages: [],
        messagesLoaded: true,
        runtimeNodes: [],
      });
      setProjects((previous) => [result.project, ...previous.filter((item) => item.project_id !== result.project.project_id)]);
      setConversations((previous) => [conversation, ...previous]);
      setCurrentId(conversation.id);
      setPage("chat");
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
    } finally {
      setProjectLoading(false);
    }
  }

  async function newProjectConversation(projectId: string): Promise<void> {
    setActionError(null);
    try {
      const existing = activeConversations.find(
        (conversation) =>
          conversation.projectId === projectId &&
          conversation.messages.length === 0 &&
          (conversation.messageCount ?? 0) === 0,
      );
      if (existing) {
        setCurrentId(existing.id);
        setPage("chat");
        return;
      }
      const result = await createProjectSession(projectId, crypto.randomUUID());
      const conversation = summaryToConversation(result.session, {
        id: result.session.session_id,
        clientId: result.session.client_id ?? result.session.session_id,
        title: result.session.title || "新对话",
        messages: [],
        messagesLoaded: true,
        runtimeNodes: [],
      });
      setConversations((previous) => [conversation, ...previous]);
      setProjects((previous) => previous.map((item) => item.project_id === projectId ? result.project : item));
      setCurrentId(conversation.id);
      setPage("chat");
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
    }
  }

  async function removeProjectFromSidebar(projectId: string): Promise<void> {
    setActionError(null);
    try {
      await removeProject(projectId);
      setProjects((previous) => previous.filter((item) => item.project_id !== projectId));
      const removed = await listProjects("removed").catch(() => []);
      setRemovedProjects(removed);
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
    }
  }

  async function renameProjectFromSidebar(projectId: string, name: string): Promise<void> {
    setActionError(null);
    try {
      const updated = await renameProject(projectId, name);
      setProjects((previous) => previous.map((item) => item.project_id === projectId ? updated : item));
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
      throw error;
    }
  }

  async function changeProjectPathFromSidebar(projectId: string): Promise<void> {
    setActionError(null);
    try {
      const updated = await changeProjectPath(projectId);
      if (!updated) return;
      setProjects((previous) => previous.map((item) => item.project_id === projectId ? updated : item));
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
      throw error;
    }
  }

  async function revokeProjectSkillTrustFromSidebar(projectId: string): Promise<void> {
    setActionError(null);
    try {
      await revokeProjectSkillTrust(projectId);
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
      throw error;
    }
  }

  async function restoreProjectFromTrash(projectId: string): Promise<void> {
    setActionError(null);
    try {
      const restored = await restoreProject(projectId);
      setProjects((previous) => [restored, ...previous.filter((item) => item.project_id !== projectId)]);
      setRemovedProjects((previous) => previous.filter((item) => item.project_id !== projectId));
      await refreshSessions();
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
    }
  }

  function selectConversation(id: string) {
    setCurrentId(id);
    setPage("chat");
  }

  async function renameConversation(id: string, title: string) {
    setActionError(null);
    try {
      const conversation = conversations.find((item) => item.id === id);
      const sessionId = await ensureSession(id);
      const summary = await renameSession(conversation?.threadId ?? sessionId, title);
      updateConversation(id, (conversation) => summaryToConversation(summary, conversation));
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
      throw error;
    }
  }

  async function archiveConversation(id: string) {
    setActionError(null);
    try {
      const conversation = conversations.find((item) => item.id === id);
      const sessionId = await ensureSession(id);
      const summary = await archiveSession(conversation?.threadId ?? sessionId);
      updateConversation(id, (conversation) => summaryToConversation(summary, conversation));
      if (currentId === id) setCurrentId(activeConversations.find((conversation) => conversation.id !== id)?.id ?? null);
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
    }
  }

  async function deleteConversation(id: string) {
    setActionError(null);
    try {
      const conversation = conversations.find((item) => item.id === id);
      const sessionId = await ensureSession(id);
      await deleteSession(conversation?.threadId ?? sessionId);
      setConversations((previous) => previous.filter((conversation) => conversation.id !== id));
      if (currentId === id) setCurrentId(null);
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
    }
  }

  async function restoreConversation(id: string) {
    setActionError(null);
    try {
      const conversation = conversations.find((item) => item.id === id);
      if (!conversation) return;
      const sessionId = await ensureSession(id);
      const summary = await restoreSession(conversation.threadId ?? sessionId);
      updateConversation(id, (currentConversation) => summaryToConversation(summary, currentConversation));
      if (!currentId) setCurrentId(conversation.id);
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
    }
  }

  async function forkConversation(id: string, messageId: string) {
    setActionError(null);
    const source = conversations.find((conversation) => conversation.id === id);
    if (!source) return;
    const index = source.messages.findIndex((message) => message.id === messageId);
    if (index < 0 || source.messages[index].role !== "assistant") return;
    try {
      await ensureSession(id);
      const sourceTurnId = source.messages[index].sourceNodeId;
      if (!sourceTurnId) throw new Error("fork requires an assistant Turn");
      const forked = await forkTurn(sourceTurnId);
      const sidebar = forked.sidebar_thread;
      const branch = withLoadedTurns({
        id: sidebar.thread_id,
        clientId: sidebar.thread_id,
        sessionId: sidebar.session_id,
        threadId: sidebar.thread_id,
        activeTurnId: forked.turn.id,
        title: sidebar.title,
        messages: [],
        messagesLoaded: false,
        updatedAt: sidebar.updated_at,
      }, await getSessionNodes(sidebar.session_id));
      setConversations((previous) => [branch, ...previous]);
      setCurrentId(branch.id);
      setPage("chat");
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
    }
  }

  async function rewindConversation(id: string, messageId: string): Promise<{ content: string; sessionId: string; sourceNodeId?: string; rewindTurnId?: string } | undefined> {
    setActionError(null);
    const source = conversations.find((conversation) => conversation.id === id);
    if (!source) return undefined;
    const index = source.messages.findIndex((message) => message.id === messageId);
    if (index < 0 || source.messages[index].role !== "user") return undefined;
    try {
      const sessionId = await ensureSession(id);
      const turnId = source.messages[index].nodeId;
      if (!turnId) throw new Error("rewind requires a user Turn");
      setCurrentId(id);
      setPage("chat");
      return {
        content: source.messages[index].content,
        sessionId,
        sourceNodeId: turnId,
        rewindTurnId: turnId,
      };
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
      return undefined;
    }
  }
  async function reloadConversation(id: string, preferredActiveTurnId?: string): Promise<void> {
    const conversation = conversations.find((item) => item.id === id);
    if (!conversation) throw new Error("会话不存在");
    const sessionId = await ensureSession(id);
    const nodes = await getSessionNodes(sessionId);
    updateConversation(id, (currentConversation) => withLoadedTurns(currentConversation, nodes, preferredActiveTurnId));
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

  async function useSession(sessionId: string): Promise<string> {
    let target = conversations.find((item) => item.threadId === sessionId || item.sessionId === sessionId);
    if (!target) {
      const summaries = await listSessions("active");
      const summary = summaries.find((item) => item.thread_id === sessionId || item.session_id === sessionId);
      if (!summary) throw new Error(`未知会话：${sessionId}`);
      target = summaryToConversation(summary);
      setConversations((previous) => [target!, ...previous]);
    }
    setCurrentId(target.id);
    setPage("chat");
    if (!target.messagesLoaded) {
      const nodes = await getSessionNodes(target.sessionId ?? sessionId);
      setConversations((previous) => previous.map((item) => (
        item.id === target!.id ? withLoadedTurns(item, nodes) : item
      )));
    }
    return target.id;
  }

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
      user={user}
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
      onUserUpdate={(patch) => setUser(user ? { ...user, ...patch } : user)}
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
      onSignOut={signOut}
      onProfileUpdate={async (profile) => {
        const updated = await updateProfile(profile);
        if (user) setUser({ ...user, ...updated });
      }}
      onUpdate={updateConversation}
      onModeChange={(mode) => setConversationMode(current, mode)}
      onEnsureSession={ensureSession}
      onFork={forkConversation}
      onRewind={rewindConversation}
      onSelectSession={useSession}
      onReload={reloadConversation}
      onRefresh={refreshSessions}
      onRun={runConversation}
      onStopRun={stopConversation}
      queuedMessages={queuedMessages}
      onQueuedMessagesChange={updateQueuedMessages}
      onClearError={() => setActionError(null)}
      onDisplayModeUpdate={(config) => setDisplayMode(effectiveDisplayMode(config.display_mode))}
      onProviderConfigUpdate={setProviderConfig}
    />
  );
}

export default AgentApp;
