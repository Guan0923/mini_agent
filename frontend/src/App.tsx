import { Alert, App as AntApp, Button, ConfigProvider, Drawer, Grid, Layout, Spin, Typography } from "antd";
import { CloseOutlined, MenuOutlined } from "@ant-design/icons";
import zhCN from "antd/locale/zh_CN";
import { useEffect, useMemo, useRef, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import {
  archiveSession,
  createSession,
  deleteSession,
  forkSession,
  getSessionTranscript,
  listSessions,
  renameSession,
  restoreSession,
  rewindSession,
  streamChat,
  streamResume,
  type SessionInfo,
} from "./api";
import { AuthProvider, useAuth } from "./auth/AuthProvider";
import BenchmarkPage from "./pages/BenchmarkPage";
import ChatPage from "./pages/ChatPage";
import TrashPage from "./pages/TrashPage";
import DeviceApprovalPage from "./pages/auth/DeviceApprovalPage";
import LoginPage from "./pages/auth/LoginPage";
import RegisterPage from "./pages/auth/RegisterPage";
import ResetPasswordPage from "./pages/auth/ResetPasswordPage";
import HomePage from "./pages/HomePage";
import PublicLayout from "./pages/PublicLayout";
import AppSidebar from "./components/AppSidebar";
import IconAction from "./components/IconAction";
import { loadSessionModes, saveSessionModes } from "./sessionModes";
import { oceanTheme } from "./theme";
import type {
  ChatMessage,
  ChatMode,
  Conversation,
  Page,
  PermissionMode,
  ReasoningEffort,
  StreamMessage,
} from "./types";

const STORAGE_KEY = "mini-agent-conversations";
const ARCHIVE_READ_KEY = "mini-agent-archive-read";

interface ChatRunRequest {
  conversationId: string;
  sessionId: string;
  prompt: string | null;
  resume: boolean;
  mode: ChatMode;
  permissionMode: PermissionMode;
  reasoningEffort: ReasoningEffort;
}

interface ActiveRun {
  controller: AbortController;
  sessionId: string;
}

function loadConversations(key: string): Conversation[] {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((value): value is Conversation => {
        if (!value || typeof value !== "object") return false;
        const candidate = value as Partial<Conversation>;
        return typeof candidate.id === "string" && Array.isArray(candidate.messages);
      })
      .map((conversation) => ({
        ...conversation,
        clientId: conversation.clientId ?? conversation.id,
        messagesLoaded: conversation.messagesLoaded ?? conversation.messages.length > 0,
        messages: conversation.messages.map((message) =>
          message.running
            ? { ...message, running: false, status: message.status ?? "上次运行已中断" }
            : message,
        ),
      }));
  } catch {
    return [];
  }
}

type ArchiveReadState = Record<string, string>;

function loadArchiveReadState(userId: string | undefined): ArchiveReadState {
  if (!userId) return {};
  try {
    const raw = localStorage.getItem(`${ARCHIVE_READ_KEY}:${userId}`);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    return Object.fromEntries(
      Object.entries(parsed).filter((entry): entry is [string, string] => typeof entry[1] === "string"),
    );
  } catch {
    return {};
  }
}

function markArchivedAsRead(state: ArchiveReadState, conversations: Conversation[]): ArchiveReadState {
  let changed = false;
  const next = { ...state };
  for (const conversation of conversations) {
    if (!conversation.archivedAt || next[conversation.id] === conversation.archivedAt) continue;
    next[conversation.id] = conversation.archivedAt;
    changed = true;
  }
  return changed ? next : state;
}

function countUnreadArchived(conversations: Conversation[], state: ArchiveReadState): number {
  return conversations.filter((conversation) => state[conversation.id] !== conversation.archivedAt).length;
}

function summaryToConversation(summary: SessionInfo, existing?: Conversation): Conversation {
  return {
    id: existing?.id ?? summary.client_id ?? summary.session_id,
    title: summary.title || existing?.title || "新对话",
    messages: existing?.messages ?? [],
    sessionId: summary.session_id,
    clientId: summary.client_id ?? existing?.clientId ?? existing?.id ?? summary.session_id,
    archivedAt: summary.archived_at ?? undefined,
    deletedAt: summary.deleted_at ?? undefined,
    messagesLoaded: existing?.messagesLoaded ?? false,
  };
}

function transcriptToMessages(transcript: Awaited<ReturnType<typeof getSessionTranscript>>): ChatMessage[] {
  return transcript.map((message, index) => ({
    id: message.id ?? `transcript-${index}`,
    role: message.role,
    content: message.content,
    events: message.events ?? [],
    status: message.status,
    metrics: message.metrics,
    error: message.error,
    running: message.running ? false : undefined,
    runId: message.run_id ?? undefined,
  }));
}

function importableMessages(messages: ChatMessage[]): Array<Pick<ChatMessage, "role" | "content">> {
  return messages
    .filter((message) => message.content.trim() || message.role === "user")
    .map(({ role, content }) => ({ role, content }));
}

