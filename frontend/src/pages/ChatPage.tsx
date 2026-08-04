import { useEffect, useRef, useState } from "react";
import { marked } from "marked";
import { listSessions, listSkills, listTools, streamChat } from "../api";
import type { ChatMessage, Conversation, Metrics, Page, ToolEvent } from "../types";

interface Props {
  conversation: Conversation | null;
  onUpdate: (id: string, updater: (c: Conversation) => Conversation) => void;
  onNew: () => string;
  onNavigate: (page: Page) => void;
}

interface Command {
  name: string;
  label: string;
  description: string;
}

const COMMANDS: Command[] = [
  { name: "/help", label: "帮助", description: "查看使用说明" },
  { name: "/tools", label: "工具", description: "列出 agent 可用的工具" },
  { name: "/skills", label: "技能", description: "列出已发现的技能" },
  { name: "/sessions", label: "会话", description: "列出后端的会话记录" },
  { name: "/time", label: "时间", description: "显示当前时间" },
  { name: "/clear", label: "清空对话", description: "清空当前会话的消息" },
  { name: "/new", label: "新建对话", description: "开启一个新的对话" },
  { name: "/benchmark", label: "成绩单", description: "打开 Benchmark 成绩单页" },
];

const HELP_TEXT = [
  "# 使用说明",
  "",
  "向 Mini-Agent 输入任务，它会自动调用工具（读文件、跑命令、Web 搜索、MCP 等）来完成任务。",
  "",
  "**斜杠命令：**",
  "- `/tools` 列出 agent 可用的工具",
  "- `/skills` 列出已发现的技能",
  "- `/sessions` 列出后端的会话记录",
  "- `/time` 显示当前时间",
  "- `/clear` 清空当前对话",
  "- `/new` 新建对话",
  "- `/benchmark` 打开 Benchmark 成绩单页",
  "- `/help` 显示本说明",
  "",
  "发送方式：`Enter` 发送，`Shift+Enter` 换行。",
].join("\n");

function deriveTitle(prompt: string): string {
  const text = prompt.trim();
  return text.length > 18 ? text.slice(0, 18) + "…" : text;
}

function Markdown({ text }: { text: string }) {
  const html = marked.parse(text || "", { async: false }) as string;
  return <div className="markdown" dangerouslySetInnerHTML={{ __html: html }} />;
}

function ToolLine({ ev }: { ev: ToolEvent }) {
  if (ev.kind === "tool_call") {
    const args = ev.data?.arguments;
    const shown = typeof args === "string" ? args : JSON.stringify(args ?? "");
    return (
      <div className="tool-line">
        <span className="tool-icon">🔧</span>
        <b>{ev.message}</b> <span className="mono">{shown}</span>
      </div>
    );
  }
  if (ev.kind === "tool_failed") {
    return <div className="tool-line failed">✖ {ev.message}</div>;
  }
  if (ev.kind === "tool_result") {
    const result = (ev.data?.result as string | undefined) ?? ev.message;
    return (
      <details className="tool-result">
        <summary>📄 {ev.data?.tool ? String(ev.data.tool) : "工具"} 结果</summary>
        <pre>{result}</pre>
      </details>
    );
  }
  return null;
}

function AssistantMessage({ msg }: { msg: ChatMessage }) {
  return (
    <div className="message assistant">
      <div className="avatar">A</div>
      <div className="bubble">
        {msg.events.length > 0 && (
          <div className="event-list">
            {msg.events.map((ev, i) => (
              <ToolLine key={i} ev={ev} />
            ))}
          </div>
        )}
        {msg.error ? (
          <div className="error-text">⚠️ {msg.error}</div>
        ) : msg.content ? (
          <Markdown text={msg.content} />
        ) : msg.running ? (
          <div className="thinking" role="status" aria-label="思考中">
            <span className="dot" />
            <span className="dot" />
            <span className="dot" />
          </div>
        ) : null}
        {(msg.status || (msg.metrics && msg.metrics.duration_ms != null)) && (
          <div className="meta">
            {msg.status ?? ""}
            {msg.status && msg.metrics && msg.metrics.duration_ms != null ? " · " : ""}
            {msg.metrics && msg.metrics.duration_ms != null
              ? `${(msg.metrics.duration_ms / 1000).toFixed(1)}s · ${msg.metrics.model_calls ?? 0} 次模型调用 · ${
                  msg.metrics.tool_calls ?? 0
                } 次工具调用`
              : null}
          </div>
        )}
      </div>
    </div>
  );
}

