import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { App as AntApp, Button, Dropdown, FloatButton, Grid, Tooltip } from "antd";
import { BranchesOutlined, CommentOutlined, NodeIndexOutlined, VerticalAlignBottomOutlined } from "@ant-design/icons";
import {
  compactTurn,
  createQueuedMessage,
  deleteQueuedMessage,
  listSkills,
  sessionFileContentUrl,
  steerTurn,
  submitDecision,
  updateQueuedMessage,
} from "../../api";
import { HELP_TEXT, parseCommand } from "../../commands";
import { commandKeyAction, commandSuggestions, completionText, nextCommandIndex } from "../../commands/completion";
import { fileKeyAction } from "../../commands/fileCompletion";
import Composer from "./Composer";
import { latestTodoList } from "./todoPanel";
import { messagesBeforeRewind, projectTurnPath, pruneTurnDescendants } from "../../app/runtime/runtimeDetailProjection";
import { leafNodes } from "../../app/runtime/runtimeNodeReducer";
import type { QueuedMessage } from "../../app/types";
import { isRuntimeTurnNode } from "../../app/runtime/runtimeNodeNormalization";
import type {
  ChatMessage,
  ChatMode,
  Conversation,
  DecisionRequest,
  FileReference,
  RuntimeStateNode,
} from "../../types";
import { ChatMessageList } from "./ChatMessageList";
import TracePage from "./TracePage";
import { composerAction, type ChatPageProps } from "./contracts";
import { useComposerFiles } from "./useComposerFiles";
import { useMessageEditing } from "./useMessageEditing";
import { useRuntimeControls } from "./useRuntimeControls";

export { composerAction } from "./contracts";

const BOTTOM_THRESHOLD_PX = 24;
export const CHAT_COMPACT_WIDTH = 700;

