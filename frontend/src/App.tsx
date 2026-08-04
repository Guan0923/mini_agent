import { useEffect, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth/AuthProvider";
import BenchmarkPage from "./pages/BenchmarkPage";
import ChatPage from "./pages/ChatPage";
import DeviceApprovalPage from "./pages/auth/DeviceApprovalPage";
import LoginPage from "./pages/auth/LoginPage";
import RegisterPage from "./pages/auth/RegisterPage";
import ResetPasswordPage from "./pages/auth/ResetPasswordPage";
import HomePage from "./pages/HomePage";
import type { ChatMessage, Conversation, Page } from "./types";

const STORAGE_KEY = "mini-agent-conversations";

function loadConversations(key: string): Conversation[] {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as Conversation[]) : [];
  } catch {
    return [];
  }
}

function AgentApp() {
  const { user, signOut } = useAuth();
  const [page, setPage] = useState<Page>("chat");
  const storageKey = `${STORAGE_KEY}:${user?.id ?? "anonymous"}`;
  const [conversations, setConversations] = useState<Conversation[]>(() => loadConversations(storageKey));
  const [currentId, setCurrentId] = useState<string | null>(null);

  useEffect(() => {
    localStorage.setItem(storageKey, JSON.stringify(conversations));
  }, [conversations, storageKey]);

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
          <div className="account-row">
            <span className="account-email" title={user?.email}>{user?.email}</span>
            <button className="logout-button" onClick={() => void signOut()}>退出</button>
          </div>
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
