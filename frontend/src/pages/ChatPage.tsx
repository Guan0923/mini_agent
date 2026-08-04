import { useEffect, useRef, useState } from "react";
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
  StreamMessage,
  ToolEvent,
} from "../types";

interface Props {
  conversation: Conversation | null;
  mode: ChatMode;
  onModeChange: (mode: ChatMode) => void;
  onUpdate: (id: string, updater: (conversation: Conversation) => Conversation) => void;
  onNew: (title?: string) => Promise<string>;
  onNavigate: (page: Page) => void;
  onSelectSession: (id: string) => Promise<void>;
  onReload: (id: string) => Promise<void>;
  onRefresh: () => Promise<void>;
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
}: {
  msg: ChatMessage;
  display: DisplayMode;
  onDecision: (request: DecisionRequest, choice: string, options?: { supplement?: string; answers?: Record<string, string[]> }) => Promise<void>;
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
          <div className="thinking"><span className="dot" /><span className="dot" /><span className="dot" /></div>
        ) : null}
        {msg.metrics && msg.metrics.duration_ms != null ? (
          <div className="meta">
            {msg.status ? `${msg.status} · ` : ""}
            {(msg.metrics.duration_ms / 1000).toFixed(1)}s · {msg.metrics.model_calls ?? 0} 次模型调用 · {msg.metrics.tool_calls ?? 0} 次工具调用
          </div>
        ) : null}
      </div>
    </div>
  );
}