function isScrollContainerAtBottom(scrollContainer: HTMLDivElement): boolean {
  return scrollContainer.scrollHeight - scrollContainer.scrollTop - scrollContainer.clientHeight <= BOTTOM_THRESHOLD_PX;
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
  queuedMessages = [],
  onQueuedMessagesChange = () => undefined,
  onQueuedMessagesRefresh = async () => undefined,
  sandboxHealth = { phase: "healthy", detail: null },
}: ChatPageProps) {
  const { message } = AntApp.useApp();
  const mode = selectedMode ?? "agent";
  const screens = Grid.useBreakpoint();
  const isMobile = screens.md === false && (typeof window === "undefined" || window.innerWidth < 768);
  const chatPageRef = useRef<HTMLDivElement | null>(null);
  const measuredChatWidthRef = useRef<number | null>(null);
  const [compact, setCompact] = useState(isMobile);
  const compactRef = useRef(isMobile);
  const [queueSubmitting, setQueueSubmitting] = useState(false);
  const [compactionPending, setCompactionPending] = useState(false);
  const [activeCommandIndex, setActiveCommandIndex] = useState(0);
  const [commandMenuDismissedFor, setCommandMenuDismissedFor] = useState<string | null>(null);
  const [isAtBottom, setIsAtBottom] = useState(true);
  const [dismissedTodoPanels, setDismissedTodoPanels] = useState<Set<string>>(() => new Set());
  const [mainView, setMainView] = useState<"chat" | "trace">("chat");
  const chatScrollRef = useRef<HTMLDivElement | null>(null);
  const shouldStickToBottomRef = useRef(true);
  const scrollConversationIdRef = useRef<string | undefined>(undefined);
  const queueFlushRef = useRef(false);
  // IDs captured when a queue flush starts. Items added while that flush
  // is running belong to the next FIFO pass and must never be removed when
  // the submitted user frames are acknowledged.
  const queueAutoBlockedRef = useRef(false);
  const acknowledgedDeliveryIdsRef = useRef(new Set<string>());
  const [editingQueuedMessageId, setEditingQueuedMessageId] = useState<string | null>(null);

  useEffect(() => {
    const element = chatPageRef.current;
    if (!element) return;
    const applyWidth = (width: number) => {
      if (width > 0) measuredChatWidthRef.current = width;
      const measuredWidth = measuredChatWidthRef.current;
      const next = isMobile || (measuredWidth != null && measuredWidth < CHAT_COMPACT_WIDTH);
      if (compactRef.current === next) return;
      compactRef.current = next;
      setCompact(next);
    };
    applyWidth(element.getBoundingClientRect().width);
    if (typeof ResizeObserver !== "function") return;
    const observer = new ResizeObserver((entries) => {
      applyWidth(entries[0]?.contentRect.width ?? element.getBoundingClientRect().width);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [isMobile]);

  useEffect(() => {
    setEditingQueuedMessageId(null);
  }, [conversation?.id]);

  const messages = conversation?.messages ?? [];
  // A queue flush has no optimistic assistant message by design. Keep the
  // composer in its running interaction mode from the moment the flush
  // request is sent until its SSE cleanup, including the tiny interval
  // between the optimistic user bubble and the first turn.snapshot frame.
  const busy = Boolean(runningProp) || queueSubmitting;
  const sandboxBlocked = sandboxHealth.phase !== "healthy";
  const interactionBusy = busy || compactionPending || sandboxBlocked;
  const composerFiles = useComposerFiles({
    conversationId: conversation?.id,
    sessionId: conversation?.sessionId,
    interactionBusy,
    onTextChanged: () => {
      setCommandMenuDismissedFor(null);
      setActiveCommandIndex(0);
    },
  });
  const {
    input,
    setInput,
    references,
    setReferences,
    pendingUploads,
    setPendingUploads,
    fileCandidates,
    activeFileIndex,
    setActiveFileIndex,
    fileTriggerState,
    fileMenuAvailable,
    setFileMenuDismissedFor,
    editorRef,
    handleEditorChange,
    completeFile,
    handlePickFiles,
    removePendingUpload,
    retryUpload,
    collectedReferences,
    clearComposer,
  } = composerFiles;
  const todo = useMemo(() => latestTodoList(messages), [messages]);
  const filteredCommands = commandSuggestions(input);
  const commandMenuVisible = !interactionBusy && commandMenuDismissedFor !== input && filteredCommands.length > 0;
  // The file menu is mutually exclusive with the slash-command menu and only
  // appears while the caret still sits inside an `@` trigger.
  const fileMenuVisible = !commandMenuVisible && fileMenuAvailable;
  const display = configuredDisplayMode ?? "medium";

  const activeRuntimeNode = (() => {
    const nodes = (conversation?.runtimeNodes ?? []).filter(
      (node) => !conversation?.threadId || node.thread_id === conversation.threadId,
    );
    if (conversation?.activeTurnId) {
      const persisted = nodes.find((node) => node.id === conversation.activeTurnId);
      if (persisted && isRuntimeTurnNode(persisted)) return persisted;
    }
    const sessionLeaves = leafNodes(nodes, conversation?.sessionId);
    if (!sessionLeaves.length) return undefined;
    if (conversation?.lastNodeId) {
      const selected = sessionLeaves.find(
        (node) => node.id === conversation.lastNodeId && node.session_id === conversation.sessionId,
      );
      if (selected) return selected;
    }
    const sorted = [...sessionLeaves].sort((left, right) => left.timestamp.localeCompare(right.timestamp) || left.id.localeCompare(right.id));
    return sorted[sorted.length - 1];
  })();
  const todoPanelKey = `${conversation?.id ?? "draft"}:${activeRuntimeNode?.id ?? conversation?.activeTurnId ?? "no-turn"}`;
  const todoCompleted = todo !== null && todo.length > 0 && todo.every((item) => item.status === "completed");
  const visibleTodo = todo?.length && !todoCompleted && !dismissedTodoPanels.has(todoPanelKey) ? todo : null;
  const todoClosable = Boolean(visibleTodo) && !busy;
  const closeTodoPanel = useCallback(() => {
    setDismissedTodoPanels((current) => {
      if (current.has(todoPanelKey)) return current;
      const next = new Set(current);
      next.add(todoPanelKey);
      return next;
    });
  }, [todoPanelKey]);
  const currentThreadId = activeRuntimeNode?.thread_id ?? conversation?.threadId ?? conversation?.sessionId;
  const traceTurns = (conversation?.runtimeNodes ?? []).filter(
    (node): node is RuntimeStateNode => isRuntimeTurnNode(node)
      && node.thread_id === currentThreadId
      && node.id !== conversation?.hiddenBeforeTurnId,
  );
  const hasTurnTree = traceTurns.length > 0;
  const visibleMainView = hasTurnTree ? mainView : "chat";
  useEffect(() => {
    if (!hasTurnTree) setMainView("chat");
  }, [conversation?.id, hasTurnTree]);
  const runtimeControls = useRuntimeControls({
    conversation,
    activeRuntimeNode,
    busy: busy || sandboxBlocked,
    providerConfig,
    mode,
    onModeChange,
    onFailure: (error) => setLast({ error: `运行配置更新失败：${String((error as Error).message ?? error)}` }),
  });
  const {
    permissionMode,
    reasoningEffort,
    runtimeConfigPending,
    openSettingsSelect,
    setOpenSettingsSelect,
    activeUsage,
    usagePercent,
    requestProviderName,
    requestModel,
    changeRunningMode,
    changePermissionMode,
    changeReasoningEffort,
  } = runtimeControls;
  const messageEditing = useMessageEditing({
    conversation,
    interactionBusy,
    activeRuntimeNode,
    onRewind,
    onFork,
    onUpdate,
    runPrompt,
    onError: (error) => setLast({ error: String((error as Error).message ?? error) }),
  });
  const {
    editingMessageId,
    editingDraft,
    setEditingDraft,
    rewindPending,
    editingSubmitting,
    editRef,
    beginEdit,
    cancelEdit,
    saveEdit,
    handleUserBubbleClick,
    forkMessage,
    changeMessageVersion,
    messageVersion,
  } = messageEditing;
  const hasDraft = Boolean(input.trim() || references.length > 0 || pendingUploads.some((upload) => upload.status === "done"));
  const composerActionState = composerAction(
    activeRuntimeNode?.status,
    hasDraft,
    pendingUploads.some((upload) => upload.status === "uploading"),
  );
  const actionMode = composerActionState.mode;
  const projectUnavailable = conversation?.projectId !== undefined && conversation.projectAvailable === false;

  const syncBottomState = useCallback((scrollContainer: HTMLDivElement) => {
    const nextIsAtBottom = isScrollContainerAtBottom(scrollContainer);
    shouldStickToBottomRef.current = nextIsAtBottom;
    setIsAtBottom((current) => current === nextIsAtBottom ? current : nextIsAtBottom);
  }, []);

  useLayoutEffect(() => {
    const scrollContainer = chatScrollRef.current;
    if (!scrollContainer) return;
    const conversationChanged = scrollConversationIdRef.current !== conversation?.id;
    scrollConversationIdRef.current = conversation?.id;
    if (conversationChanged) shouldStickToBottomRef.current = true;
    if (!shouldStickToBottomRef.current) return;
    scrollContainer.scrollTop = scrollContainer.scrollHeight;
    syncBottomState(scrollContainer);
  }, [conversation?.id, conversation?.messages, syncBottomState]);

  useEffect(() => {
    const scrollContainer = chatScrollRef.current;
    const scrollContent = scrollContainer?.querySelector<HTMLElement>(".chat-scroll-content");
    if (!scrollContainer || !scrollContent || typeof ResizeObserver !== "function") return;
    const observer = new ResizeObserver(() => {
      if (shouldStickToBottomRef.current) scrollContainer.scrollTop = scrollContainer.scrollHeight;
      syncBottomState(scrollContainer);
    });
    observer.observe(scrollContainer);
    observer.observe(scrollContent);
    return () => observer.disconnect();
  }, [conversation?.id, syncBottomState]);

  useEffect(() => {
    const status = activeRuntimeNode?.status;
    if (status === "running") {
      queueAutoBlockedRef.current = false;
      return;
    }
    if (
      !sandboxBlocked
      && !queueFlushRef.current
      && !queueAutoBlockedRef.current
      && queuedMessages.length > 0
      && conversation?.id
      && (status === "success" || status === "failed")
    ) {
      queueFlushRef.current = true;
      setQueueSubmitting(true);
      void flushQueuedMessages();
    }
  }, [activeRuntimeNode?.id, activeRuntimeNode?.status, busy, queuedMessages.length, conversation?.id, sandboxBlocked]);

  useEffect(() => {
    if (!conversation?.id || !activeRuntimeNode) return;
    const ids = activeRuntimeNode.data[activeRuntimeNode.current_data_idx]
      ?.filter((item) => item.role === "user" && typeof item.delivery_id === "string")
      .map((item) => String(item.delivery_id)) ?? [];
    const fresh = ids.filter((id) => !acknowledgedDeliveryIdsRef.current.has(id));
    if (fresh.length === 0) return;
    fresh.forEach((id) => acknowledgedDeliveryIdsRef.current.add(id));
    void onQueuedMessagesRefresh(conversation.id);
  }, [activeRuntimeNode?.data, activeRuntimeNode?.current_data_idx, conversation?.id, onQueuedMessagesRefresh]);

  function completeCommand(index = activeCommandIndex) {
    const command = filteredCommands[index];
    if (!command) return;
    const value = completionText(command);
    editorRef.current?.clear();
    editorRef.current?.insertText(value);
    setCommandMenuDismissedFor(value);
    setActiveCommandIndex(0);
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

  function setLast(fields: Partial<ChatMessage>, conversationId?: string) {
    updateLast((message) => ({ ...message, ...fields }), conversationId);
  }

  function defaultSourceNodeId(): string | undefined {
    return conversation?.lastNodeId;
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

  async function dispatchRun(
    conversationId: string,
    sessionId: string,
    prompt: string | null,
    resume = false,
    sourceNodeId: string | null = defaultSourceNodeId() ?? null,
    references?: FileReference[],
    rewindTurnId?: string,
    waitForActiveRun = false,
    onBaseline?: (turn: RuntimeStateNode) => void,
    queuedDelivery?: { deliveryId: string; messageIds: string[] },
  ) {
    if (sandboxBlocked) throw new Error("沙箱 Broker 尚未确认健康。");
    if (!onRun) throw new Error("ChatPage requires the Turn run controller.");
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
        threadId: conversation?.threadId ?? sessionId,
        turnId: crypto.randomUUID(),
        references,
        rewindTurnId,
        waitForActiveRun,
        onBaseline,
        queuedDelivery,
    });
  }

  function updateQueue(updater: (items: QueuedMessage[]) => QueuedMessage[]) {
    if (!conversation?.id) return;
    onQueuedMessagesChange(conversation.id, updater);
  }

  async function queueCurrentPrompt(prompt: string, itemReferences?: FileReference[]) {
    if (!prompt.trim() && (!itemReferences || itemReferences.length === 0)) return;
    if (!conversation?.id || !conversation.threadId) return;
    try {
      const stored = editingQueuedMessageId
        ? await updateQueuedMessage(conversation.threadId, editingQueuedMessageId, prompt, itemReferences ?? [])
        : await createQueuedMessage(conversation.threadId, crypto.randomUUID(), prompt, itemReferences ?? []);
      updateQueue((items) => editingQueuedMessageId
        ? items.map((item) => item.id === stored.id ? stored : item)
        : [...items, stored]);
      setEditingQueuedMessageId(null);
    } catch (error) {
      setLast({ error: String((error as Error).message ?? error) });
      return;
    }
    clearComposer();
    // The uploaded files are already represented by references on this queue
    // item.  Detach them from the composer so a subsequent queued message
    // cannot accidentally inherit the same upload; keep the server-side files
    // because the queue item still needs them when its Turn is sent.
    setPendingUploads([]);
  }

  function editQueuedMessage(item: QueuedMessage) {
    if (item.state !== "pending") return;
    const currentPrompt = input.trim();
    const currentReferences = collectedReferences();
    if (currentPrompt || currentReferences.length > 0) {
      void message.warning("输入框有内容，无法修改队列消息");
      return;
    }
    setEditingQueuedMessageId(item.id);
    editorRef.current?.restore(item.content, item.references);
    setInput(item.content);
    setReferences(item.references ?? []);
    window.setTimeout(() => editorRef.current?.focus(), 0);
  }

  function sendQueuedMessage(item: QueuedMessage) {
    if (item.state !== "pending") return;
    void submitSteering([item]);
  }

  async function submitSteering(items: QueuedMessage[]) {
    if (sandboxBlocked || !conversation?.id || !activeRuntimeNode || activeRuntimeNode.status !== "running" || items.length === 0) return;
    const deliveryId = crypto.randomUUID();
    try {
      await steerTurn(activeRuntimeNode.id, deliveryId, items.map((item) => item.id));
      await onQueuedMessagesRefresh(conversation.id);
    } catch (error) {
      setLast({ error: String((error as Error).message ?? error) });
    }
  }

  function pauseOrSteer() {
    const pending = queuedMessages.filter((item) => item.state === "pending");
    if (pending.length > 0) {
      void submitSteering(pending);
      return;
    }
    if (queuedMessages.length > 0) return;
    stop();
  }

  async function flushQueuedMessages() {
    // Snapshot both content and IDs. React may persist a new queue while this
    // request is in flight; that new content belongs to the next merged Turn.
    const items = queuedMessages.slice();
    if (sandboxBlocked) {
      queueFlushRef.current = false;
      setQueueSubmitting(false);
      return;
    }
    if (!conversation?.sessionId || items.length === 0) {
      queueFlushRef.current = false;
      setQueueSubmitting(false);
      return;
    }
    const pendingItems = items.filter((item) => item.state === "pending");
    if (pendingItems.length === 0) {
      queueFlushRef.current = false;
      setQueueSubmitting(false);
      return;
    }
    const deliveryId = crypto.randomUUID();
    let acknowledged = false;
    try {
      const source = activeRuntimeNode;
      await dispatchRun(
        conversation.id,
        conversation.sessionId,
        null,
        false,
        source?.id ?? null,
        undefined,
        undefined,
        true,
        () => {
          if (acknowledged) return;
          acknowledged = true;
          void onQueuedMessagesRefresh(conversation.id);
        },
        { deliveryId, messageIds: pendingItems.map((item) => item.id) },
      );
      if (!acknowledged) queueAutoBlockedRef.current = true;
    } catch (error) {
      // A rejected Turn leaves its in-memory queue items untouched. Surface the
      // failure on the current assistant projection without converting the
      // queued content into optimistic canonical messages.
      setLast({ error: String((error as Error).message ?? error), running: false, decision: undefined });
      queueAutoBlockedRef.current = true;
    } finally {
      queueFlushRef.current = false;
      setQueueSubmitting(false);
    }
  }

  async function runPrompt(
    prompt: string,
    target?: { conversationId: string; sessionId: string; sourceNodeId?: string; rewindTurnId?: string },
    references?: FileReference[],
  ) {
    const { conversationId, sessionId } = target ?? await ensureSession();
    const userMessage: ChatMessage = { id: crypto.randomUUID(), role: "user", content: prompt, events: [], references };
    const assistantMessage: ChatMessage = { id: crypto.randomUUID(), role: "assistant", content: "", events: [], running: true };
    onUpdate(conversationId, (current) => {
      let visibleMessages = current.messages;
      let runtimeNodes = current.runtimeNodes;
      if (target?.rewindTurnId) {
        if (current.runtimeNodes) {
          runtimeNodes = pruneTurnDescendants(current.runtimeNodes, target.rewindTurnId);
          const map = new Map(runtimeNodes.map((node) => [`${node.session_id}:${node.id}`, node] as const));
          visibleMessages = map.has(`${sessionId}:${target.rewindTurnId}`)
            ? messagesBeforeRewind(projectTurnPath(map, target.rewindTurnId), target.rewindTurnId)
            : messagesBeforeRewind(current.messages, target.rewindTurnId);
        } else {
          visibleMessages = messagesBeforeRewind(current.messages, target.rewindTurnId);
        }
      }
      const messages = [...visibleMessages, userMessage, assistantMessage];
      return {
        ...current,
        messageCount: messages.filter((message) => message.role === "user" || message.role === "assistant").length,
        messages,
        runtimeNodes,
        activeTurnId: target?.rewindTurnId ?? current.activeTurnId,
        lastNodeId: target?.rewindTurnId ?? current.lastNodeId,
      };
    });
    await dispatchRun(
      conversationId,
      sessionId,
      prompt,
      false,
      target ? target.sourceNodeId ?? null : defaultSourceNodeId() ?? null,
      references,
      target?.rewindTurnId,
      Boolean(activeRuntimeNode && activeRuntimeNode.status !== "running"),
    );
  }


  async function executeCommand(name: string, argument: string) {
    if (compactionPending) return;
    clearComposer();
    setCommandMenuDismissedFor(null);
    setActiveCommandIndex(0);
    if (name === "/trace") {
      setMainView("trace");
      return;
    }
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
      if (!conversation || !activeRuntimeNode) return;
      setCompactionPending(true);
      try {
        const compacted = await compactTurn(activeRuntimeNode.id);
        await onReload(conversation.id, compacted.id);
      } catch (error) {
        await insert(`⚠️ 压缩失败：${String((error as Error).message ?? error)}`);
      } finally {
        setCompactionPending(false);
      }
    }
  }

  async function send() {
    if (compactionPending || sandboxBlocked) return;
    const prompt = input.trim();
    // A running assistant no longer blocks the composer: a draft is handed
    // to the in-memory FIFO queue below.  Only an in-progress upload prevents
    // submission because its final reference is not available yet.
    if (pendingUploads.some((upload) => upload.status === "uploading")) return;
    if (actionMode === "resume" && conversation?.sessionId && activeRuntimeNode) {
      await dispatchRun(
        conversation.id,
        conversation.sessionId,
        null,
        true,
        activeRuntimeNode.id,
        undefined,
        undefined,
        true,
      );
      return;
    }
    const mergedReferences = collectedReferences();
    if (!prompt && mergedReferences.length === 0) return;
    // Slash commands are control actions, not conversational turns.  They
    // must never be persisted into the running FIFO queue.  Keep command
    // handling ahead of the running branch so `/new`, `/compact`, etc. remain
    // explicit commands even while an assistant is active.
    const command = parseCommand(prompt);
    if (command && prompt) {
      await executeCommand(command.name, command.argument);
      return;
    }
    if (activeRuntimeNode?.status === "running") {
      await queueCurrentPrompt(prompt, mergedReferences);
      return;
    }
    clearComposer();
    await runPrompt(
      prompt,
      undefined,
      mergedReferences.length > 0 ? mergedReferences : undefined,
    );
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
    }
  }

  function scrollToBottom() {
    const scrollContainer = chatScrollRef.current;
    if (!scrollContainer) return;
    scrollContainer.scrollTo({ top: scrollContainer.scrollHeight, behavior: "smooth" });
  }

  function handleChatScroll() {
    const scrollContainer = chatScrollRef.current;
    if (scrollContainer) syncBottomState(scrollContainer);
  }


  function handleComposerKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    const isComposing = event.nativeEvent.isComposing;
    const fileAction = fileKeyAction({ key: event.key, shiftKey: event.shiftKey, isComposing, menuVisible: fileMenuVisible });
    if (fileAction.type === "move") { event.preventDefault(); setActiveFileIndex((current) => nextCommandIndex(current, fileAction.direction, fileCandidates.length)); return; }
    if (fileAction.type === "dismiss") { event.preventDefault(); setFileMenuDismissedFor(input); return; }
    if (fileAction.type === "complete") { event.preventDefault(); completeFile(); return; }
    const action = commandKeyAction({ key: event.key, shiftKey: event.shiftKey, isComposing, menuVisible: commandMenuVisible });
    if (action.type === "move") { event.preventDefault(); setActiveCommandIndex((current) => nextCommandIndex(current, action.direction, filteredCommands.length)); return; }
    if (action.type === "dismiss") { event.preventDefault(); setCommandMenuDismissedFor(input); return; }
    if (action.type === "complete") { event.preventDefault(); completeCommand(); return; }
    if (action.type === "send") { event.preventDefault(); void send(); }
  }

  return (
    <div ref={chatPageRef} className={`chat-page${compact ? " chat-page--compact" : ""}`}>
      {hasTurnTree && currentThreadId ? <div className="trace-toolbar" role="navigation" aria-label="主内容视图">
        <Dropdown
          trigger={["click"]}
          menu={{
            selectable: true,
            selectedKeys: [currentThreadId],
            items: [{ key: currentThreadId, label: currentThreadId }],
          }}
        >
          <Tooltip title={compact ? `Thread：${currentThreadId}` : undefined}>
            <Button type="text" aria-label="Thread" icon={compact ? <BranchesOutlined /> : undefined}>{compact ? null : "Thread"}</Button>
          </Tooltip>
        </Dropdown>
        <span className="trace-toolbar-thread-id" title={currentThreadId}>{currentThreadId}</span>
        <Tooltip title={compact ? "Chat" : undefined}>
          <Button type="text" aria-label="Chat" icon={compact ? <CommentOutlined /> : undefined} aria-pressed={visibleMainView === "chat"} onClick={() => setMainView("chat")}>{compact ? null : "Chat"}</Button>
        </Tooltip>
        <Tooltip title={compact ? "Trace" : undefined}>
          <Button type="text" aria-label="Trace" icon={compact ? <NodeIndexOutlined /> : undefined} aria-pressed={visibleMainView === "trace"} onClick={() => setMainView("trace")}>{compact ? null : "Trace"}</Button>
        </Tooltip>
      </div> : null}
      {visibleMainView === "trace" ? <TracePage key={currentThreadId} turns={traceTurns} /> : <>
      <div className="chat-content">
        <ChatMessageList
          messages={messages}
          sessionId={conversation?.sessionId}
          display={display}
          interactionBusy={interactionBusy}
          isMobile={isMobile}
          compactionPending={compactionPending}
          chatScrollRef={chatScrollRef}
          onScroll={handleChatScroll}
          editingMessageId={editingMessageId}
          editingDraft={editingDraft}
          editRef={editRef}
          rewindPending={rewindPending}
          editingSubmitting={editingSubmitting}
          canEdit={Boolean(onRewind)}
          setEditingDraft={setEditingDraft}
          cancelEdit={cancelEdit}
          saveEdit={saveEdit}
          beginEdit={beginEdit}
          handleUserBubbleClick={handleUserBubbleClick}
          messageVersion={messageVersion}
          changeMessageVersion={changeMessageVersion}
          onDecision={chooseDecision}
          onFork={onFork ? forkMessage : undefined}
          sandboxFailure={sandboxHealth.phase === "unhealthy" ? sandboxHealth.detail ?? "健康检查未通过。" : null}
        />
        {!isAtBottom ? (
          <FloatButton
            className="chat-scroll-bottom-button"
            icon={<VerticalAlignBottomOutlined />}
            tooltip="滚动到底部"
            aria-label="滚动到底部"
            onClick={scrollToBottom}
          />
        ) : null}
      </div>
      <Composer
        input={input}
        busy={interactionBusy}
        compact={compact}
        filteredCommands={filteredCommands}
        commandMenuVisible={commandMenuVisible}
        activeCommandIndex={activeCommandIndex}
        mode={mode}
        permissionMode={permissionMode}
        reasoningEffort={reasoningEffort}
        modePending={runtimeConfigPending.mode}
        permissionPending={runtimeConfigPending.permission}
        reasoningPending={runtimeConfigPending.reasoning}
        todos={visibleTodo}
        todoClosable={todoClosable}
        onTodoClose={closeTodoPanel}
        usagePercent={usagePercent}
        usageTotalTokens={activeUsage?.total ?? null}
        usageContextLength={activeUsage?.context}
        openSettingsSelect={openSettingsSelect}
        editorRef={editorRef}
        onEditorChange={handleEditorChange}
        onKeyDown={handleComposerKeyDown}
        onComplete={completeCommand}
        onActiveCommandChange={setActiveCommandIndex}
        onModeChange={(value) => void changeRunningMode(value)}
        onPermissionChange={(value) => void changePermissionMode(value)}
        onReasoningChange={(value) => void changeReasoningEffort(value)}
        onSettingsSelectChange={setOpenSettingsSelect}
        onStop={pauseOrSteer}
        onSend={() => void send()}
        actionMode={actionMode}
        submitDisabled={sandboxBlocked || projectUnavailable || compactionPending || composerActionState.disabled}
        disabled={sandboxBlocked || projectUnavailable || compactionPending}
        disabledReason={sandboxBlocked
          ? sandboxHealth.phase === "checking" ? "正在检查沙箱 Broker" : "沙箱 Broker 不可用"
          : conversation?.projectAvailable === false ? "项目 cwd 不可用，恢复文件夹后才能运行" : undefined}
        fileCandidates={fileCandidates}
        fileMenuVisible={fileMenuVisible}
        activeFileIndex={activeFileIndex}
        fileMenuQuery={fileTriggerState?.query ?? ""}
        onFileComplete={completeFile}
        onActiveFileChange={setActiveFileIndex}
        onPickFiles={handlePickFiles}
        sessionId={conversation?.sessionId}
        pendingUploads={pendingUploads}
        uploadsUploading={pendingUploads.some((upload) => upload.status === "uploading")}
        queuedMessages={queuedMessages}
        onQueueSend={sendQueuedMessage}
        onQueueEdit={editQueuedMessage}
        onQueueDelete={(item) => {
          if (item.state !== "pending" || !conversation?.threadId) return;
          void deleteQueuedMessage(conversation.threadId, item.id)
            .then(() => updateQueue((items) => items.filter((candidate) => candidate.id !== item.id)))
            .catch((error) => setLast({ error: String((error as Error).message ?? error) }));
        }}
        onRemoveUpload={removePendingUpload}
        onRetryUpload={retryUpload}
        onUploadPreview={(index) => {
          const upload = pendingUploads[index];
          if (upload?.path && conversation?.sessionId) {
            window.open(sessionFileContentUrl(conversation.sessionId, "upload", upload.path), "_blank", "noopener");
          }
        }}
      />
      </>}
    </div>
  );
}