export default function ChatPage({ conversation, onUpdate, onNew, onNavigate }: Props) {
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const activeRef = useRef<{
    conversationId: string;
    messageId: string;
    controller: AbortController;
    cancelled: boolean;
  } | null>(null);
  const previousConversationIdRef = useRef<string | null>(conversation?.id ?? null);
  const taRef = useRef<HTMLTextAreaElement>(null);

  const messages = conversation?.messages ?? [];

  const commandMenuVisible = input.startsWith("/") && !busy;
  const filteredCommands =
    commandMenuVisible && input.length > 1
      ? COMMANDS.filter((c) => c.name.startsWith(input.toLowerCase()))
      : commandMenuVisible
        ? COMMANDS
        : [];

  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 200) + "px";
  }, [input]);

  function updateMessage(
    conversationId: string,
    messageId: string,
    fn: (message: ChatMessage) => ChatMessage,
  ) {
    onUpdate(conversationId, (current) => {
      const index = current.messages.findIndex((message) => message.id === messageId);
      if (index < 0 || current.messages[index].role !== "assistant") return current;
      const messages = [...current.messages];
      messages[index] = fn(messages[index]);
      return { ...current, messages };
    });
  }

  function stopActive(status = "已停止") {
    const active = activeRef.current;
    if (!active) return;
    active.cancelled = true;
    updateMessage(active.conversationId, active.messageId, (message) => ({
      ...message,
      running: false,
      status,
    }));
    active.controller.abort();
  }

  useEffect(() => {
    const previousId = previousConversationIdRef.current;
    const currentId = conversation?.id ?? null;
    if (previousId !== null && previousId !== currentId) stopActive();
    previousConversationIdRef.current = currentId;
  }, [conversation?.id]);

  useEffect(() => () => stopActive(), []);

  // 没有会话时先创建一个（会复用未发送过消息的空对话），返回会话 id
  function ensureConv(): string {
    if (conversation) return conversation.id;
    return onNew();
  }

  async function applyCommand(cmd: Command) {
    setInput("");
    const insert = (content: string) => {
      const msg: ChatMessage = { id: crypto.randomUUID(), role: "assistant", content, events: [] };
      const id = ensureConv();
      onUpdate(id, (c) => ({ ...c, messages: [...c.messages, msg] }));
    };
    if (cmd.name === "/help") {
      insert(HELP_TEXT);
    } else if (cmd.name === "/clear") {
      if (conversation) {
        onUpdate(conversation.id, (c) => ({ ...c, messages: [] }));
      }
    } else if (cmd.name === "/new") {
      onNew();
    } else if (cmd.name === "/benchmark") {
      onNavigate("benchmark");
    } else if (cmd.name === "/time") {
      insert(`当前时间：**${new Date().toLocaleString()}**`);
    } else if (cmd.name === "/tools") {
      try {
        const tools = await listTools();
        const lines = tools
          .map((t) => `- \`${t.name}\` — ${t.description}`)
          .join("\n");
        insert(`# 可用工具（${tools.length} 个）\n\n${lines || "（无）"}`);
      } catch (err) {
        insert(`⚠️ 获取工具列表失败：${String((err as Error).message ?? err)}`);
      }
    } else if (cmd.name === "/skills") {
      try {
        const skills = await listSkills();
        const lines = skills.map((s) => `- \`${s.name}\` — ${s.description}`).join("\n");
        insert(
          `# 已发现技能（${skills.length} 个）\n\n${lines || "（无）\n\n可在工作区 \`webapp-data/chat-workspace/.mini_agent/skills/\` 下添加 SKILL.md"}`,
        );
      } catch (err) {
        insert(`⚠️ 获取技能列表失败：${String((err as Error).message ?? err)}`);
      }
    } else if (cmd.name === "/sessions") {
      try {
        const sessions = await listSessions();
        const lines = sessions
          .map(
            (s) =>
              `- \`${s.session_id.slice(0, 20)}…\` — ${s.title || "（无标题）"} · ${s.message_count} 条消息 · ${s.last_run_status ?? "?"}`,
          )
          .join("\n");
        insert(`# 后端会话（${sessions.length} 个）\n\n${lines || "（暂无）"}`);
      } catch (err) {
        insert(`⚠️ 获取会话列表失败：${String((err as Error).message ?? err)}`);
      }
    }
  }

  async function send() {
    const prompt = input.trim();
    if (!prompt || busy) return;
    setInput("");
    setBusy(true);
    // 没有会话时先创建（复用空对话），发消息才算真正开始一个对话
    const convId = ensureConv();
    const userMsg: ChatMessage = { id: crypto.randomUUID(), role: "user", content: prompt, events: [] };
    const assistantMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: "assistant",
      content: "",
      events: [],
      running: true,
    };
    onUpdate(convId, (c) => ({
      ...c,
      title: deriveTitle(prompt),
      messages: [...c.messages, userMsg, assistantMsg],
    }));

    const active = {
      conversationId: convId,
      messageId: assistantMsg.id,
      controller: new AbortController(),
      cancelled: false,
    };
    activeRef.current = active;

    try {
      const result = await streamChat(
        prompt,
        (m) => {
          if (active.cancelled) return;
          if (m.type === "event") {
            const kind = m.kind ?? "";
            if (kind === "response_delta") {
              const content = (m.data?.content as string | undefined) ?? m.message ?? "";
              if (content) {
                updateMessage(active.conversationId, active.messageId, (message) => ({
                  ...message,
                  content: message.content + content,
                }));
              }
            } else if (kind === "tool_call" || kind === "tool_result" || kind === "tool_failed") {
              updateMessage(active.conversationId, active.messageId, (message) => ({
                ...message,
                events: [...message.events, { kind, message: m.message ?? "", data: m.data }],
              }));
            } else if (kind === "run_finished" || kind === "cancelled") {
              updateMessage(active.conversationId, active.messageId, (message) => ({
                ...message,
                status: m.message,
              }));
            }
          } else if (m.type === "done") {
            updateMessage(active.conversationId, active.messageId, (message) => ({
              ...message,
              content: m.final_answer ?? "",
              status: m.status,
              metrics: m.metrics,
              running: false,
            }));
          } else if (m.type === "error") {
            updateMessage(active.conversationId, active.messageId, (message) => ({
              ...message,
              error: m.error ?? m.message ?? "发生错误",
              running: false,
            }));
          }
        },
        active.controller.signal,
      );
      if (result === "aborted") {
        updateMessage(active.conversationId, active.messageId, (message) => ({
          ...message,
          running: false,
          status: message.status ?? "已停止",
        }));
      }
    } catch (error) {
      if (!active.cancelled) {
        updateMessage(active.conversationId, active.messageId, (message) => ({
          ...message,
          error: String((error as Error).message ?? error),
          running: false,
        }));
      }
    } finally {
      if (activeRef.current === active) {
        setBusy(false);
        activeRef.current = null;
      }
    }
  }

  function stop() {
    stopActive();
  }

  return (
    <div className="chat-page">
      <div className="chat-scroll">
        {messages.length === 0 ? (
          <div className="welcome">
            <div className="logo">Mini-Agent</div>
            <p className="welcome-sub">向你的智能体提问，它会调用文件、Shell、Web 等工具完成任务</p>
          </div>
        ) : (
          messages.map((msg) =>
            msg.role === "user" ? (
              <div className="message user" key={msg.id}>
                <div className="bubble">{msg.content}</div>
              </div>
            ) : (
              <AssistantMessage key={msg.id} msg={msg} />
            ),
          )
        )}
      </div>
      <div className="composer">
        {commandMenuVisible && (
          <div className="command-menu">
            {filteredCommands.length === 0 ? (
              <div className="command-menu-empty">没有匹配的命令</div>
            ) : (
              filteredCommands.map((cmd) => (
                <button key={cmd.name} className="command-item" onClick={() => applyCommand(cmd)}>
                  <span className="command-name">{cmd.name}</span>
                  <span className="command-desc">{cmd.label} · {cmd.description}</span>
                </button>
              ))
            )}
          </div>
        )}
        <div className="composer-box">
          <textarea
            ref={taRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void send();
              }
            }}
            placeholder="输入任务，按 Enter 发送"
            rows={1}
          />
          {busy ? (
            <button className="send-btn stop" onClick={stop}>
              停止
            </button>
          ) : (
            <button className="send-btn" onClick={() => void send()} disabled={!input.trim()}>
              发送
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
