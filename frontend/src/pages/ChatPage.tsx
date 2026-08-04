import { useEffect, useRef, useState, type MouseEvent as ReactMouseEvent } from "react";
import {
  compactSession,
  forkRun,
  getTimezone,
  getTrace,
  listForkableRuns,
  listSessions,
  listSkills,
  listTools,
  setTimezone,
  streamChat,
  streamResume,
  submitDecision,
  type ForkableRun,
} from "../api";
import { DISPLAY_LEVELS, HELP_TEXT, parseCommand } from "../commands";
import { commandKeyAction, commandSuggestions, completionText, nextCommandIndex } from "../commandCompletion";
import DecisionCard from "../components/DecisionCard";
import MarkdownContent from "../components/MarkdownContent";
import type {
  ChatMessage,
  ChatMode,
  Conversation,
  DecisionRequest,
  DisplayMode,
  Metrics,
  Page,
  PermissionMode,
  ReasoningEffort,
  StreamMessage,
  ToolEvent,
} from "../types";

interface Props {
  conversation: Conversation | null;
  mode?: ChatMode;
  onModeChange?: (mode: ChatMode) => void;
  onUpdate: (id: string, updater: (conversation: Conversation) => Conversation) => void;
  onNew: (title?: string) => Promise<string> | string;
  onNavigate: (page: Page) => void;
  onEnsureSession?: (id: string) => Promise<string>;
  onFork?: (conversationId: string, messageId: string) => Promise<void>;
  onRewind?: (conversationId: string, messageId: string) => Promise<RewindResult | string | undefined>;
  onSelectSession?: (id: string) => Promise<string>;
  onReload?: (id: string) => Promise<void>;
  onRefresh?: () => Promise<void>;
  running?: boolean;
  onRun?: (request: ChatRunRequest) => Promise<void>;
  onStopRun?: (conversationId: string) => void;
}

interface RewindResult {
  content: string;
  sessionId: string;
}

interface ChatRunRequest {
  conversationId: string;
  sessionId: string;
  prompt: string | null;
  resume: boolean;
  mode: ChatMode;
  permissionMode: PermissionMode;
  reasoningEffort: ReasoningEffort;
}

const REASONING_LABELS: Record<ReasoningEffort, string> = {
  low: "低",
  medium: "中",
  high: "高",
  xhigh: "超高",
  max: "最大",
};

async function copyText(value: string): Promise<void> {
  const clipboard = typeof window !== "undefined" ? window.navigator.clipboard : navigator.clipboard;
  if (clipboard?.writeText) {
    await clipboard.writeText(value);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) throw new Error("浏览器拒绝了复制操作");
}

function MessageActions({
  msg,
  busy,
  onFork,
  onRewind,
  onEdit,
}: {
  msg: ChatMessage;
  busy: boolean;
  onFork?: () => void;
  onRewind?: () => void;
  onEdit?: () => void;
}) {
  const [feedback, setFeedback] = useState<string | null>(null);

  async function copy() {
    if (!msg.content) return;
    try {
      await copyText(msg.content);
      setFeedback("已复制");
      window.setTimeout(() => setFeedback(null), 1400);
    } catch {
      setFeedback("复制失败");
      window.setTimeout(() => setFeedback(null), 1800);
    }
  }

  return (
    <div className="message-actions" aria-label={`${msg.role === "user" ? "用户" : "Agent"}消息操作`}>
      <button type="button" onClick={() => void copy()} disabled={!msg.content} aria-label="复制">
        {feedback ?? "复制"}
      </button>
      {onRewind ? <button type="button" onClick={onRewind} disabled={busy} aria-label="回溯">回溯</button> : null}
      {onEdit ? <button type="button" onClick={onEdit} disabled={busy || !msg.content} aria-label="编辑">编辑</button> : null}
      {onFork ? <button type="button" onClick={onFork} disabled={busy || msg.running || !msg.content} aria-label="Fork">Fork</button> : null}
    </div>
  );
}

