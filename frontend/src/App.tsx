import { useCallback, useEffect, useRef, useState } from "react";
import BenchmarkPage from "./pages/BenchmarkPage";
import ChatPage from "./pages/ChatPage";
import { createSession, getSessionMessages, listSessions, type SessionInfo } from "./api";
import { loadSessionModes, saveSessionModes } from "./sessionModes";
import type { ChatMessage, ChatMode, Conversation, Page } from "./types";

const LEGACY_STORAGE_KEY = "mini-agent-conversations";

function fromSummary(summary: SessionInfo, messages: ChatMessage[] = []): Conversation {
  return {
    id: summary.session_id,
    title: !summary.title || summary.title === "New session" ? "新对话" : summary.title,
    messages,
  };
}

function fromPersistedMessages(messages: Array<{ role: string; content: string }>): ChatMessage[] {
  return messages.map((message) => ({
    id: crypto.randomUUID(),
    role: message.role === "user" ? "user" : "assistant",
    content: message.content,
    events: [],
  }));
}

export default function App() {
  const [page, setPage] = useState<Page>("chat");
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [modeBySession, setModeBySession] = useState<Record<string, ChatMode>>(() => loadSessionModes(localStorage));
  const [draftMode, setDraftMode] = useState<ChatMode>("agent");
  const [loading, setLoading] = useState(true);
  const loadedIds = useRef(new Set<string>());

  const refreshSessions = useCallback(async (): Promise<SessionInfo[]> => {
    const summaries = await listSessions();
    setConversations((current) =>
      summaries.map((summary) => {
        const existing = current.find((conversation) => conversation.id === summary.session_id);
        return fromSummary(summary, existing?.messages ?? []);
      }),
    );
    setCurrentId((current) => current && summaries.some((summary) => summary.session_id === current) ? current : summaries[0]?.session_id ?? null);
    return summaries;
  }, []);

  useEffect(() => {
    // The web UI is now server-session based. Remove only the old browser cache; server data is untouched.
    localStorage.removeItem(LEGACY_STORAGE_KEY);
    void refreshSessions().finally(() => setLoading(false));
  }, [refreshSessions]);

  useEffect(() => {
    saveSessionModes(localStorage, modeBySession);
  }, [modeBySession]);

  const current = conversations.find((conversation) => conversation.id === currentId) ?? null;

  const loadConversation = useCallback(async (id: string, force = false) => {
    if (!force && loadedIds.current.has(id)) return;
    const persisted = await getSessionMessages(id);
    loadedIds.current.add(id);
    setConversations((currentConversations) =>
      currentConversations.map((conversation) =>
        conversation.id === id ? { ...conversation, messages: fromPersistedMessages(persisted) } : conversation,
      ),
    );
  }, []);

  useEffect(() => {
    if (currentId) void loadConversation(currentId);
  }, [currentId, loadConversation]);

  async function newConversation(title?: string): Promise<string> {
    const empty = conversations.find((conversation) => conversation.messages.length === 0);
    if (empty && !title) {
      setCurrentId(empty.id);
      setPage("chat");
      return empty.id;
    }
    const summary = await createSession(title);
    loadedIds.current.add(summary.session_id);
    setModeBySession((currentModes) => ({ ...currentModes, [summary.session_id]: draftMode }));
    setConversations((currentConversations) => [fromSummary(summary), ...currentConversations.filter((item) => item.id !== summary.session_id)]);
    setCurrentId(summary.session_id);
    setPage("chat");
    return summary.session_id;
  }

  function updateConversation(id: string, updater: (conversation: Conversation) => Conversation) {
    setConversations((currentConversations) =>
      currentConversations.map((conversation) => (conversation.id === id ? updater(conversation) : conversation)),
    );
  }

  async function selectConversation(id: string) {
    setCurrentId(id);
    setPage("chat");
    try {
      await loadConversation(id);
    } catch {
      /* ChatPage will surface command/API errors; selection remains safe. */
    }
  }

  async function reloadConversation(id: string) {
    await loadConversation(id, true);
  }

  async function useSession(id: string) {
    await refreshSessions();
    await loadConversation(id, true);
    setCurrentId(id);
    setPage("chat");
  }

  function setMode(id: string, mode: ChatMode) {
    setModeBySession((currentModes) => ({ ...currentModes, [id]: mode }));
  }

  return (
    <div className="app">
      <aside className="sidebar">
        <button className="new-chat-btn" onClick={() => void newConversation()}>
          ＋ 新建对话
        </button>
        <div className="history">
          {loading ? <div className="history-empty">正在加载会话…</div> : null}
          {!loading && conversations.length === 0 ? <div className="history-empty">暂无服务端会话</div> : null}
          {conversations.length > 0 && (
            <>
              <div className="history-label">服务端会话</div>
              {conversations.map((conversation) => (
                <button
                  key={conversation.id}
                  className={"history-item" + (conversation.id === current?.id && page === "chat" ? " active" : "")}
                  onClick={() => void selectConversation(conversation.id)}
                  title={conversation.title}
                >
                  <span className="history-title">{conversation.title || "新对话"}</span>
                </button>
              ))}
            </>
          )}
        </div>
        <div className="sidebar-bottom">
          <button className={"nav-item" + (page === "benchmark" ? " active" : "")} onClick={() => setPage("benchmark")}>
            📊 Benchmark 成绩单
          </button>
          <div className="app-name">Mini-Agent</div>
        </div>
      </aside>
      <main className="main">
        {page === "chat" ? (
          <ChatPage
            conversation={current}
            mode={current ? modeBySession[current.id] ?? "agent" : draftMode}
            onModeChange={(mode) => current ? setMode(current.id, mode) : setDraftMode(mode)}
            onUpdate={updateConversation}
            onNew={newConversation}
            onNavigate={setPage}
            onSelectSession={useSession}
            onReload={reloadConversation}
            onRefresh={refreshSessions}
          />
        ) : (
          <BenchmarkPage />
        )}
      </main>
    </div>
  );
}
