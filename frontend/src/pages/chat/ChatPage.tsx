import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent } from "react";
import { Button, Grid, Input } from "antd";
import type { TextAreaRef } from "antd/es/input/TextArea";
import {
  compactSession,
  listSkills,
  patchRuntimeConfig,
  streamChat,
  streamResume,
  submitDecision,
} from "../../api";
import type { ProviderConfig } from "../../api";
import { HELP_TEXT, parseCommand } from "../../commands";
import { commandKeyAction, commandSuggestions, completionText, nextCommandIndex } from "../../commands/completion";
import MarkdownContent from "../../components/MarkdownContent";
import { AssistantMessage, MessageActions } from "./messageParts";
import Composer, { type SettingsSelectKey } from "./Composer";
import ConversationTimeline, { conversationTurnId } from "./ConversationTimeline";
import { latestTodoList } from "./todoPanel";
import { appendLegacyRuntimeEvent, integrateRuntimeNodeFrame, projectRuntimeNode } from "../../app/runtimeDetailProjection";
import { DEFAULT_RUNTIME_NODE_MODEL, normalizeRuntimeNodeModel } from "../../app/runtimeNodeNormalization";
import type {
  ChatMessage,
  ChatMode,
  Conversation,
  DecisionRequest,
  DisplayMode,
  FileReference,
  Page,
  PermissionMode,
  ReasoningEffort,
  RuntimeNodeModel,
  RuntimeStateNode,
  StreamMessage,
} from "../../types";

interface Props {
  conversation: Conversation | null;
  displayMode?: DisplayMode;
  providerConfig?: ProviderConfig | null;
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
  sourceNodeId?: string;
}

interface ChatRunRequest {
  conversationId: string;
  sessionId: string;
  prompt: string | null;
  resume: boolean;
  mode: ChatMode;
  permissionMode: PermissionMode;
  reasoningEffort: ReasoningEffort;
  providerName?: string;
  model?: RuntimeNodeModel;
  sourceNodeId?: string;
  references?: FileReference[];
}

function nativeTextArea(ref: TextAreaRef | null): HTMLTextAreaElement | null {
  return ref?.resizableTextArea?.textArea ?? null;
}