function ToolLine({ ev, display }: { ev: ToolEvent; display: DisplayMode }) {
  if (display === "minimal") return null;
  if (ev.kind === "tool_call") {
    const args = ev.data?.arguments;
    const shown = typeof args === "string" ? args : JSON.stringify(args ?? "");
    return (
      <div className="tool-line">
        <span>🔧</span>
        <b>{ev.message}</b>
        {display === "verbose" ? <span className="mono">{shown}</span> : null}
      </div>
    );
  }
  if (ev.kind === "tool_failed") return <div className="tool-line failed">✖ {ev.message}</div>;
  if (ev.kind === "tool_result") {
    const result = (ev.data?.result as string | undefined) ?? ev.message;
    return (
      <details className="tool-result" open={display === "verbose"}>
        <summary>📄 {ev.data?.tool ? String(ev.data.tool) : "工具"} 结果</summary>
        {display === "verbose" ? <pre>{result}</pre> : null}
      </details>
    );
  }
  return null;
}

function AssistantMessage({
  msg,
  display,
  onDecision,
  busy,
  onFork,
}: {
  msg: ChatMessage;
  display: DisplayMode;
  onDecision: (request: DecisionRequest, choice: string, options?: { supplement?: string; answers?: Record<string, string[]> }) => Promise<void>;
  busy: boolean;
  onFork?: () => void;
}) {
  return (
    <div className="message assistant">
      <div className="avatar">A</div>
      <div className="bubble">
        {msg.events.length > 0 && display !== "minimal" ? (
          <div className="event-list">
            {msg.events.map((ev, index) => <ToolLine key={index} ev={ev} display={display} />)}
          </div>
        ) : null}
        {msg.decision ? (
          <DecisionCard request={msg.decision} onSubmit={(choice, options) => onDecision(msg.decision!, choice, options)} />
        ) : null}
        {msg.error ? <div className="error-text">⚠️ {msg.error}</div> : msg.content ? <MarkdownContent text={msg.content} /> : msg.running && !msg.decision ? (
          <div className="thinking" role="status" aria-label="思考中" data-state="thinking" aria-live="polite"><span className="dot" /><span className="dot" /><span className="dot" /></div>
        ) : null}
        {msg.status || (msg.metrics && msg.metrics.duration_ms != null) ? (
          <div className="meta">
            {msg.status ?? ""}
            {msg.status && msg.metrics && msg.metrics.duration_ms != null ? " · " : ""}
            {msg.metrics && msg.metrics.duration_ms != null ? `${(msg.metrics.duration_ms / 1000).toFixed(1)}s · ${msg.metrics.model_calls ?? 0} 次模型调用 · ${msg.metrics.tool_calls ?? 0} 次工具调用` : null}
          </div>
        ) : null}
        <MessageActions msg={msg} busy={busy} onFork={onFork} />
      </div>
    </div>
  );
}