export { countUnreadArchived, loadArchiveReadState, loadConversations, markArchivedAsRead };

function AgentApp() {
  const { user, signOut } = useAuth();
  const [page, setPage] = useState<Page>("chat");
  const storageKey = `${STORAGE_KEY}:${user?.id ?? "anonymous"}`;
  const [conversations, setConversations] = useState<Conversation[]>(() => loadConversations(storageKey));
  const [archiveReadState, setArchiveReadState] = useState<ArchiveReadState>(() => loadArchiveReadState(user?.id));
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [modeBySession, setModeBySession] = useState<Record<string, ChatMode>>(() => loadSessionModes(localStorage));
  const [draftMode, setDraftMode] = useState<ChatMode>("agent");
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const activeRunsRef = useRef<Map<string, ActiveRun>>(new Map());

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

  async function runConversation(request: ChatRunRequest): Promise<void> {
    if (activeRunsRef.current.has(request.conversationId)) {
      updateLastMessage(request.conversationId, (item) => ({
        ...item,
        running: false,
        error: "上一运行仍在停止，请稍后再试。",
        decision: undefined,
      }));
      return;
    }
    const controller = new AbortController();
    activeRunsRef.current.set(request.conversationId, { controller, sessionId: request.sessionId });

    const onMessage = (message: StreamMessage) => {
      const active = activeRunsRef.current.get(request.conversationId);
      if (active?.controller !== controller || controller.signal.aborted) return;
      if (message.type === "event") {
        const kind = message.kind ?? "";
        if (kind === "response_delta") {
          const content = (message.data?.content as string | undefined) ?? message.message ?? "";
          if (content) updateLastMessage(request.conversationId, (item) => ({ ...item, content: item.content + content }));
        } else if (kind === "tool_call" || kind === "tool_result" || kind === "tool_failed") {
          updateLastMessage(request.conversationId, (item) => ({
            ...item,
            events: [...item.events, { kind, message: message.message ?? "", data: message.data }],
          }));
        } else if (kind === "decision_requested" && message.data) {
          updateLastMessage(request.conversationId, (item) => ({
            ...item,
            decision: { ...message.data, message: message.message } as ChatMessage["decision"],
          }));
        } else if (kind === "run_finished") {
          updateLastMessage(request.conversationId, (item) => ({ ...item, status: message.message }));
        }
        const runId = message.run_id ?? (typeof message.data?.run_id === "string" ? message.data.run_id : undefined);
        if (runId) updateLastMessage(request.conversationId, (item) => ({ ...item, runId }));
      } else if (message.type === "done") {
        updateLastMessage(request.conversationId, (item) => ({
          ...item,
          content: message.final_answer ?? "",
          status: message.status,
          metrics: message.metrics,
          running: false,
          decision: undefined,
          runId: message.run_id,
        }));
        if (message.session_id && message.session_id !== request.sessionId) {
          void rebindRunSession(request.conversationId, message.session_id);
        }
        void refreshSessions().catch(() => undefined);
      } else if (message.type === "error") {
        updateLastMessage(request.conversationId, (item) => ({
          ...item,
          error: message.error ?? message.message ?? "发生错误",
          running: false,
          decision: undefined,
        }));
      }
    };

    try {
      const result = request.resume
        ? await streamResume(request.sessionId, onMessage, controller.signal, request.permissionMode, request.reasoningEffort)
        : await streamChat(
            request.prompt ?? "",
            onMessage,
            controller.signal,
            {
              sessionId: request.sessionId,
              mode: request.mode,
              permissionMode: request.permissionMode,
              reasoningEffort: request.reasoningEffort,
            },
          );
      if (result === "aborted") {
        updateLastMessage(request.conversationId, (item) => ({
          ...item,
          running: false,
          status: "已停止",
          decision: undefined,
        }));
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        updateLastMessage(request.conversationId, (item) => ({
          ...item,
          error: String((error as Error).message ?? error),
          running: false,
          decision: undefined,
        }));
      }
    } finally {
      const active = activeRunsRef.current.get(request.conversationId);
      if (active?.controller === controller) activeRunsRef.current.delete(request.conversationId);
    }
  }

  function stopConversation(id: string): void {
    const active = activeRunsRef.current.get(id);
    if (!active) return;
    active.controller.abort();
    updateLastMessage(id, (item) => ({ ...item, running: false, status: "已停止", decision: undefined }));
  }

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

  const screens = Grid.useBreakpoint();
  // `useBreakpoint` starts with an empty map while the browser evaluates media
  // queries. Treat that first render as desktop so the sidebar remains usable
  // in tests and during hydration; once `md` is false, the Drawer takes over.
  const isMobile = screens.md === false;

  useEffect(() => {
    if (!isMobile) setMobileSidebarOpen(false);
  }, [isMobile]);

  function navigate(nextPage: Page) {
    setPage(nextPage);
    setMobileSidebarOpen(false);
  }

  function selectConversationAndClose(id: string) {
    selectConversation(id);
    setMobileSidebarOpen(false);
  }

  async function newConversationAndClose(title?: string): Promise<string> {
    const id = await newConversation(title);
    setMobileSidebarOpen(false);
    return id;
  }

  async function useSessionAndClose(sessionId: string): Promise<string> {
    const id = await useSession(sessionId);
    setMobileSidebarOpen(false);
    return id;
  }

  async function signOutAndClose(): Promise<void> {
    setMobileSidebarOpen(false);
    await signOut();
  }

  const sidebar = (
    <AppSidebar
      user={user}
      conversations={activeConversations}
      archivedCount={unreadArchivedCount}
      currentId={current?.id ?? null}
      page={page}
      onNew={newConversationAndClose}
      onSelect={selectConversationAndClose}
      onNavigate={navigate}
      onRename={renameConversation}
      onArchive={archiveConversation}
      onDelete={deleteConversation}
      onSignOut={signOutAndClose}
    />
  );

  return (
    <Layout className="app-shell" style={{ minHeight: "100vh", height: "100vh" }}>
      {!isMobile && (
        <Layout.Sider width={280} theme="light" style={{ background: "#fff" }}>
          {sidebar}
        </Layout.Sider>
      )}
      {isMobile && (
        <Drawer
          title="会话列表"
          placement="left"
          width={280}
          open={mobileSidebarOpen}
          onClose={() => setMobileSidebarOpen(false)}
          styles={{ body: { padding: 0 } }}
        >
          {sidebar}
        </Drawer>
      )}
      <Layout style={{ minWidth: 0, minHeight: 0 }}>
        {isMobile && (
          <div style={{ padding: "8px 12px", background: "#fff", borderBottom: "1px solid #e5e7eb" }}>
            <Button
              type="text"
              icon={<MenuOutlined />}
              onClick={() => setMobileSidebarOpen(true)}
              aria-label="打开会话列表"
            >
              会话列表
            </Button>
          </div>
        )}
        <Layout.Content className="main" style={{ minHeight: 0 }}>
        {actionError && (
          <Alert
            className="global-error"
            type="error"
            showIcon
            message={actionError}
            action={
              <IconAction label="关闭错误" icon={<CloseOutlined />} onClick={() => setActionError(null)} />
            }
          />
        )}
        {page === "chat" ? (
          <ChatPage
            conversation={current}
            mode={current ? modeBySession[current.sessionId ?? current.id] ?? "agent" : draftMode}
            onModeChange={(mode) => setConversationMode(current, mode)}
            onUpdate={updateConversation}
            onNew={newConversationAndClose}
            onNavigate={navigate}
            onEnsureSession={ensureSession}
            onFork={forkConversation}
            onRewind={rewindConversation}
            onSelectSession={useSessionAndClose}
            onReload={reloadConversation}
            onRefresh={refreshSessions}
            running={Boolean(current?.messages.some((message) => message.running))}
            onRun={runConversation}
            onStopRun={stopConversation}
          />
        ) : page === "trash" ? (
          <TrashPage conversations={archivedConversations} onRestore={restoreConversation} onDelete={deleteConversation} />
        ) : (
          <BenchmarkPage />
        )}
        </Layout.Content>
      </Layout>
    </Layout>
  );
}