export default function ChatPage({
  conversation,
  displayMode: configuredDisplayMode,
  providerConfig,
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
  const [providerName, setProviderName] = useState("unknown");
  const [runtimeModel, setRuntimeModel] = useState<RuntimeNodeModel>(DEFAULT_RUNTIME_NODE_MODEL);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [openSettingsSelect, setOpenSettingsSelect] = useState<SettingsSelectKey | null>(null);
  const [activeCommandIndex, setActiveCommandIndex] = useState(0);
  const [commandMenuDismissedFor, setCommandMenuDismissedFor] = useState<string | null>(null);
  const [editingMessageId, setEditingMessageId] = useState<string | null>(null);
  const [editingDraft, setEditingDraft] = useState("");
  const [references, setReferences] = useState<FileReference[]>([]);
  const abortRef = useRef<AbortController | null>(null);
  const taRef = useRef<TextAreaRef>(null);
  const editRef = useRef<TextAreaRef>(null);
  const chatScrollRef = useRef<HTMLDivElement | null>(null);
  const pendingCaretRef = useRef<number | null>(null);

  const messages = conversation?.messages ?? [];
  const busy = runningProp ?? localBusy;
  const todo = useMemo(() => latestTodoList(messages), [messages]);
  const filteredCommands = commandSuggestions(input);
  const commandMenuVisible = !busy && commandMenuDismissedFor !== input && filteredCommands.length > 0;
  const display = configuredDisplayMode ?? "medium";

  const activeRuntimeNode = (() => {
    const nodes = (conversation?.runtimeNodes ?? []).filter((node) => node.data.type !== "root");
    if (!nodes.length) return undefined;
    if (conversation?.lastNodeId) {
      const selected = nodes.find((node) => node.id === conversation.lastNodeId && node.session_id === conversation.sessionId);
      if (selected) return selected;
    }
    const sorted = [...nodes].sort((left, right) => left.timestamp.localeCompare(right.timestamp) || left.id.localeCompare(right.id));
    return sorted[sorted.length - 1];
  })();

  useEffect(() => {
    const node = activeRuntimeNode;
    if (!node) return;
    const model = normalizeRuntimeNodeModel(node.model);
    setProviderName(node.provider_name || "unknown");
    setRuntimeModel(model);
    setPermissionMode(node.permission_mode || "approval_for_me");
    setReasoningEffort(model.reasoning_effort);
    if (node.running_mode && node.running_mode !== mode) onModeChange(node.running_mode);
  }, [activeRuntimeNode?.id, activeRuntimeNode?.provider_name, activeRuntimeNode?.model, activeRuntimeNode?.permission_mode, activeRuntimeNode?.running_mode]);

  function nearestUsage(node: RuntimeStateNode | undefined): { total: number; context: number } | undefined {
    if (!node) return undefined;
    const nodes = conversation?.runtimeNodes ?? [];
    const byKey = new Map(nodes.map((item) => [`${item.session_id}:${item.id}`, item] as const));
    let current: RuntimeStateNode | undefined = node;
    const seen = new Set<string>();
    while (current && !seen.has(`${current.session_id}:${current.id}`)) {
      seen.add(`${current.session_id}:${current.id}`);
      const total = current.usage?.total_tokens;
      const context = current.model?.context_length;
      if (typeof total === "number" && typeof context === "number" && context > 0) return { total, context };
      current = current.parent_id ? byKey.get(`${current.parent_session_id}:${current.parent_id}`) : undefined;
    }
    return undefined;
  }

  const activeUsage = nearestUsage(activeRuntimeNode);
  const activeRuntimeModel = normalizeRuntimeNodeModel(activeRuntimeNode?.model);
  const usagePercent = activeUsage ? Math.max(0, Math.min(100, (activeUsage.total / activeUsage.context) * 100)) : 0;
  const configuredProviderName = providerConfig?.provider_name?.trim();
  const requestProviderName = configuredProviderName || (providerName && providerName !== "unknown" ? providerName : undefined);
  const requestModel = (() => {
    if (providerConfig?.model) {
      return {
        ...runtimeModel,
        reasoning_effort: reasoningEffort,
        current_model: providerConfig.model,
        context_length: providerConfig.context_size,
        output_length: providerConfig.max_tokens,
      };
    }
    return runtimeModel.current_model && runtimeModel.current_model !== "unknown"
      ? { ...runtimeModel, reasoning_effort: reasoningEffort }
      : undefined;
  })();

  async function updateRuntimeConfig(patch: {
    provider_name?: string;
    model?: Partial<RuntimeNodeModel>;
    permission_mode?: PermissionMode;
    running_mode?: ChatMode;
  }) {
    if (!conversation?.sessionId || !activeRuntimeNode) return;
    try {
      await patchRuntimeConfig(conversation.sessionId, {
        node_id: activeRuntimeNode.id,
        provider_name: patch.provider_name,
        model: patch.model,
        permission_mode: patch.permission_mode,
        running_mode: patch.running_mode,
      });
    } catch (error) {
      setLast({ error: `运行配置更新失败：${String((error as Error).message ?? error)}` });
    }
  }

  useEffect(() => {
    if (!busy || !activeRuntimeNode || !configuredProviderName || !requestModel) return;
    if (
      activeRuntimeNode.provider_name === configuredProviderName
      && activeRuntimeModel.current_model === requestModel.current_model
      && activeRuntimeModel.context_length === requestModel.context_length
      && activeRuntimeModel.output_length === requestModel.output_length
    ) return;
    void updateRuntimeConfig({
      provider_name: configuredProviderName,
      model: requestModel,
    });
  }, [
    busy,
    activeRuntimeNode?.id,
    activeRuntimeNode?.provider_name,
    activeRuntimeModel.current_model,
    activeRuntimeModel.context_length,
    activeRuntimeModel.output_length,
    configuredProviderName,
    requestModel?.current_model,
    requestModel?.context_length,
    requestModel?.output_length,
  ]);

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

  async function runStream(
    conversationId: string,
    sessionId: string,
    prompt: string | null,
    resume = false,
    sourceNodeId?: string | null,
    references?: FileReference[],
  ) {
    const controller = new AbortController();
    abortRef.current = controller;
    setLocalBusy(true);
    try {
      let nodeProtocol = false;
      let sawDone = false;
      let finalNode: import("../../types").RuntimeStateNode | undefined;
      const onMessage = (message: StreamMessage) => {
        if (message.type === "event") {
          const kind = message.kind ?? "";
          if (kind === "response_delta" && !nodeProtocol) {
            const content = (message.data?.content as string | undefined) ?? message.message ?? "";
            if (content) appendDelta(content, conversationId);
          } else if (kind.startsWith("thinking_") || kind === "tool_call" || kind === "tool_result") {
            updateLast((item) => appendLegacyRuntimeEvent(item, {
              kind,
              message: message.message ?? "",
              data: message.data,
            }), conversationId);
          } else if (kind === "decision_requested" && message.data) {
            setLast({ decision: { ...message.data, message: message.message } as DecisionRequest }, conversationId);
          } else if (kind === "run_finished") {
            setLast({ status: message.message }, conversationId);
          }
          const runId = message.run_id ?? (typeof message.data?.run_id === "string" ? message.data.run_id : undefined);
          if (runId) setLast({ runId }, conversationId);
        } else if (message.type === "done") {
          sawDone = true;
          setLast({
            content: message.final_answer ?? "",
            status: message.status,
            metrics: message.metrics,
            ...(message.status === "completed" || message.status === "success" ? { error: undefined } : {}),
            running: false,
            decision: undefined,
            ...(message.run_id ? { runId: message.run_id } : {}),
          }, conversationId);
          if (message.mode) onModeChange(message.mode);
          if (message.session_id && message.session_id !== sessionId) {
            void onSelectSession(message.session_id);
          }
          void onRefresh();
        } else if ((message.type === "node.create" || message.type === "node.update" || message.type === "node.delete") && message.node) {
          nodeProtocol = true;
          const frame = { type: message.type, node: message.node } as const;
          onUpdate(conversationId, (current) => integrateRuntimeNodeFrame(current, frame));
          if (message.type === "node.delete") {
            finalNode = message.node;
          }
        } else if (message.type === "error") {
          setLast({ error: message.error ?? message.message ?? "发生错误", running: false, decision: undefined }, conversationId);
        }
      };
      if (resume) {
        const resumeSourceNodeId = sourceNodeId === undefined ? conversation?.lastNodeId : sourceNodeId ?? undefined;
        const result = resumeSourceNodeId
          ? await streamResume(
              sessionId,
              onMessage,
              controller.signal,
              permissionMode,
              reasoningEffort,
              resumeSourceNodeId,
              requestProviderName,
              requestModel,
              mode,
            )
          : await streamResume(sessionId, onMessage, controller.signal, permissionMode, reasoningEffort, undefined, requestProviderName, requestModel, mode);
        if (result === "aborted") setLast({
          running: false,
          status: "已停止",
          error: "The run was aborted at the user's request.",
          decision: undefined,
        }, conversationId);
        else if (!sawDone && finalNode) {
          const projection = projectRuntimeNode(finalNode);
          const content = projection?.content ?? "";
          setLast({
            content: content || "",
            status: finalNode.status,
            error: projection?.error,
            running: false,
            decision: undefined,
          }, conversationId);
          void onRefresh();
        }
      } else {
        // Keep the historical positional session-id call for an empty tree.
        // Once a node exists, use the object form so the optimistic source
        // node travels with the request and the backend can validate it.
        const chatSourceNodeId = sourceNodeId === undefined ? conversation?.lastNodeId : sourceNodeId ?? undefined;
        const options = chatSourceNodeId
          ? (enhancedChatOptions
            ? { sessionId, mode, permissionMode, reasoningEffort, providerName: requestProviderName, model: requestModel, sourceNodeId: chatSourceNodeId, references }
            : { sessionId, sourceNodeId: chatSourceNodeId, providerName: requestProviderName, model: requestModel, mode, permissionMode, reasoningEffort, references })
          // An empty tree has no dynamic runtime configuration to submit. Keep
          // the stable positional call for clients embedding ChatPage while
          // all established sessions use the explicit v0.3 config object.
          : (enhancedChatOptions
            ? { sessionId, mode, permissionMode, reasoningEffort, providerName: requestProviderName, model: requestModel, references }
            : sessionId);
        const result = await streamChat(
          prompt ?? "",
          onMessage,
          controller.signal,
          options,
        );
        if (result === "aborted") setLast({
          running: false,
          status: "已停止",
          error: "The run was aborted at the user's request.",
          decision: undefined,
        }, conversationId);
        else if (!sawDone && finalNode) {
          const projection = projectRuntimeNode(finalNode);
          const content = projection?.content ?? "";
          setLast({
            content: content || "",
            status: finalNode.status,
            error: projection?.error,
            running: false,
            decision: undefined,
          }, conversationId);
          void onRefresh();
        }
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

  async function dispatchRun(
    conversationId: string,
    sessionId: string,
    prompt: string | null,
    resume = false,
    sourceNodeId: string | null = conversation?.lastNodeId ?? null,
    references?: FileReference[],
  ) {
    if (onRun) {
      await onRun({
        conversationId,
        sessionId,
        prompt,
        resume,
        mode,
        permissionMode,
        reasoningEffort,
        providerName: requestProviderName,
        model: requestModel,
        sourceNodeId: sourceNodeId ?? undefined,
        references,
      });
      return;
    }
    await runStream(conversationId, sessionId, prompt, resume, sourceNodeId, references);
  }

  async function runPrompt(
    prompt: string,
    target?: { conversationId: string; sessionId: string; sourceNodeId?: string },
    references?: FileReference[],
  ) {
    const { conversationId, sessionId } = target ?? await ensureSession();
    const userMessage: ChatMessage = { id: crypto.randomUUID(), role: "user", content: prompt, events: [], references };
    const assistantMessage: ChatMessage = { id: crypto.randomUUID(), role: "assistant", content: "", events: [], running: true };
    onUpdate(conversationId, (current) => ({
      ...current,
      title: current.title === "新对话" ? prompt.slice(0, 18) + (prompt.length > 18 ? "…" : "") : current.title,
      messageCount:
        (current.messages.length > 0
          ? current.messages.filter((message) => message.role === "user" || message.role === "assistant").length
          : current.messageCount ?? 0) + 2,
      messages: [...current.messages, userMessage, assistantMessage],
    }));
    await dispatchRun(
      conversationId,
      sessionId,
      prompt,
      false,
      target ? target.sourceNodeId ?? null : conversation?.lastNodeId ?? null,
      references,
    );
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
    await runPrompt(prompt, undefined, references.length > 0 ? references : undefined);
    setReferences([]);
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
    setLast({
      running: false,
      status: "已停止",
      error: "The run was aborted at the user's request.",
      decision: undefined,
    });
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
    await runPrompt(nextPrompt, {
      conversationId: conversation.id,
      sessionId,
      sourceNodeId: typeof result === "string" ? undefined : result.sourceNodeId,
    });
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


  function handleComposerKeyDown(event: ReactKeyboardEvent<HTMLTextAreaElement>) {
    const action = commandKeyAction({ key: event.key, shiftKey: event.shiftKey, isComposing: event.nativeEvent.isComposing, menuVisible: commandMenuVisible });
    if (action.type === "move") { event.preventDefault(); setActiveCommandIndex((current) => nextCommandIndex(current, action.direction, filteredCommands.length)); return; }
    if (action.type === "dismiss") { event.preventDefault(); setCommandMenuDismissedFor(input); return; }
    if (action.type === "complete") { event.preventDefault(); completeCommand(); return; }
    if (action.type === "send") { event.preventDefault(); void send(); }
  }

  return (
    <div className="chat-page">
      <div className="chat-content">
        <div className="chat-scroll" ref={chatScrollRef}>
          <div className="chat-scroll-content">
            <div className="chat-messages">
              {messages.length === 0 ? (
                <div className="welcome">
                  <div className="logo">Mini-Agent</div>
                  <p className="welcome-sub">向你的智能体提问，它会调用文件、Shell、Web 等工具完成任务</p>
                </div>
              ) : messages.map((message) => message.role === "user" ? (
                <div className="message user" id={conversationTurnId(message.id)} key={message.id}>
                  <div className={editingMessageId === message.id ? "message-content is-editing" : "message-content"}>
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
            {!isMobile ? <ConversationTimeline messages={messages} scrollContainerRef={chatScrollRef} /> : null}
          </div>
        </div>
      </div>
      <Composer
        input={input}
        busy={busy}
        isMobile={isMobile}
        filteredCommands={filteredCommands}
        commandMenuVisible={commandMenuVisible}
        activeCommandIndex={activeCommandIndex}
        mode={mode}
        permissionMode={permissionMode}
        reasoningEffort={reasoningEffort}
        todos={todo}
        usagePercent={usagePercent}
        usageTotalTokens={activeUsage?.total ?? null}
        usageContextLength={activeUsage?.context}
        openSettingsSelect={openSettingsSelect}
        settingsOpen={settingsOpen}
        taRef={taRef}
        onInputChange={changeInput}
        onKeyDown={handleComposerKeyDown}
        onComplete={completeCommand}
        onActiveCommandChange={setActiveCommandIndex}
        onModeChange={(value) => { onModeChange(value); void updateRuntimeConfig({ running_mode: value }); setOpenSettingsSelect(null); }}
        onPermissionChange={(value) => { setPermissionMode(value); void updateRuntimeConfig({ permission_mode: value }); setOpenSettingsSelect(null); }}
        onReasoningChange={(value) => { setReasoningEffort(value); setRuntimeModel((current) => ({ ...current, reasoning_effort: value })); void updateRuntimeConfig({ model: { reasoning_effort: value } }); setOpenSettingsSelect(null); }}
        onSettingsSelectChange={setOpenSettingsSelect}
        onOpenSettings={() => setSettingsOpen(true)}
        onCloseSettings={() => setSettingsOpen(false)}
        onStop={stop}
        onSend={() => void send()}
        disabled={conversation?.projectId !== undefined && conversation.projectAvailable === false}
        disabledReason={conversation?.projectAvailable === false ? "项目 cwd 不可用，恢复文件夹后才能运行" : undefined}
      />
    </div>
  );
}
