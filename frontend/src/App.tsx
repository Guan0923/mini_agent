import { useEffect, useState } from "react";
import BenchmarkPage from "./pages/BenchmarkPage";
import ChatPage from "./pages/ChatPage";
import type { ChatMessage, Conversation, Page } from "./types";

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

export { loadConversations };

export default function App() {
  const [page, setPage] = useState<Page>("chat");
  const [conversations, setConversations] = useState<Conversation[]>(loadConversations);
  const [currentId, setCurrentId] = useState<string | null>(null);

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(conversations));
  }, [conversations]);

  const current = conversations.find((c) => c.id === currentId) ?? conversations[0] ?? null;

  function newConversation(): string {
    // 存在未发送过消息的空对话时直接复用，避免产生一堆空对话
    const empty = conversations.find((c) => c.messages.length === 0);
    if (empty) {
      setCurrentId(empty.id);
      setPage("chat");
      return empty.id;
    }
    const conv: Conversation = { id: crypto.randomUUID(), title: "新对话", messages: [] };
    setConversations((prev) => [conv, ...prev]);
    setCurrentId(conv.id);
    setPage("chat");
    return conv.id;
  }

  function updateConversation(id: string, updater: (c: Conversation) => Conversation) {
    setConversations((prev) => prev.map((c) => (c.id === id ? updater(c) : c)));
  }

  function selectConversation(id: string) {
    setCurrentId(id);
    setPage("chat");
  }

  return (
    <div className="app">
      <aside className="sidebar">
        <button className="new-chat-btn" onClick={newConversation}>
          ＋ 新建对话
        </button>
        <div className="history">
          {conversations.length > 0 && (
            <>
              <div className="history-label">对话</div>
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