export default function ChatPage({
  conversation,
  mode: selectedMode,
  onModeChange = () => undefined,
  onUpdate,
  onNew,
  onNavigate,
  onEnsureSession = async (id) => conversation?.sessionId ?? id,
  onFork,
  onRewind,
  onSelectSession = async (id) => id,
  onReload = async () => undefined,
  onRefresh = async () => undefined,
  running: runningProp,
  onRun,
  onStopRun,
}: Props) {
  const mode = selectedMode ?? "agent";
  const enhancedChatOptions = selectedMode !== undefined;
  const [input, setInput] = useState("");
  const [localBusy, setLocalBusy] = useState(false);
  const [modeMenu, setModeMenu] = useState(false);
  const [permissionMode, setPermissionMode] = useState<PermissionMode>("approval_for_me");
  const [permissionMenu, setPermissionMenu] = useState(false);
  const [display, setDisplay] = useState<DisplayMode>("medium");
  const [displayMenu, setDisplayMenu] = useState(false);
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort>("medium");
  const [reasoningMenu, setReasoningMenu] = useState(false);
  const [timezoneOptions, setTimezoneOptions] = useState<Array<{ identifier: string; label: string }>>([]);
  const [timezoneMenu, setTimezoneMenu] = useState(false);
  const [forkOptions, setForkOptions] = useState<ForkableRun[]>([]);
  const [forkMenu, setForkMenu] = useState(false);
  const [activeCommandIndex, setActiveCommandIndex] = useState(0);
  const [commandMenuDismissedFor, setCommandMenuDismissedFor] = useState<string | null>(null);
  const [editingMessageId, setEditingMessageId] = useState<string | null>(null);
  const [editingDraft, setEditingDraft] = useState("");
  const abortRef = useRef<AbortController | null>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const editRef = useRef<HTMLTextAreaElement>(null);
  const pendingCaretRef = useRef<number | null>(null);

  const messages = conversation?.messages ?? [];
  const busy = runningProp ?? localBusy;
  const filteredCommands = commandSuggestions(input);
  const commandMenuVisible = !busy && commandMenuDismissedFor !== input && filteredCommands.length > 0;

  useEffect(() => {
    const textarea = taRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = Math.min(textarea.scrollHeight, 200) + "px";
    if (pendingCaretRef.current !== null) {
      const caret = pendingCaretRef.current;
      textarea.setSelectionRange(caret, caret);
      pendingCaretRef.current = null;
    }
  }, [input]);

  useEffect(() => () => abortRef.current?.abort(), []);

  useEffect(() => {
    if (editingMessageId) {
      editRef.current?.focus();
      editRef.current?.select();
    }
  }, [editingMessageId]);

  useEffect(() => {
    const textarea = editRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 240)}px`;
  }, [editingDraft, editingMessageId]);

  useEffect(() => {
    if (!modeMenu && !permissionMenu && !displayMenu && !reasoningMenu && !timezoneMenu && !forkMenu) return undefined;
    const closeOnOutsideClick = (event: globalThis.MouseEvent) => {
      if (!(event.target as HTMLElement).closest(".composer")) {
        setModeMenu(false);
        setPermissionMenu(false);
        setDisplayMenu(false);
        setReasoningMenu(false);
        setTimezoneMenu(false);
        setForkMenu(false);
      }
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setModeMenu(false);
      setPermissionMenu(false);
      setDisplayMenu(false);
      setReasoningMenu(false);
      setTimezoneMenu(false);
      setForkMenu(false);
    };
    document.addEventListener("mousedown", closeOnOutsideClick);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("mousedown", closeOnOutsideClick);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [displayMenu, forkMenu, modeMenu, permissionMenu, reasoningMenu, timezoneMenu]);

  function changeInput(value: string) {
    setInput(value);
    setCommandMenuDismissedFor(null);
    setActiveCommandIndex(0);
  }

  function completeCommand(index = activeCommandIndex) {
    const command = filteredCommands[index];
    if (!command) return;
    const value = completionText(command);
    setInput(value);
    setCommandMenuDismissedFor(value);
    setActiveCommandIndex(0);
    pendingCaretRef.current = value.length;
  }

  function updateLast(updater: (message: ChatMessage) => ChatMessage, conversationId = conversation?.id) {
    if (!conversationId) return;
    onUpdate(conversationId, (current) => {
      const currentMessages = [...current.messages];
      const index = currentMessages.length - 1;
      if (index < 0 || currentMessages[index].role !== "assistant") return current;
      currentMessages[index] = updater(currentMessages[index]);
      return { ...current, messages: currentMessages };
    });
  }

  function appendDelta(content: string, conversationId?: string) {
    updateLast((message) => ({ ...message, content: message.content + content }), conversationId);
  }

  function appendEvent(event: ToolEvent, conversationId?: string) {
    updateLast((message) => ({ ...message, events: [...message.events, event] }), conversationId);
  }

  function setLast(fields: Partial<ChatMessage>, conversationId?: string) {
    updateLast((message) => ({ ...message, ...fields }), conversationId);
  }

  async function ensureSession(): Promise<{ conversationId: string; sessionId: string }> {
    if (!conversation) {
      const id = await onNew();
      return { conversationId: id, sessionId: id };
    }
    const conversationId = conversation.id;
    const sessionId = await onEnsureSession(conversationId);
    return { conversationId, sessionId };
  }

  async function insert(content: string) {
    const { conversationId } = await ensureSession();
    const message: ChatMessage = { id: crypto.randomUUID(), role: "assistant", content, events: [] };
    onUpdate(conversationId, (current) => ({ ...current, messages: [...current.messages, message] }));
  }

  async function runStream(conversationId: string, sessionId: string, prompt: string | null, resume = false) {
    const controller = new AbortController();
    abortRef.current = controller;
    setLocalBusy(true);
    try {
      const onMessage = (message: StreamMessage) => {
        if (message.type === "event") {
          const kind = message.kind ?? "";
          if (kind === "response_delta") {
            const content = (message.data?.content as string | undefined) ?? message.message ?? "";
            if (content) appendDelta(content, conversationId);
          } else if (kind === "tool_call" || kind === "tool_result" || kind === "tool_failed") {
            appendEvent({ kind, message: message.message ?? "", data: message.data }, conversationId);
          } else if (kind === "decision_requested" && message.data) {
            setLast({ decision: { ...message.data, message: message.message } as DecisionRequest }, conversationId);
          } else if (kind === "run_finished") {
            setLast({ status: message.message }, conversationId);
          }
          const runId = message.run_id ?? (typeof message.data?.run_id === "string" ? message.data.run_id : undefined);
          if (runId) setLast({ runId }, conversationId);
        } else if (message.type === "done") {
          setLast({
            content: message.final_answer ?? "",
            status: message.status,
            metrics: message.metrics,
            running: false,
            decision: undefined,
            runId: message.run_id,
          }, conversationId);
          if (message.mode) onModeChange(message.mode);
          if (message.session_id && message.session_id !== sessionId) {
            void onSelectSession(message.session_id);
          }
          void onRefresh();
        } else if (message.type === "error") {
          setLast({ error: message.error ?? message.message ?? "发生错误", running: false, decision: undefined }, conversationId);
        }
      };
      if (resume) {
        const result = await streamResume(sessionId, onMessage, controller.signal, permissionMode, reasoningEffort);
        if (result === "aborted") setLast({ running: false, status: "已停止", decision: undefined }, conversationId);
      } else {
        const result = await streamChat(
          prompt ?? "",
          onMessage,
          controller.signal,
          enhancedChatOptions ? { sessionId, mode, permissionMode, reasoningEffort } : sessionId,
        );
        if (result === "aborted") setLast({ running: false, status: "已停止", decision: undefined }, conversationId);
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        setLast({ error: String((error as Error).message ?? error), running: false, decision: undefined }, conversationId);
      }
    } finally {
      setLocalBusy(false);
      abortRef.current = null;
    }
  }

  async function dispatchRun(conversationId: string, sessionId: string, prompt: string | null, resume = false) {
    if (onRun) {
      await onRun({
        conversationId,
        sessionId,
        prompt,
        resume,
        mode,
        permissionMode,
        reasoningEffort,
      });
      return;
    }
    await runStream(conversationId, sessionId, prompt, resume);
  }

  async function runPrompt(prompt: string, target?: { conversationId: string; sessionId: string }) {
    const { conversationId, sessionId } = target ?? await ensureSession();
    const userMessage: ChatMessage = { id: crypto.randomUUID(), role: "user", content: prompt, events: [] };
    const assistantMessage: ChatMessage = { id: crypto.randomUUID(), role: "assistant", content: "", events: [], running: true };
    onUpdate(conversationId, (current) => ({
      ...current,
      title: current.title === "新对话" ? prompt.slice(0, 18) + (prompt.length > 18 ? "…" : "") : current.title,
      messages: [...current.messages, userMessage, assistantMessage],
    }));
    await dispatchRun(conversationId, sessionId, prompt);
  }

  async function resumeSession(sessionId?: string) {
    let targetSessionId = sessionId ?? conversation?.sessionId;
    if (!targetSessionId) {
      const sessions = await listSessions();
      if (!sessions[0]) return insert("没有可恢复的服务端会话。");
      return resumeSession(sessions[0].session_id);
    }
    let conversationId = conversation?.id;
    if (!conversationId || conversation?.sessionId !== targetSessionId) {
      conversationId = await onSelectSession(targetSessionId);
    } else {
      targetSessionId = await onEnsureSession(conversationId);
    }
    const assistant: ChatMessage = { id: crypto.randomUUID(), role: "assistant", content: "", events: [], running: true };
    onUpdate(conversationId, (current) => ({ ...current, messages: [...current.messages, assistant] }));
    await dispatchRun(conversationId, targetSessionId, null, true);
  }

  async function executeCommand(name: string, argument: string) {
    setInput("");
    setCommandMenuDismissedFor(null);
    setActiveCommandIndex(0);
    setModeMenu(false);
    setPermissionMenu(false);
    setDisplayMenu(false);
    setTimezoneMenu(false);
    setForkMenu(false);
    if (name === "/agent" || name === "/plan") {
      onModeChange(name.slice(1) as ChatMode);
      return;
    }
    if (name === "/help") return insert(HELP_TEXT);
    if (name === "/benchmark") return onNavigate("benchmark");
    if (name === "/new" || name === "/clear") return onNew(argument || undefined);
    if (name === "/permission") return setPermissionMenu(true);
    if (name === "/display") {
      if (argument && DISPLAY_LEVELS.includes(argument as DisplayMode)) return setDisplay(argument as DisplayMode);
      return setDisplayMenu(true);
    }
    if (name === "/time") {
      const { sessionId } = await ensureSession();
      if (argument) {
        try {
          await setTimezone(sessionId, argument);
          await insert(`当前会话时区已设置为 **${argument}**。`);
        } catch (error) {
          await insert(`⚠️ 设置时区失败：${String((error as Error).message ?? error)}`);
        }
        return;
      }
      try {
        const info = await getTimezone(sessionId);
        setTimezoneOptions(info.options);
        setTimezoneMenu(true);
      } catch (error) {
        await insert(`⚠️ 获取时区失败：${String((error as Error).message ?? error)}`);
      }
      return;
    }
    if (name === "/sessions") {
      try {
        const sessions = await listSessions();
        const lines = sessions.map((session) => `- \`${session.session_id.slice(0, 20)}…\` — ${session.title || "（无标题）"} · ${session.message_count} 条消息 · ${session.last_run_status ?? "?"}`).join("\n");
        await insert(`# 后端会话（${sessions.length} 个）\n\n${lines || "（暂无）"}`);
      } catch (error) {
        await insert(`⚠️ 获取会话列表失败：${String((error as Error).message ?? error)}`);
      }
      return;
    }
    if (name === "/history") {
      if (conversation) await onReload(conversation.id);
      return;
    }
    if (name === "/resume") return resumeSession(argument || undefined);
    if (name === "/fork") {
      if (argument) {
        try {
          const session = await forkRun(argument);
          await onSelectSession(session.session_id);
          await onRefresh();
        } catch (error) {
          await insert(`⚠️ 分叉失败：${String((error as Error).message ?? error)}`);
        }
      } else {
        try {
          setForkOptions(await listForkableRuns());
          setForkMenu(true);
        } catch (error) {
          await insert(`⚠️ 获取可分叉运行失败：${String((error as Error).message ?? error)}`);
        }
      }
      return;
    }
    if (name === "/tools") {
      try {
        const tools = await listTools();
        await insert(`# 可用工具（${tools.length} 个）\n\n${tools.map((tool) => `- \`${tool.name}\` — ${tool.description}`).join("\n") || "（无）"}`);
      } catch (error) {
        await insert(`⚠️ 获取工具列表失败：${String((error as Error).message ?? error)}`);
      }
      return;
    }
    if (name === "/skills") {
      try {
        const skills = await listSkills();
        await insert(`# 已发现技能（${skills.length} 个）\n\n${skills.map((skill) => `- \`${skill.name}\` — ${skill.description}`).join("\n") || "（无）"}`);
      } catch (error) {
        await insert(`⚠️ 获取技能列表失败：${String((error as Error).message ?? error)}`);
      }
      return;
    }
    if (name === "/compact") {
      if (!conversation) return;
      try {
        const { sessionId } = await ensureSession();
        const result = await compactSession(sessionId);
        await insert(result.compacted ? `上下文已压缩：${result.previous_messages} → ${result.remaining_messages} 条消息。` : "没有可压缩的旧上下文。");
        await onReload(conversation.id);
      } catch (error) {
        await insert(`⚠️ 压缩失败：${String((error as Error).message ?? error)}`);
      }
      return;
    }
    if (name === "/trace") {
      if (!conversation) return insert("还没有当前会话运行记录。");
      try {
        const { sessionId } = await ensureSession();
        const trace = await getTrace(sessionId);
        await insert(`\`\`\`json\n${JSON.stringify(trace, null, 2)}\n\`\`\``);
      } catch (error) {
        await insert(`⚠️ 获取追踪失败：${String((error as Error).message ?? error)}`);
      }
    }
  }

  async function send() {
    const prompt = input.trim();
    if (!prompt || busy) return;
    const command = parseCommand(prompt);
    if (command) {
      await executeCommand(command.name, command.argument);
      return;
    }
    setInput("");
    await runPrompt(prompt);
  }

  async function chooseDecision(request: DecisionRequest, choice: string, options?: { supplement?: string; answers?: Record<string, string[]> }) {
    try {
      await submitDecision(request.decision_id, choice, options);
      setLast({ decision: undefined });
    } catch (error) {
      setLast({ error: `决策提交失败：${String((error as Error).message ?? error)}` });
    }
  }

  function stop() {
    if (conversation && onStopRun) {
      onStopRun(conversation.id);
      return;
    }
    abortRef.current?.abort();
    setLast({ running: false, status: "已停止", decision: undefined });
    setLocalBusy(false);
  }

  async function openTimezoneMenu() {
    const { sessionId } = await ensureSession();
    const info = await getTimezone(sessionId);
    setTimezoneOptions(info.options);
    setTimezoneMenu(true);
  }

  async function rewindMessage(messageId: string) {
    if (!conversation || !onRewind || busy) return;
    const result = await onRewind(conversation.id, messageId);
    if (result === undefined) return;
    setInput(typeof result === "string" ? result : result.content);
    window.setTimeout(() => taRef.current?.focus(), 0);
  }

  function beginEdit(message: ChatMessage) {
    if (busy || !onRewind || !message.content) return;
    setEditingMessageId(message.id);
    setEditingDraft(message.content);
  }

  function cancelEdit() {
    setEditingMessageId(null);
    setEditingDraft("");
  }

  async function saveEdit(message: ChatMessage) {
    if (!conversation || !onRewind || busy || !editingDraft.trim() || editingDraft.trim() === message.content.trim()) {
      cancelEdit();
      return;
    }
    const result = await onRewind(conversation.id, message.id);
    if (result === undefined) return;
    const nextPrompt = editingDraft.trim();
    const sessionId = typeof result === "string" ? conversation.sessionId : result.sessionId;
    if (!sessionId) return;
    cancelEdit();
    await runPrompt(nextPrompt, { conversationId: conversation.id, sessionId });
  }

  function handleUserBubbleClick(event: ReactMouseEvent<HTMLDivElement>, message: ChatMessage) {
    if (
      busy ||
      !onRewind ||
      !message.content ||
      event.button !== 0 ||
      event.altKey ||
      event.ctrlKey ||
      event.metaKey ||
      event.shiftKey
    ) return;
    const target = event.target as HTMLElement;
    if (target.closest("a,button,textarea,input,code,pre,details,summary")) return;
    const selection = window.getSelection();
    if (selection && !selection.isCollapsed) return;
    beginEdit(message);
  }

  function forkMessage(messageId: string) {
    if (!conversation || !onFork || busy) return;
    void onFork(conversation.id, messageId);
  }

  return (
    <div className="chat-page">
      <div className="chat-scroll">
        {messages.length === 0 ? (
          <div className="welcome">
            <div className="logo">Mini-Agent</div>
            <p className="welcome-sub">向你的智能体提问，它会调用文件、Shell、Web 等工具完成任务</p>
          </div>
        ) : messages.map((message) => message.role === "user" ? (
          <div className="message user" key={message.id}>
            <div className="message-content">
              {editingMessageId === message.id ? (
                <div className="message-edit" aria-label="编辑用户消息">
                  <textarea
                    ref={editRef}
                    aria-label="编辑用户消息"
                    value={editingDraft}
                    onChange={(event) => setEditingDraft(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Escape") {
                        event.preventDefault();
                        cancelEdit();
                      } else if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
                        event.preventDefault();
                        void saveEdit(message);
                      }
                    }}
                    rows={Math.min(8, Math.max(2, editingDraft.split("\n").length))}
                  />
                  <div className="message-edit-actions">
                    <button type="button" onClick={cancelEdit}>取消</button>
                    <button type="button" onClick={() => void saveEdit(message)} disabled={!editingDraft.trim()}>保存并重新生成</button>
                  </div>
                </div>
              ) : (
                <div
                  className="bubble user-bubble"
                  onClick={(event) => handleUserBubbleClick(event, message)}
                  title={onRewind && !busy ? "点击编辑此消息" : undefined}
                >
                  <MarkdownContent text={message.content} />
                </div>
              )}
              {editingMessageId !== message.id ? (
                <MessageActions
                  msg={message}
                  busy={busy}
                  onRewind={onRewind ? () => void rewindMessage(message.id) : undefined}
                  onEdit={onRewind ? () => beginEdit(message) : undefined}
                />
              ) : null}
            </div>
          </div>
        ) : (
          <AssistantMessage
            key={message.id}
            msg={message}
            display={display}
            onDecision={chooseDecision}
            busy={busy}
            onFork={onFork ? () => forkMessage(message.id) : undefined}
          />
        ))}
      </div>
      <div className="composer">
        {commandMenuVisible ? (
          <div className="command-menu">
            {filteredCommands.map((command, index) => (
              <button
                key={command.name}
                className={`command-item${index === activeCommandIndex ? " selected" : ""}`}
                onMouseEnter={() => setActiveCommandIndex(index)}
                onClick={() => completeCommand(index)}
              >
                <span className="command-name">{command.name}</span>
                <span className="command-desc">{command.label} · {command.description}</span>
              </button>
            ))}
          </div>
        ) : null}
        {timezoneMenu ? (
          <div className="picker-menu timezone-menu">
            <div className="picker-title">会话时区</div>
            {timezoneOptions.map((option) => <button key={option.identifier} onClick={async () => { const { sessionId } = await ensureSession(); await setTimezone(sessionId, option.identifier); setTimezoneMenu(false); }}>{option.label} <small>{option.identifier}</small></button>)}
          </div>
        ) : null}
        {forkMenu ? (
          <div className="picker-menu fork-menu">
            <div className="picker-title">选择要分叉的运行</div>
            {forkOptions.length === 0 ? <div className="picker-empty">暂无可分叉运行</div> : forkOptions.map((run) => <button key={run.run_id} onClick={async () => { const session = await forkRun(run.run_id); setForkMenu(false); await onSelectSession(session.session_id); await onRefresh(); }}><b>{run.run_id.slice(0, 18)}…</b><small>{run.task} · {run.status}</small></button>)}
          </div>
        ) : null}
        <div className="composer-box">
          <textarea
            ref={taRef}
            value={input}
            onChange={(event) => changeInput(event.target.value)}
            onKeyDown={(event) => {
              const action = commandKeyAction({
                key: event.key,
                shiftKey: event.shiftKey,
                isComposing: event.nativeEvent.isComposing,
                menuVisible: commandMenuVisible,
              });
              if (action.type === "move") {
                event.preventDefault();
                setActiveCommandIndex((current) => nextCommandIndex(current, action.direction, filteredCommands.length));
                return;
              }
              if (action.type === "dismiss") {
                event.preventDefault();
                setCommandMenuDismissedFor(input);
                return;
              }
              if (action.type === "complete") {
                event.preventDefault();
                completeCommand();
                return;
              }
              if (action.type === "send") {
                event.preventDefault();
                void send();
              }
            }}
            placeholder="输入任务，按 Enter 发送"
            rows={1}
          />
          <div className="composer-toolbar">
            <div className="mode-picker">
              <button
                type="button"
                className="mode-trigger"
                disabled={busy}
                aria-expanded={modeMenu}
                aria-haspopup="menu"
                onClick={() => {
                  setModeMenu((current) => !current);
                  setPermissionMenu(false);
                  setDisplayMenu(false);
                  setReasoningMenu(false);
                }}
              >
                {mode === "plan" ? "📋 Plan" : "⚙ Agent"} <span>⌃</span>
              </button>
              {modeMenu ? (
                <div className="mode-menu composer-mode-menu">
                  <button type="button" className={mode === "agent" ? "selected" : ""} onClick={() => { onModeChange("agent"); setModeMenu(false); }}>⚙ Agent<small>执行工具并修改工作区</small></button>
                  <button type="button" className={mode === "plan" ? "selected" : ""} onClick={() => { onModeChange("plan"); setModeMenu(false); }}>📋 Plan<small>只读规划和讨论</small></button>
                </div>
              ) : null}
            </div>
            <div className="composer-picker">
              <button
                type="button"
                className="picker-trigger"
                disabled={busy}
                aria-expanded={permissionMenu}
                aria-haspopup="menu"
                onClick={() => {
                  setPermissionMenu((current) => !current);
                  setModeMenu(false);
                  setDisplayMenu(false);
                  setReasoningMenu(false);
                }}
              >
                {permissionMode === "full_access" ? "完全访问" : "逐次审批"} <span>⌃</span>
              </button>
              {permissionMenu ? (
                <div className="picker-menu composer-picker-menu">
                  <div className="picker-title">权限模式</div>
                  <button type="button" className={permissionMode === "approval_for_me" ? "selected" : ""} onClick={() => { setPermissionMode("approval_for_me"); setPermissionMenu(false); }}>逐次审批<small>每个需要确认的工具都询问</small></button>
                  <button type="button" className={permissionMode === "full_access" ? "selected" : ""} onClick={() => { setPermissionMode("full_access"); setPermissionMenu(false); }}>完全访问<small>工具自动批准，但 Plan Review 仍需确认</small></button>
                </div>
              ) : null}
            </div>
            <div className="composer-picker">
              <button
                type="button"
                className="picker-trigger"
                aria-expanded={displayMenu}
                aria-haspopup="menu"
                onClick={() => {
                  setDisplayMenu((current) => !current);
                  setModeMenu(false);
                  setPermissionMenu(false);
                  setReasoningMenu(false);
                }}
              >
                显示：{display} <span>⌃</span>
              </button>
              {displayMenu ? (
                <div className="picker-menu composer-picker-menu">
                  <div className="picker-title">显示级别</div>
                  {DISPLAY_LEVELS.map((level) => <button type="button" className={display === level ? "selected" : ""} key={level} onClick={() => { setDisplay(level); setDisplayMenu(false); }}>{level}</button>)}
                </div>
              ) : null}
            </div>
            <div className="composer-picker">
              <button
                type="button"
                className="picker-trigger"
                disabled={busy}
                aria-expanded={reasoningMenu}
                aria-haspopup="menu"
                onClick={() => {
                  setReasoningMenu((current) => !current);
                  setModeMenu(false);
                  setPermissionMenu(false);
                  setDisplayMenu(false);
                }}
              >
                思考：{REASONING_LABELS[reasoningEffort]} <span>⌃</span>
              </button>
              {reasoningMenu ? (
                <div className="picker-menu composer-picker-menu reasoning-picker-menu">
                  <div className="picker-title">思考等级</div>
                  {(["low", "medium", "high", "xhigh", "max"] as ReasoningEffort[]).map((level) => (
                    <button type="button" className={reasoningEffort === level ? "selected" : ""} key={level} onClick={() => { setReasoningEffort(level); setReasoningMenu(false); }}>
                      {REASONING_LABELS[level]}<small>{level}</small>
                    </button>
                  ))}
                </div>
              ) : null}
            </div>
          </div>
          {busy ? <button className="send-btn stop" onClick={stop}>停止</button> : <button className="send-btn" onClick={() => void send()} disabled={!input.trim()}>发送</button>}
        </div>
      </div>
    </div>
  );
}
