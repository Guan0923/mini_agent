import { useCallback, useEffect, useRef, useState } from "react";
import { getSessionMessages, listSessions, type SessionInfo } from "./api";
import BenchmarkPage from "./pages/BenchmarkPage";
import ChatPage from "./pages/ChatPage";
import type { ChatMessage, Conversation, Page } from "./types";

const STORAGE_KEY = "mini-agent-conversations";

function loadConversations(): Conversation[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as Conversation[]) : [];
  } catch {
    return [];
  }
}

function toChatMessage(message: { role: string; content: string }, index: number): ChatMessage {
  return {
    id: `srv-${index}`,
    role: message.role === "user" ? "user" : "assistant",
    content: message.content,
    events: [],
  };
}

export default function App() {
  const [page, setPage] = useState<Page>("chat");
  const [conversations, setConversations] = useState<Conversation[]>(loadConversations);
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [serverSessions, setServerSessions] = useState<SessionInfo[]>([]);
  const initialized = useRef(false);

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(conversations));
  }, [conversations]);

  const refreshSessions = useCallback(() => {
    listSessions()
      .then(setServerSessions)
      .catch(() => {
        /* backend offline: keep whatever we had */
      });
  }, []);

  useEffect(() => {
    if (!initialized.current) {
      initialized.current = true;
      refreshSessions();
      if (conversations.length === 0) newConversation();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshSessions]);

  const current = conversations.find((c) => c.id === currentId) ?? conversations[0] ?? null;

  function newConversation() {
    const conv: Conversation = { id: crypto.randomUUID(), title: "新对话", messages: [] };
    setConversations((prev) => [conv, ...prev]);
    setCurrentId(conv.id);
    setPage("chat");
    refreshSessions();
  }

  function updateConversation(id: string, updater: (c: Conversation) => Conversation) {
    setConversations((prev) => prev.map((c) => (c.id === id ? updater(c) : c)));
  }

  function selectConversation(id: string) {
    setCurrentId(id);
    setPage("chat");
  }

  async function openServerSession(session: SessionInfo) {
    try {
      const messages = await getSessionMessages(session.session_id);
      const conv: Conversation = {
        id: `server:${session.session_id}`,
        title: session.title || session.session_id.slice(0, 18),
        messages: messages.map(toChatMessage),
      };
      setConversations((prev) => {
        const without = prev.filter((c) => c.id !== conv.id);
        return [conv, ...without];
      });
      setCurrentId(conv.id);
      setPage("chat");
    } catch {
      /* ignore load errors */
    }
  }

  return (
    <div className="app">
      <aside className="sidebar">
        <button className="new-chat-btn" onClick={newConversation}>
          ＋ 新建对话
        </button>
        <div className="history">
          {serverSessions.length > 0 && (
            <>
              <div className="history-label">历史记录（后端会话）</div>
              {serverSessions.map((s) => (
                <button
                  key={s.session_id}
                  className={
                    "history-item" + (current?.id === `server:${s.session_id}` && page === "chat" ? " active" : "")
                  }
                  onClick={() => void openServerSession(s)}
                  title={s.title || s.session_id}
                >
                  <span className="history-title">{s.title || s.session_id.slice(0, 18)}</span>
                </button>
              ))}
            </>
          )}
          {conversations.length > 0 && (
            <>
              <div className="history-label">当前会话</div>
              {conversations.map((c) => (
                <button
                  key={c.id}
                  className={"history-item" + (c.id === current?.id && page === "chat" ? " active" : "")}
                  onClick={() => selectConversation(c.id)}
                  title={c.title}
                >
                  <span className="history-title">{c.title || "新对话"}</span>
                </button>
              ))}
            </>
          )}
        </div>
        <div className="sidebar-bottom">
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
        {page === "chat" ? (
          <ChatPage
            conversation={current}
            onUpdate={updateConversation}
            onNew={newConversation}
            onNavigate={setPage}
          />
        ) : (
          <BenchmarkPage />
        )}
      </main>
    </div>
  );
}