function LoadingScreen() {
  return (
    <div className="auth-loading" aria-live="polite">
      <Spin size="small" />
      <Typography.Text>正在确认登录状态…</Typography.Text>
    </div>
  );
}

function PublicRoute({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  if (loading) return <LoadingScreen />;
  return user ? <Navigate to="/app" replace /> : <>{children}</>;
}

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  const location = useLocation();
  if (loading) return <LoadingScreen />;
  if (!user) return <Navigate to={`/login?next=${encodeURIComponent(`${location.pathname}${location.search}`)}`} replace />;
  return <>{children}</>;
}

function AppRoutes() {
  return (
    <Routes>
      <Route element={<PublicLayout />}>
        <Route path="/" element={<PublicRoute><HomePage /></PublicRoute>} />
        <Route path="/login" element={<PublicRoute><LoginPage /></PublicRoute>} />
        <Route path="/register" element={<PublicRoute><RegisterPage /></PublicRoute>} />
        <Route path="/forgot-password" element={<PublicRoute><ResetPasswordPage /></PublicRoute>} />
        <Route path="/device/approve" element={<DeviceApprovalPage />} />
      </Route>
      <Route path="/app" element={<ProtectedRoute><AgentApp /></ProtectedRoute>} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

export default function App() {
  return (
    <ConfigProvider locale={zhCN} theme={oceanTheme}>
      <AntApp>
        <BrowserRouter>
          <AuthProvider><AppRoutes /></AuthProvider>
        </BrowserRouter>
      </AntApp>
    </ConfigProvider>
  );
}
