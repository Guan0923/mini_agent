import { useEffect, useRef, useState, type MouseEvent as ReactMouseEvent } from "react";
import {
  Alert,
  Avatar,
  Button,
  Collapse,
  Drawer,
  Grid,
  Input,
  Select,
  Space,
  Tooltip,
  App as AntApp,
  message as staticMessage,
} from "antd";
import {
  BranchesOutlined,
  CloseCircleOutlined,
  CopyOutlined,
  EditOutlined,
  FileTextOutlined,
  RollbackOutlined,
  ArrowUpOutlined,
  SettingOutlined,
  StopOutlined,
  ToolOutlined,
} from "@ant-design/icons";
import type { TextAreaRef } from "antd/es/input/TextArea";
import {
  compactSession,
  listSkills,
  streamChat,
  streamResume,
  submitDecision,
} from "../api";
import { HELP_TEXT, parseCommand } from "../commands";
import { commandKeyAction, commandSuggestions, completionText, nextCommandIndex } from "../commandCompletion";
import DecisionCard from "../components/DecisionCard";
import IconAction from "../components/IconAction";
import MarkdownContent from "../components/MarkdownContent";
import type {
  ChatMessage,
  ChatMode,
  Conversation,
  DecisionRequest,
  DisplayMode,
  Page,
  PermissionMode,
  ReasoningEffort,
  StreamMessage,
  ToolEvent,
} from "../types";