export default function ChatPage({
  conversation,
  mode,
  onModeChange,
  onUpdate,
  onNew,
  onNavigate,
  onSelectSession,
  onReload,
  onRefresh,
}: Props) {
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [modeMenu, setModeMenu] = useState(false);
  const [permissionMode, setPermissionMode] = useState<PermissionMode>("approval_for_me");
  const [permissionMenu, setPermissionMenu] = useState(false);
  const [display, setDisplay] = useState<DisplayMode>("medium");
  const [displayMenu, setDisplayMenu] = useState(false);
  const [timezoneOptions, setTimezoneOptions] = useState<Array<{ identifier: string; label: string }>>([]);
  const [timezoneMenu, setTimezoneMenu] = useState(false);
  const [forkOptions, setForkOptions] = useState<ForkableRun[]>([]);
  const [forkMenu, setForkMenu] = useState(false);
  const [activeCommandIndex, setActiveCommandIndex] = useState(0);
  const [commandMenuDismissedFor, setCommandMenuDismissedFor] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const pendingCaretRef = useRef<number | null>(null);

  const messages = conversation?.messages ?? [];
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

  function updateLast(updater: (message: ChatMessage) => ChatMessage, sessionId = conversation?.id) {
    if (!sessionId) return;
    onUpdate(sessionId, (current) => {
      const currentMessages = [...current.messages];
      const index = currentMessages.length - 1;
      if (index < 0 || currentMessages[index].role !== "assistant") return current;
      currentMessages[index] = updater(currentMessages[index]);
      return { ...current, messages: currentMessages };
    });
  }

  function appendDelta(content: string, sessionId?: string) {
    updateLast((message) => ({ ...message, content: message.content + content }), sessionId);
  }

  function appendEvent(event: ToolEvent, sessionId?: string) {
    updateLast((message) => ({ ...message, events: [...message.events, event] }), sessionId);
  }

  function setLast(fields: Partial<ChatMessage>, sessionId?: string) {
    updateLast((message) => ({ ...message, ...fields }), sessionId);
  }

  async function ensureSession(): Promise<string> {
    if (conversation) return conversation.id;
    return onNew();
  }

  async function insert(content: string) {
    const id = await ensureSession();
    const message: ChatMessage = { id: crypto.randomUUID(), role: "assistant", content, events: [] };
    onUpdate(id, (current) => ({ ...current, messages: [...current.messages, message] }));
  }

  async function runStream(id: string, prompt: string | null, resume = false) {
    const controller = new AbortController();
    abortRef.current = controller;
    setBusy(true);
    try {
      const onMessage = (message: StreamMessage) => {
        if (message.type === "event") {
          const kind = message.kind ?? "";
          if (kind === "response_delta") {
            const content = (message.data?.content as string | undefined) ?? message.message ?? "";
            if (content) appendDelta(content, id);
          } else if (kind === "tool_call" || kind === "tool_result" || kind === "tool_failed") {
            appendEvent({ kind, message: message.message ?? "", data: message.data }, id);
          } else if (kind === "decision_requested" && message.data) {
            setLast({ decision: { ...message.data, message: message.message } as DecisionRequest }, id);
          } else if (kind === "run_finished") {
            setLast({ status: message.message }, id);
          }
        } else if (message.type === "done") {
          setLast({
            content: message.final_answer ?? "",
            status: message.status,
            metrics: message.metrics,
            running: false,
            decision: undefined,
          }, id);
          if (message.mode) onModeChange(message.mode);
          if (message.session_id && message.session_id !== id) {
            void onSelectSession(message.session_id);
          }
          void onRefresh();
        } else if (message.type === "error") {
          setLast({ error: message.error ?? message.message ?? "发生错误", running: false, decision: undefined }, id);
        }
      };
      if (resume) {
        await streamResume(id, onMessage, controller.signal, permissionMode);
      } else {
        await streamChat(prompt ?? "", onMessage, controller.signal, {
          sessionId: id,
          mode,
          permissionMode,
        });
      }
    } finally {
      setBusy(false);
      abortRef.current = null;
    }
  }

  async function runPrompt(prompt: string) {
    const id = await ensureSession();
    const userMessage: ChatMessage = { id: crypto.randomUUID(), role: "user", content: prompt, events: [] };
    const assistantMessage: ChatMessage = { id: crypto.randomUUID(), role: "assistant", content: "", events: [], running: true };
    onUpdate(id, (current) => ({
      ...current,
      title: current.title === "新对话" ? prompt.slice(0, 18) + (prompt.length > 18 ? "…" : "") : current.title,
      messages: [...current.messages, userMessage, assistantMessage],
    }));
    await runStream(id, prompt);
  }

  async function resumeSession(sessionId?: string) {
    const id = sessionId || conversation?.id;
    if (!id) {
      const sessions = await listSessions();
      if (!sessions[0]) return insert("没有可恢复的服务端会话。");
      return resumeSession(sessions[0].session_id);
    }
    const target = id === conversation?.id ? id : await onSelectSession(id).then(() => id);
    const assistant: ChatMessage = { id: crypto.randomUUID(), role: "assistant", content: "", events: [], running: true };
    onUpdate(target, (current) => ({ ...current, messages: [...current.messages, assistant] }));
    await runStream(target, null, true);
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
      const id = await ensureSession();
      if (argument) {
        try {
          await setTimezone(id, argument);
          await insert(`当前会话时区已设置为 **${argument}**。`);
        } catch (error) {
          await insert(`⚠️ 设置时区失败：${String((error as Error).message ?? error)}`);
        }
        return;
      }
      try {
        const info = await getTimezone(id);
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
        const result = await compactSession(conversation.id);
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
        const trace = await getTrace(conversation.id);
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
    abortRef.current?.abort();
  }

  async function openTimezoneMenu() {
    if (!conversation) return;
    const info = await getTimezone(conversation.id);
    setTimezoneOptions(info.options);
    setTimezoneMenu(true);
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
          <div className="message user" key={message.id}><div className="bubble"><MarkdownContent text={message.content} /></div></div>
        ) : (
          <AssistantMessage key={message.id} msg={message} display={display} onDecision={chooseDecision} />
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
        {permissionMenu ? (
          <div className="picker-menu permission-menu">
            <div className="picker-title">权限模式</div>
            <button className={permissionMode === "approval_for_me" ? "selected" : ""} onClick={() => { setPermissionMode("approval_for_me"); setPermissionMenu(false); }}>逐次审批<small>每个需要确认的工具都询问</small></button>
            <button className={permissionMode === "full_access" ? "selected" : ""} onClick={() => { setPermissionMode("full_access"); setPermissionMenu(false); }}>完全访问<small>工具自动批准，但 Plan Review 仍需确认</small></button>
          </div>
        ) : null}
        {displayMenu ? (
          <div className="picker-menu display-menu">
            <div className="picker-title">显示级别</div>
            {DISPLAY_LEVELS.map((level) => <button className={display === level ? "selected" : ""} key={level} onClick={() => { setDisplay(level); setDisplayMenu(false); }}>{level}</button>)}
          </div>
        ) : null}
        {timezoneMenu ? (
          <div className="picker-menu timezone-menu">
            <div className="picker-title">会话时区</div>
            {timezoneOptions.map((option) => <button key={option.identifier} onClick={async () => { if (conversation) { await setTimezone(conversation.id, option.identifier); } setTimezoneMenu(false); }}>{option.label} <small>{option.identifier}</small></button>)}
          </div>
        ) : null}
        {forkMenu ? (
          <div className="picker-menu fork-menu">
            <div className="picker-title">选择要分叉的运行</div>
            {forkOptions.length === 0 ? <div className="picker-empty">暂无可分叉运行</div> : forkOptions.map((run) => <button key={run.run_id} onClick={async () => { const session = await forkRun(run.run_id); setForkMenu(false); await onSelectSession(session.session_id); await onRefresh(); }}><b>{run.run_id.slice(0, 18)}…</b><small>{run.task} · {run.status}</small></button>)}
          </div>
        ) : null}
        {modeMenu ? <div className="mode-menu composer-mode-menu"><button className={mode === "agent" ? "selected" : ""} onClick={() => { onModeChange("agent"); setModeMenu(false); }}>⚙ Agent<small>执行工具并修改工作区</small></button><button className={mode === "plan" ? "selected" : ""} onClick={() => { onModeChange("plan"); setModeMenu(false); }}>📋 Plan<small>只读规划和讨论</small></button></div> : null}
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
              <button className="mode-trigger" disabled={busy} onClick={() => setModeMenu((current) => !current)}>
                {mode === "plan" ? "📋 Plan" : "⚙ Agent"} <span>⌃</span>
              </button>
            </div>
            <span className="composer-hint">{permissionMode === "full_access" ? "完全访问" : "逐次审批"} · {display}</span>
          </div>
          {busy ? <button className="send-btn stop" onClick={stop}>停止</button> : <button className="send-btn" onClick={() => void send()} disabled={!input.trim()}>发送</button>}
        </div>
      </div>
    </div>
  );
}
