import { useEffect, useMemo, useRef, useState } from "react";
import {
  archiveSession,
  createSession,
  deleteSession,
  forkSession,
  getSettings,
  getSessionTranscript,
  listSessions,
  renameSession,
  restoreSession,
  rewindSession,
  updateProfile,
  type SessionInfo,
} from "../api";
import { useAuth } from "../auth/AuthProvider";
import { loadSessionModes, saveSessionModes } from "./sessionModes";
import { loadArchiveReadState, loadConversations, markArchivedAsRead, countUnreadArchived, summaryToConversation, transcriptToMessages, importableMessages, STORAGE_KEY, ARCHIVE_READ_KEY } from "./storage";
import type { ArchiveReadState } from "./storage";
import AgentShell from "./AgentShell";
import { createRunController } from "./runController";
import type {
  ChatMessage,
  ChatMode,
  Conversation,
  Page,
  DisplayMode,
} from "../types";
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
  const [settingsOpen, setSettingsOpen] = useState(false);
  const activeRunsRef = useRef(new Map<string, import("./types").ActiveRun>());

  useEffect(() => {
    if (!user?.id) {
      setDisplayMode("medium");
      return undefined;
    }
    let active = true;
    void getSettings()
      .then((settings) => {
        if (active) setDisplayMode(settings.agent_config.display_mode);
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
      try {
        const [active, archived, deleted] = await Promise.all([
          listSessions("active").catch(() => []),
          listSessions("archived").catch(() => []),
          listSessions("deleted").catch(() => []),
        ]);
        summaries = [...active, ...archived, ...deleted];
      } catch {
        // The local cache remains usable while the backend is unavailable.
      }
      if (disposed) return;

      const byClient = new Map<string, SessionInfo>();
      const bySession = new Map<string, SessionInfo>();
      const deletedByClient = new Map<string, SessionInfo>();
      const deletedBySession = new Map<string, SessionInfo>();
      for (const summary of summaries) {
        bySession.set(summary.session_id, summary);
        if (summary.deleted_at) {
          deletedBySession.set(summary.session_id, summary);
          if (summary.client_id) deletedByClient.set(summary.client_id, summary);
        } else {
          if (summary.client_id) byClient.set(summary.client_id, summary);
        }
      }

      const merged: Conversation[] = [];
      for (const conversation of local) {
        const exactDeleted = conversation.sessionId ? deletedBySession.get(conversation.sessionId) : undefined;
        if (exactDeleted) continue;
        const summary =
          (conversation.sessionId ? bySession.get(conversation.sessionId) : undefined) ??
          byClient.get(conversation.clientId ?? conversation.id);
        if (summary) {
          if (summary.deleted_at) continue;
          merged.push(summaryToConversation(summary, conversation));
          bySession.delete(summary.session_id);
          continue;
        }

        if (deletedByClient.has(conversation.clientId ?? conversation.id)) continue;

        if (conversation.messages.length > 0) {
          try {
            const imported = await createSession(
              conversation.title,
              conversation.clientId ?? conversation.id,
              importableMessages(conversation.messages),
            );
            merged.push(summaryToConversation(imported, conversation));
            bySession.delete(imported.session_id);
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

  const activeConversations = useMemo(
    () => conversations.filter((conversation) => !conversation.archivedAt && !conversation.deletedAt),
    [conversations],
  );
  const archivedConversations = useMemo(
    () => conversations.filter((conversation) => Boolean(conversation.archivedAt) && !conversation.deletedAt),
    [conversations],
  );
  const unreadArchivedCount = useMemo(
    () => countUnreadArchived(archivedConversations, archiveReadState),
    [archiveReadState, archivedConversations],
  );

  useEffect(() => {
    if (page === "trash") setArchiveReadState((previous) => markArchivedAsRead(previous, archivedConversations));
  }, [archivedConversations, page]);
  const current = activeConversations.find((conversation) => conversation.id === currentId) ?? activeConversations[0] ?? null;

  useEffect(() => {
    if (current && current.sessionId && !current.messagesLoaded) {
      let disposed = false;
      void getSessionTranscript(current.sessionId)
        .then((transcript) => {
          if (disposed) return;
          updateConversation(current.id, (conversation) => ({
            ...conversation,
            messages: transcriptToMessages(transcript),
            messagesLoaded: true,
          }));
        })
        .catch((error) => {
          if (!disposed) setActionError(String((error as Error).message ?? error));
        });
      return () => {
        disposed = true;
      };
    }
    return undefined;
  }, [current?.id, current?.sessionId, current?.messagesLoaded]);

  function updateConversation(id: string, updater: (conversation: Conversation) => Conversation) {
    setConversations((previous) => previous.map((conversation) => (conversation.id === id ? updater(conversation) : conversation)));
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

  const { runConversation, stopConversation } = createRunController({
    activeRuns: activeRunsRef.current,
    updateLastMessage,
    rebindRunSession,
    refreshSessions: () => refreshSessions(),
  });

  async function ensureSession(id: string): Promise<string> {
    const conversation = conversations.find((item) => item.id === id);
    if (!conversation) throw new Error("会话不存在");
    if (conversation.sessionId) {
      if (!conversation.messagesLoaded) {
        const transcript = await getSessionTranscript(conversation.sessionId);
        updateConversation(id, (currentConversation) => ({
          ...currentConversation,
          messages: transcriptToMessages(transcript),
          messagesLoaded: true,
        }));
      }
      return conversation.sessionId;
    }
    const summary = await createSession(
      conversation.title,
      conversation.clientId ?? conversation.id,
      importableMessages(conversation.messages),
    );
    updateConversation(id, (currentConversation) => summaryToConversation(summary, currentConversation));
    return summary.session_id;
  }

  async function newConversation(title?: string): Promise<string> {
    const empty = activeConversations.find((conversation) => conversation.messages.length === 0);
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
    });
    setModeBySession((currentModes) => ({ ...currentModes, [summary.session_id]: draftMode }));
    setConversations((previous) => [conversation, ...previous]);
    setCurrentId(conversation.id);
    setPage("chat");
    return conversation.id;
  }

  function selectConversation(id: string) {
    setCurrentId(id);
    setPage("chat");
  }

  async function renameConversation(id: string, title: string) {
    setActionError(null);
    try {
      const sessionId = await ensureSession(id);
      const summary = await renameSession(sessionId, title);
      updateConversation(id, (conversation) => summaryToConversation(summary, conversation));
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
      throw error;
    }
  }

  async function archiveConversation(id: string) {
    setActionError(null);
    try {
      const sessionId = await ensureSession(id);
      const summary = await archiveSession(sessionId);
      updateConversation(id, (conversation) => summaryToConversation(summary, conversation));
      if (currentId === id) setCurrentId(activeConversations.find((conversation) => conversation.id !== id)?.id ?? null);
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
    }
  }

  async function deleteConversation(id: string) {
    setActionError(null);
    try {
      const sessionId = await ensureSession(id);
      await deleteSession(sessionId);
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
      const summary = await restoreSession(sessionId);
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
      const sessionId = await ensureSession(id);
      const branchId = crypto.randomUUID();
      const prefix = source.messages.slice(0, index + 1).map((message) => ({
        ...message,
        id: crypto.randomUUID(),
        running: false,
      }));
      const summary = await forkSession(
        sessionId,
        source.messages[index].runId,
        `${source.title || "新对话"}（分支）`,
        branchId,
        importableMessages(prefix),
      );
      const branch = summaryToConversation(summary, {
        id: branchId,
        clientId: branchId,
        title: summary.title,
        messages: prefix,
        messagesLoaded: true,
      });
      setConversations((previous) => [branch, ...previous]);
      setCurrentId(branch.id);
      setPage("chat");
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
    }
  }

  async function rewindConversation(id: string, messageId: string): Promise<{ content: string; sessionId: string } | undefined> {
    setActionError(null);
    const source = conversations.find((conversation) => conversation.id === id);
    if (!source) return undefined;
    const index = source.messages.findIndex((message) => message.id === messageId);
    if (index < 0 || source.messages[index].role !== "user") return undefined;
    try {
      const sessionId = await ensureSession(id);
      const previousAssistant = [...source.messages.slice(0, index)].reverse().find((message) => message.role === "assistant");
      const summary = await rewindSession(
        sessionId,
        previousAssistant?.runId,
        source.title,
        source.clientId ?? source.id,
        importableMessages(source.messages.slice(0, index)),
      );
      updateConversation(id, (conversation) => ({
        ...summaryToConversation(summary, conversation),
        messages: conversation.messages.slice(0, index),
        messagesLoaded: true,
      }));
      setCurrentId(id);
      setPage("chat");
      return { content: source.messages[index].content, sessionId: summary.session_id };
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
      return undefined;
    }
  }

  async function reloadConversation(id: string): Promise<void> {
    const conversation = conversations.find((item) => item.id === id);
    if (!conversation) throw new Error("会话不存在");
    const sessionId = await ensureSession(id);
    const transcript = await getSessionTranscript(sessionId);
    updateConversation(id, (currentConversation) => ({
      ...currentConversation,
      messages: transcriptToMessages(transcript),
      messagesLoaded: true,
    }));
  }

  async function refreshSessions(): Promise<void> {
    const summaries = await listSessions("active");
    setConversations((previous) => {
      const next = [...previous];
      for (const summary of summaries) {
        const index = next.findIndex((item) => item.sessionId === summary.session_id || item.clientId === summary.client_id);
        if (index >= 0) next[index] = summaryToConversation(summary, next[index]);
        else next.push(summaryToConversation(summary));
      }
      return next;
    });
  }

  async function useSession(sessionId: string): Promise<string> {
    let target = conversations.find((item) => item.sessionId === sessionId);
    if (!target) {
      const summaries = await listSessions("active");
      const summary = summaries.find((item) => item.session_id === sessionId);
      if (!summary) throw new Error(`未知会话：${sessionId}`);
      target = summaryToConversation(summary);
      setConversations((previous) => [target!, ...previous]);
    }
    setCurrentId(target.id);
    setPage("chat");
    if (!target.messagesLoaded) {
      const transcript = await getSessionTranscript(sessionId);
      const messages = transcriptToMessages(transcript);
      setConversations((previous) => previous.map((item) => (
        item.id === target!.id ? { ...item, messages, messagesLoaded: true } : item
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
      archivedConversations={archivedConversations}
      unreadArchivedCount={unreadArchivedCount}
      modeBySession={modeBySession}
      draftMode={draftMode}
      displayMode={displayMode}
      actionError={actionError}
      settingsOpen={settingsOpen}
      setSettingsOpen={setSettingsOpen}
      onUserUpdate={(patch) => setUser(user ? { ...user, ...patch } : user)}
      onNew={newConversation}
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
      onClearError={() => setActionError(null)}
      onDisplayModeUpdate={(config) => setDisplayMode(config.display_mode)}
    />
  );
}

export default AgentApp;