interface Props {
  conversation: Conversation | null;
  displayMode?: DisplayMode;
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

type SettingsSelectKey = "mode" | "permission" | "reasoning";

function nativeTextArea(ref: TextAreaRef | null): HTMLTextAreaElement | null {
  const native = ref?.nativeElement;
  if (!native) return null;
  if (typeof HTMLTextAreaElement !== "undefined" && native instanceof HTMLTextAreaElement) return native;
  return native.querySelector("textarea");
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
  const { message: contextMessage } = AntApp.useApp();
  const message = contextMessage && typeof contextMessage.success === "function" ? contextMessage : staticMessage;

  async function copy() {
    if (!msg.content) return;
    try {
      await copyText(msg.content);
      message.success("已复制");
    } catch {
      message.error("复制失败");
    }
  }

  return (
    <div className="message-actions" aria-label={`${msg.role === "user" ? "用户" : "Agent"}消息操作`}>
      <IconAction label="复制" icon={<CopyOutlined />} onClick={() => void copy()} disabled={!msg.content} />
      {onRewind ? <IconAction label="回溯" icon={<RollbackOutlined />} onClick={onRewind} disabled={busy} /> : null}
      {onEdit ? <IconAction label="编辑" icon={<EditOutlined />} onClick={onEdit} disabled={busy || !msg.content} /> : null}
      {onFork ? <IconAction label="Fork" icon={<BranchesOutlined />} onClick={onFork} disabled={busy || msg.running || !msg.content} /> : null}
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
        <ToolOutlined aria-hidden="true" />
        <b>{ev.message}</b>
        {display === "verbose" ? <span className="mono">{shown}</span> : null}
      </div>
    );
  }
  if (ev.kind === "tool_failed") return <Alert className="tool-line failed" type="error" showIcon icon={<CloseCircleOutlined />} title={ev.message} />;
  if (ev.kind === "tool_result") {
    const result = (ev.data?.result as string | undefined) ?? ev.message;
    return (
      <Collapse
        className="tool-result"
        ghost
        defaultActiveKey={display === "verbose" ? ["result"] : []}
        items={[{
          key: "result",
          label: <><FileTextOutlined /> {ev.data?.tool ? String(ev.data.tool) : "工具"} 结果</>,
          children: <pre>{result}</pre>,
        }]}
      />
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
      <Avatar className="avatar" size={32}>A</Avatar>
      <div className="bubble">
        {msg.events.length > 0 && display !== "minimal" ? (
          <div className="event-list">
            {msg.events.map((ev, index) => <ToolLine key={index} ev={ev} display={display} />)}
          </div>
        ) : null}
        {msg.decision ? (
          <DecisionCard request={msg.decision} onSubmit={(choice, options) => onDecision(msg.decision!, choice, options)} />
        ) : null}
        {msg.error ? <Alert className="error-text" type="error" showIcon title={`⚠️ ${msg.error}`} /> : msg.content ? <MarkdownContent text={msg.content} /> : msg.running && !msg.decision && display !== "minimal" ? (
          <div className="thinking" role="status" aria-label="思考中" data-state="thinking" aria-live="polite"><span className="dot" /><span className="dot" /><span className="dot" /></div>
        ) : null}
        {display !== "minimal" && (msg.status || (msg.metrics && msg.metrics.duration_ms != null)) ? (
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
  displayMode: configuredDisplayMode,
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
  const screens = Grid.useBreakpoint();
  const isMobile = screens.md === false && (typeof window === "undefined" || window.innerWidth < 768);
  const [input, setInput] = useState("");
  const [localBusy, setLocalBusy] = useState(false);
  const [permissionMode, setPermissionMode] = useState<PermissionMode>("approval_for_me");
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort>("medium");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [openSettingsSelect, setOpenSettingsSelect] = useState<SettingsSelectKey | null>(null);
  const [activeCommandIndex, setActiveCommandIndex] = useState(0);
  const [commandMenuDismissedFor, setCommandMenuDismissedFor] = useState<string | null>(null);
  const [editingMessageId, setEditingMessageId] = useState<string | null>(null);
  const [editingDraft, setEditingDraft] = useState("");
  const abortRef = useRef<AbortController | null>(null);
  const taRef = useRef<TextAreaRef>(null);
  const editRef = useRef<TextAreaRef>(null);
  const pendingCaretRef = useRef<number | null>(null);

  const messages = conversation?.messages ?? [];
  const busy = runningProp ?? localBusy;
  const filteredCommands = commandSuggestions(input);
  const commandMenuVisible = !busy && commandMenuDismissedFor !== input && filteredCommands.length > 0;
  const display = configuredDisplayMode ?? "medium";

  useEffect(() => {
    if (busy) setOpenSettingsSelect(null);
  }, [busy]);

  useEffect(() => {
    if (pendingCaretRef.current !== null) {
      const caret = pendingCaretRef.current;
      const textarea = nativeTextArea(taRef.current);
      textarea?.setSelectionRange(caret, caret);
      pendingCaretRef.current = null;
    }
  }, [input]);

  useEffect(() => () => abortRef.current?.abort(), []);

  useEffect(() => {
    if (editingMessageId) {
      editRef.current?.focus();
      nativeTextArea(editRef.current)?.select();
    }
  }, [editingMessageId]);

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


  async function executeCommand(name: string, argument: string) {
    setInput("");
    setCommandMenuDismissedFor(null);
    setActiveCommandIndex(0);
    setSettingsOpen(false);
    if (name === "/help") {
      await insert(HELP_TEXT);
      return;
    }
    if (name === "/new") {
      await onNew(argument || undefined);
      return;
    }
    if (name === "/skills") {
      try {
        const skills = await listSkills();
        await insert(`# 已发现技能（${skills.length} 个）\n\n${skills.map((skill) => `- \`${skill.name}\` — ${skill.description}`).join("\n") || "（无）"}`);
      } catch (error) {
        await insert(`⚠️ 获取技能失败：${String((error as Error).message ?? error)}`);
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


  const settingsControls = (
    <Space className="composer-settings-controls" size={[6, 6]} wrap>
      <Select
        className="mode-picker"
        placement="topLeft"
        open={openSettingsSelect === "mode"}
        aria-label="运行模式"
        disabled={busy}
        value={mode}
        options={[
          { value: "agent", label: "⚙ Agent" },
          { value: "plan", label: "📋 Plan" },
        ]}
        onChange={(value: ChatMode) => {
          onModeChange(value);
          setOpenSettingsSelect(null);
        }}
        onOpenChange={(open) => {
          setOpenSettingsSelect(open ? "mode" : null);
          if (open) {
          }
        }}
      />
      <Select
        className="composer-picker"
        placement="topLeft"
        open={openSettingsSelect === "permission"}
        aria-label="权限模式"
        disabled={busy}
        value={permissionMode}
        options={[
          { value: "approval_for_me", label: "逐次审批" },
          { value: "full_access", label: "完全访问" },
        ]}
        onChange={(value: PermissionMode) => {
          setPermissionMode(value);
          setOpenSettingsSelect(null);
        }}
        onOpenChange={(open) => {
          setOpenSettingsSelect(open ? "permission" : null);
          if (open) {
          }
        }}
      />
      <Select
        className="composer-picker"
        placement="topLeft"
        open={openSettingsSelect === "reasoning"}
        aria-label="思考等级"
        disabled={busy}
        value={reasoningEffort}
        options={(Object.keys(REASONING_LABELS) as ReasoningEffort[]).map((level) => ({
          value: level,
          label: `${level}`,
        }))}
        onChange={(value: ReasoningEffort) => {
          setReasoningEffort(value);
          setOpenSettingsSelect(null);
        }}
        onOpenChange={(open) => {
          setOpenSettingsSelect(open ? "reasoning" : null);
          if (open) {
          }
        }}
      />
    </Space>
  );

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
                  <Input.TextArea
                    className="message-edit-input"
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
                    autoSize={{ minRows: 2, maxRows: 8 }}
                  />
                  <div className="message-edit-actions">
                    <Button type="text" onClick={cancelEdit}>取消</Button>
                    <Button type="primary" onClick={() => void saveEdit(message)} disabled={!editingDraft.trim()}>保存并重新生成</Button>
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
        <div className="composer-box">
          <Input.TextArea
            className="composer-input"
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
            autoSize={{ minRows: 1, maxRows: 8 }}
          />
          <div className="composer-toolbar">
            {isMobile ? (
              <IconAction
                className="run-settings-trigger"
                label="运行设置"
                icon={<SettingOutlined />}
                disabled={busy}
                onClick={() => setSettingsOpen(true)}
              />
            ) : settingsControls}
          </div>
          {busy ? (
            <Tooltip title="停止">
              <Button className="send-btn stop" type="default" danger shape="circle" icon={<StopOutlined />} aria-label="停止" onClick={stop} />
            </Tooltip>
          ) : (
            <Tooltip title="发送">
              <Button className="send-btn" type="primary" shape="circle" icon={<ArrowUpOutlined />} aria-label="发送" onClick={() => void send()} disabled={!input.trim()} />
            </Tooltip>
          )}
        </div>
        <Drawer
          className="run-settings-drawer"
          title="运行设置"
          placement="bottom"
          open={settingsOpen}
          onClose={() => setSettingsOpen(false)}
        >
          {settingsControls}
        </Drawer>
      </div>
    </div>
  );
}
