import { useCallback, useEffect, useMemo, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { App as AntApp, FloatButton } from "antd";
import { VerticalAlignBottomOutlined } from "@ant-design/icons";
import { sessionFileContentUrl, submitDecision } from "../../api";
import { parseCommand } from "../../commands";
import { commandKeyAction, commandSuggestions, nextCommandIndex } from "../../commands/completion";
import { fileKeyAction } from "../../commands/fileCompletion";
import Composer from "./Composer";
import { latestTodoList } from "./todoPanel";
import { messagesBeforeRewind, projectTurnPath, pruneTurnDescendants } from "../../app/runtime/runtimeDetailProjection";
import { leafNodes } from "../../app/runtime/runtimeNodeReducer";
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
import { useAgentThreadView } from "./useAgentThreadView";
import { ChatToolbar, type ChatMainView } from "./ChatToolbar";
import { useChatCommands } from "./useChatCommands";
import { useChatScroll } from "./useChatScroll";
import { useQueuedMessageFlow } from "./useQueuedMessageFlow";
import { useResponsiveChatLayout } from "./useResponsiveChatLayout";

export { composerAction } from "./contracts";
export { CHAT_COMPACT_WIDTH } from "./useResponsiveChatLayout";

export default function ChatPage({
  conversation: canonicalConversation,
  agentThreadNavigation = false,
  displayMode: configuredDisplayMode,
  providerConfig,
  mode: selectedMode,
  onModeChange = () => undefined,
  onUpdate,
  onNew,
  onNavigate,
  onEnsureSession = async (id) => canonicalConversation?.sessionId ?? id,
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
  const agentThreadView = useAgentThreadView({
    canonical: canonicalConversation,
    enabled: agentThreadNavigation,
    onUpdate,
  });
  const conversation = agentThreadView.conversation;
  const [agentModeByThread, setAgentModeByThread] = useState<Record<string, ChatMode>>({});
  const mode = agentThreadView.isSubagent && agentThreadView.selectedThreadId
    ? agentModeByThread[agentThreadView.selectedThreadId] ?? selectedMode ?? "agent"
    : selectedMode ?? "agent";
  const changeViewMode = useCallback((value: ChatMode) => {
    if (agentThreadView.isSubagent && agentThreadView.selectedThreadId) {
      setAgentModeByThread((current) => ({ ...current, [agentThreadView.selectedThreadId!]: value }));
      return;
    }
    onModeChange(value);
  }, [agentThreadView.isSubagent, agentThreadView.selectedThreadId, onModeChange]);
  const { chatPageRef, compact, isMobile } = useResponsiveChatLayout();
  const [queueSubmitting, setQueueSubmitting] = useState(false);
  const [compactionPending, setCompactionPending] = useState(false);
  const [activeCommandIndex, setActiveCommandIndex] = useState(0);
  const [commandMenuDismissedFor, setCommandMenuDismissedFor] = useState<string | null>(null);
  const [dismissedTodoPanels, setDismissedTodoPanels] = useState<Set<string>>(() => new Set());
  const [mainView, setMainView] = useState<ChatMainView>("chat");

  const messages = conversation?.messages ?? [];
  const { chatScrollRef, handleScroll: handleChatScroll, isAtBottom, scrollToBottom } = useChatScroll(
    conversation?.id,
    messages,
  );
  // A queue flush has no optimistic assistant message by design. Keep the
  // composer in its running interaction mode from the moment the flush
  // request is sent until its SSE cleanup, including the tiny interval
  // between the optimistic user bubble and the first turn.snapshot frame.
  const busy = agentThreadView.isSubagent ? false : Boolean(runningProp) || queueSubmitting;
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
  const filteredCommands = commandSuggestions(input).filter(
    (command) => !agentThreadView.isSubagent || command.name !== "/compact",
  );
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
    onModeChange: changeViewMode,
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
    onRewind: agentThreadView.isSubagent ? undefined : onRewind,
    onFork: agentThreadView.isSubagent ? undefined : onFork,
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
    agentThreadView.isSubagent ? undefined : activeRuntimeNode?.status,
    hasDraft,
    pendingUploads.some((upload) => upload.status === "uploading"),
  );
  const actionMode = agentThreadView.isSubagent ? "send" : composerActionState.mode;
  const projectUnavailable = conversation?.projectId !== undefined && conversation.projectAvailable === false;
  const chatCommands = useChatCommands({
    activeCommandIndex,
    filteredCommands,
    editorRef,
    setActiveCommandIndex,
    setCommandMenuDismissedFor,
    setCompactionPending,
    setMainView,
    compactionPending,
    isSubagent: agentThreadView.isSubagent,
    conversation,
    activeRuntimeNode,
    clearComposer,
    onInsert: insert,
    onNew,
    onReload,
    onInfo: (content) => void message.info(content),
  });
  const queuedMessageFlow = useQueuedMessageFlow({
    conversation,
    activeRuntimeNode,
    queuedMessages,
    queueSubmitting,
    sandboxBlocked,
    isSubagent: agentThreadView.isSubagent,
    input,
    collectedReferences,
    clearComposer,
    editorRef,
    setInput,
    setReferences,
    setPendingUploads,
    setQueueSubmitting,
    onQueuedMessagesChange,
    onQueuedMessagesRefresh,
    onSetLast: setLast,
    onDispatch: ({ conversationId, sessionId, sourceNodeId, deliveryId, messageIds, onBaseline }) => dispatchRun(
      conversationId,
      sessionId,
      null,
      false,
      sourceNodeId,
      undefined,
      undefined,
      true,
      onBaseline,
      { deliveryId, messageIds },
    ),
    onStop: stop,
    onWarning: (content) => void message.warning(content),
  });

  useEffect(() => {
    if (agentThreadView.streamError) void message.error(`Subagent 实时流重连中：${agentThreadView.streamError}`);
  }, [agentThreadView.streamError, message]);

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
      await chatCommands.executeCommand(command.name, command.argument);
      return;
    }
    if (agentThreadView.isSubagent) {
      try {
        await agentThreadView.sendMessage({
          content: prompt,
          references: mergedReferences.length > 0 ? mergedReferences : undefined,
          mode,
          permissionMode,
          providerName: requestProviderName,
          model: requestModel,
        });
        clearComposer();
        setPendingUploads([]);
      } catch (error) {
        void message.error(`Agent 消息发送失败：${String((error as Error).message ?? error)}`);
      }
      return;
    }
    if (activeRuntimeNode?.status === "running") {
      await queuedMessageFlow.queueCurrentPrompt(prompt, mergedReferences);
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

  function handleComposerKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    const isComposing = event.nativeEvent.isComposing;
    const fileAction = fileKeyAction({ key: event.key, shiftKey: event.shiftKey, isComposing, menuVisible: fileMenuVisible });
    if (fileAction.type === "move") { event.preventDefault(); setActiveFileIndex((current) => nextCommandIndex(current, fileAction.direction, fileCandidates.length)); return; }
    if (fileAction.type === "dismiss") { event.preventDefault(); setFileMenuDismissedFor(input); return; }
    if (fileAction.type === "complete") { event.preventDefault(); completeFile(); return; }
    const action = commandKeyAction({ key: event.key, shiftKey: event.shiftKey, isComposing, menuVisible: commandMenuVisible });
    if (action.type === "move") { event.preventDefault(); setActiveCommandIndex((current) => nextCommandIndex(current, action.direction, filteredCommands.length)); return; }
    if (action.type === "dismiss") { event.preventDefault(); setCommandMenuDismissedFor(input); return; }
    if (action.type === "complete") { event.preventDefault(); chatCommands.completeCommand(); return; }
    if (action.type === "send") { event.preventDefault(); void send(); }
  }

  return (
    <div ref={chatPageRef} className={`chat-page${compact ? " chat-page--compact" : ""}`}>
      {currentThreadId ? (
        <ChatToolbar
          visible={hasTurnTree || Boolean(agentThreadNavigation && conversation?.sessionId)}
          currentThreadId={currentThreadId}
          compact={compact}
          mainView={visibleMainView}
          agentThread={agentThreadNavigation && conversation?.sessionId && agentThreadView.rootThreadId && agentThreadView.selectedThreadId ? {
            sessionId: conversation.sessionId,
            rootThreadId: agentThreadView.rootThreadId,
            selectedThreadId: agentThreadView.selectedThreadId,
            invalidation: agentThreadView.treeInvalidation,
            onSelect: agentThreadView.selectThread,
          } : undefined}
          onMainViewChange={setMainView}
        />
      ) : null}
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
          canEdit={!agentThreadView.isSubagent && Boolean(onRewind)}
          setEditingDraft={setEditingDraft}
          cancelEdit={cancelEdit}
          saveEdit={saveEdit}
          beginEdit={beginEdit}
          handleUserBubbleClick={handleUserBubbleClick}
          messageVersion={messageVersion}
          changeMessageVersion={changeMessageVersion}
          onDecision={chooseDecision}
          onFork={!agentThreadView.isSubagent && onFork ? forkMessage : undefined}
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
        onComplete={chatCommands.completeCommand}
        onActiveCommandChange={setActiveCommandIndex}
        onModeChange={(value) => void changeRunningMode(value)}
        onPermissionChange={(value) => void changePermissionMode(value)}
        onReasoningChange={(value) => void changeReasoningEffort(value)}
        onSettingsSelectChange={setOpenSettingsSelect}
        onStop={queuedMessageFlow.pauseOrSteer}
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
        queuedMessages={agentThreadView.isSubagent ? [] : queuedMessages}
        onQueueSend={agentThreadView.isSubagent ? undefined : queuedMessageFlow.sendQueuedMessage}
        onQueueEdit={agentThreadView.isSubagent ? undefined : queuedMessageFlow.editQueuedMessage}
        onQueueDelete={agentThreadView.isSubagent ? undefined : queuedMessageFlow.deleteQueuedMessage}
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
