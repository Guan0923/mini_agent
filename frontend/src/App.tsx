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
import { loadSessionModes, saveSessionModes } from "./sessionModes";
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

export { loadConversations };

function AgentApp() {
  const { user, signOut } = useAuth();
  const [page, setPage] = useState<Page>("chat");
  const storageKey = `${STORAGE_KEY}:${user?.id ?? "anonymous"}`;
  const [conversations, setConversations] = useState<Conversation[]>(() => loadConversations(storageKey));
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [openHistoryId, setOpenHistoryId] = useState<string | null>(null);
  const [modeBySession, setModeBySession] = useState<Record<string, ChatMode>>(() => loadSessionModes(localStorage));
  const [draftMode, setDraftMode] = useState<ChatMode>("agent");
  const activeRunsRef = useRef<Map<string, ActiveRun>>(new Map());

  useEffect(() => {
    localStorage.setItem(storageKey, JSON.stringify(conversations));
  }, [conversations, storageKey]);

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
    if (!window.confirm("删除后将从界面隐藏，但后台仍保留审计数据。确定继续吗？")) return;
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
    <div className="app">
      <aside className="sidebar">
        <button className="new-chat-btn" onClick={() => void newConversation()}>
          ＋ 新建对话
        </button>
        <div className="history">
          {activeConversations.length > 0 && (
            <>
              <div className="history-label">对话</div>
              {activeConversations.map((conversation) => (
                <div className="history-row" key={conversation.id}>
                  <button
                    className={"history-item" + (conversation.id === current?.id && page === "chat" ? " active" : "")}
                    onClick={() => selectConversation(conversation.id)}
                    title={conversation.title}
                  >
                    {conversation.messages.some((message) => message.running) ? (
                      <span className="history-running" aria-label="正在运行" title="正在运行" />
                    ) : null}
                    <span className="history-title">{conversation.title || "新对话"}</span>
                  </button>
                  <HistoryMenu
                    conversation={conversation}
                    open={openHistoryId === conversation.id}
                    onOpenChange={(open) => setOpenHistoryId(open ? conversation.id : null)}
                    onRename={renameConversation}
                    onArchive={archiveConversation}
                    onDelete={deleteConversation}
                  />
                </div>
              ))}
            </>
          )}
          {activeConversations.length === 0 && <div className="history-empty">暂无对话</div>}
        </div>
        <div className="sidebar-bottom">
          <button className={"nav-item" + (page === "trash" ? " active" : "")} onClick={() => setPage("trash")}>
            🗑 回收站{archivedConversations.length ? ` (${archivedConversations.length})` : ""}
          </button>
          <button
            className={"nav-item" + (page === "benchmark" ? " active" : "")}
            onClick={() => setPage("benchmark")}
          >
            📊 Benchmark 成绩单
          </button>
          <div className="app-name">Mini-Agent</div>
          <div className="account-row">
            <span className="account-email" title={user?.email}>{user?.email}</span>
            <button className="logout-button" onClick={() => void signOut()}>退出</button>
          </div>
        </div>
      </aside>
      <main className="main">
        {actionError && (
          <div className="global-error" role="alert">
            {actionError}
            <button type="button" onClick={() => setActionError(null)} aria-label="关闭错误">
              ×
            </button>
          </div>
        )}
        {page === "chat" ? (
          <ChatPage
            conversation={current}
            mode={current ? modeBySession[current.sessionId ?? current.id] ?? "agent" : draftMode}
            onModeChange={(mode) => setConversationMode(current, mode)}
            onUpdate={updateConversation}
            onNew={newConversation}
            onNavigate={setPage}
            onEnsureSession={ensureSession}
            onFork={forkConversation}
            onRewind={rewindConversation}
            onSelectSession={useSession}
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
      </main>
    </div>
  );
}

interface HistoryMenuProps {
  conversation: Conversation;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onRename: (id: string, title: string) => Promise<void>;
  onArchive: (id: string) => Promise<void>;
  onDelete: (id: string) => Promise<void>;
}

function HistoryMenu({ conversation, open, onOpenChange, onRename, onArchive, onDelete }: HistoryMenuProps) {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(conversation.title);
  const busy = conversation.messages.some((message) => message.running);

  useEffect(() => setTitle(conversation.title), [conversation.title]);

  useEffect(() => {
    if (!open) return undefined;
     function close(event: MouseEvent) {
       if (!(event.target as HTMLElement).closest(".history-actions")) onOpenChange(false);
     }
     function escape(event: KeyboardEvent) {
       if (event.key === "Escape") onOpenChange(false);
     }
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", escape);
    };
   }, [open, onOpenChange]);

  async function saveTitle() {
    const next = title.trim();
    if (!next) return;
    try {
      await onRename(conversation.id, next);
      setEditing(false);
      onOpenChange(false);
    } catch {
      // App renders the mutation error; keep the editor open for correction.
    }
  }

  return (
    <div className="history-actions">
      <button
        type="button"
        className="history-more"
        aria-label={`更多操作：${conversation.title}`}
        aria-expanded={open}
         onClick={() => onOpenChange(!open)}
      >
        …
      </button>
      {open && (
        <div className="history-card" role="menu">
          {editing ? (
            <div className="rename-editor">
              <input value={title} onChange={(event) => setTitle(event.target.value)} autoFocus aria-label="新标题" />
              <div>
                <button type="button" onClick={() => void saveTitle()} disabled={!title.trim()}>
                  保存
                </button>
                <button type="button" onClick={() => setEditing(false)}>
                  取消
                </button>
              </div>
            </div>
          ) : (
            <>
              <button type="button" role="menuitem" onClick={() => setEditing(true)}>
                重命名
              </button>
               <button
                 type="button"
                 role="menuitem"
                 disabled={busy}
                 onClick={() => {
                   onOpenChange(false);
                   void onArchive(conversation.id);
                 }}
               >
                归档
              </button>
              <button
                type="button"
                role="menuitem"
                className="danger-text"
                disabled={busy}
                 onClick={() => {
                   onOpenChange(false);
                   void onDelete(conversation.id);
                 }}
              >
                删除
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function LoadingScreen() {
  return <div className="auth-loading" aria-live="polite"><span className="loading-orb" />正在确认登录状态…</div>;
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
      <Route path="/" element={<PublicRoute><HomePage /></PublicRoute>} />
      <Route path="/login" element={<PublicRoute><LoginPage /></PublicRoute>} />
      <Route path="/register" element={<PublicRoute><RegisterPage /></PublicRoute>} />
      <Route path="/forgot-password" element={<PublicRoute><ResetPasswordPage /></PublicRoute>} />
      <Route path="/device/approve" element={<DeviceApprovalPage />} />
      <Route path="/app" element={<ProtectedRoute><AgentApp /></ProtectedRoute>} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

export default function App() {
  return <BrowserRouter><AuthProvider><AppRoutes /></AuthProvider></BrowserRouter>;
}
