import { useEffect, useMemo, useState } from "react";
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
  type SessionInfo,
} from "./api";
import BenchmarkPage from "./pages/BenchmarkPage";
import ChatPage from "./pages/ChatPage";
import TrashPage from "./pages/TrashPage";
import { loadSessionModes, saveSessionModes } from "./sessionModes";
import type { ChatMessage, ChatMode, Conversation, Page } from "./types";

const STORAGE_KEY = "mini-agent-conversations";

function loadConversations(): Conversation[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
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

export default function App() {
  const [page, setPage] = useState<Page>("chat");
  const [conversations, setConversations] = useState<Conversation[]>(loadConversations);
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [openHistoryId, setOpenHistoryId] = useState<string | null>(null);
  const [modeBySession, setModeBySession] = useState<Record<string, ChatMode>>(() => loadSessionModes(localStorage));
  const [draftMode, setDraftMode] = useState<ChatMode>("agent");

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(conversations));
  }, [conversations]);

  useEffect(() => {
    saveSessionModes(localStorage, modeBySession);
  }, [modeBySession]);

  useEffect(() => {
    let disposed = false;

    async function hydrate() {
      const local = loadConversations();
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

  async function rewindConversation(id: string, messageId: string): Promise<string | undefined> {
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
      return source.messages[index].content;
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
