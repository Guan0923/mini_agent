import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent } from "react";
import { Button, FloatButton, Grid, Input, Modal } from "antd";
import { VerticalAlignBottomOutlined } from "@ant-design/icons";
import type { TextAreaRef } from "antd/es/input/TextArea";
import {
  compactSession,
  cancelJob,
  deleteSessionFile,
  listSkills,
  patchRuntimeConfig,
  searchSessionFiles,
  sessionFileContentUrl,
  streamChat,
  streamChatBatch,
  streamResume,
  submitDecision,
  uploadSessionFiles,
} from "../../api";
import type { RagMode } from "../../api/chat";
import type { ProviderConfig } from "../../api";
import { HELP_TEXT, parseCommand } from "../../commands";
import { commandKeyAction, commandSuggestions, completionText, nextCommandIndex } from "../../commands/completion";
import { completionToken, fileKeyAction, toCandidates, type FileCandidate, type FileTrigger } from "../../commands/fileCompletion";
import MarkdownContent from "../../components/MarkdownContent";
import { AssistantMessage, MessageActions, MessageReferenceChip } from "./messageParts";
import Composer, { type ComposerActionMode, type SettingsSelectKey } from "./Composer";
import type { FileMentionChange, FileMentionEditorHandle } from "./FileMentionEditor";
import ConversationTimeline, { conversationTurnId } from "./ConversationTimeline";
import { latestTodoList } from "./todoPanel";
import { appendLegacyRuntimeEvent, integrateRuntimeNodeFrame, projectRuntimeNode } from "../../app/runtimeDetailProjection";
import { leafNodes } from "../../app/runtimeNodeReducer";
import { applyRunSegment } from "../../app/runSegmentReducer";
import type { QueuedMessage } from "../../app/types";
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
  ragEnabled?: boolean;
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
  queuedMessages?: QueuedMessage[];
  onQueuedMessagesChange?: (conversationId: string, updater: (items: QueuedMessage[]) => QueuedMessage[]) => void;
}

interface RewindResult {
  content: string;
  sessionId: string;
  sourceNodeId?: string;
  sourceNodeSessionId?: string;
  branch?: boolean;
}

/** One file being uploaded or already stored in the session uploads. */
interface PendingUpload {
  uid: string;
  name: string;
  isImage: boolean;
  status: "uploading" | "done" | "error";
  percent: number;
  /** The original file, kept for retries. */
  file?: File;
  /** Server path once the upload completed. */
  path?: string;
  error?: string;
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
  sourceNodeSessionId?: string;
  branch?: boolean;
  references?: FileReference[];
  batchMessages?: Array<{ content: string; references?: FileReference[] }>;
  ragMode?: RagMode;
}

function nativeTextArea(ref: TextAreaRef | null): HTMLTextAreaElement | null {
  return ref?.resizableTextArea?.textArea ?? null;
}

export default function ChatPage({
  conversation,
  displayMode: configuredDisplayMode,
  providerConfig,
  ragEnabled = false,
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
}: Props) {
  const mode = selectedMode ?? "agent";
  const enhancedChatOptions = selectedMode !== undefined;
  const screens = Grid.useBreakpoint();
  const isMobile = screens.md === false && (typeof window === "undefined" || window.innerWidth < 768);
  const [input, setInput] = useState("");
  const [localBusy, setLocalBusy] = useState(false);
  const [batchSubmitting, setBatchSubmitting] = useState(false);
  const [permissionMode, setPermissionMode] = useState<PermissionMode>("read_only");
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort>("medium");
  const ragMode: RagMode = ragEnabled ? "tool" : "off";
  const [providerName, setProviderName] = useState("unknown");
  const [runtimeModel, setRuntimeModel] = useState<RuntimeNodeModel>(DEFAULT_RUNTIME_NODE_MODEL);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [openSettingsSelect, setOpenSettingsSelect] = useState<SettingsSelectKey | null>(null);
  const [activeCommandIndex, setActiveCommandIndex] = useState(0);
  const [commandMenuDismissedFor, setCommandMenuDismissedFor] = useState<string | null>(null);
  const [editingMessageId, setEditingMessageId] = useState<string | null>(null);
  const [editingDraft, setEditingDraft] = useState("");
  const [rewindPending, setRewindPending] = useState(false);
  const [editingSubmitting, setEditingSubmitting] = useState(false);
  const editingSubmittingRef = useRef(false);
  const [references, setReferences] = useState<FileReference[]>([]);
  const [fileTriggerState, setFileTriggerState] = useState<FileTrigger | null>(null);
  const [fileCandidates, setFileCandidates] = useState<FileCandidate[]>([]);
  const [activeFileIndex, setActiveFileIndex] = useState(0);
  const [fileMenuDismissedFor, setFileMenuDismissedFor] = useState<string | null>(null);
  const [pendingUploads, setPendingUploads] = useState<PendingUpload[]>([]);
  const fileSearchTimerRef = useRef<number | null>(null);
  const latestFileTriggerRef = useRef<FileTrigger | null>(null);
  const fileMenuDismissedPromptRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const activeStreamRef = useRef<{
    controller: AbortController;
    jobId?: string;
    stopRequested: boolean;
    cancelIssued: boolean;
    cancelTimer?: number;
  } | null>(null);
  const pendingRewindRef = useRef<{
    conversationId: string;
    sessionId: string;
    sourceNodeId?: string;
    sourceNodeSessionId?: string;
    branch: boolean;
  } | null>(null);
  const editorRef = useRef<FileMentionEditorHandle>(null);
  const editRef = useRef<TextAreaRef>(null);
  const fullAccessConfirmRef = useRef<{ destroy: () => void } | null>(null);
  const chatScrollRef = useRef<HTMLDivElement | null>(null);
  const discardedUploadUidsRef = useRef(new Set<string>());
  const batchFlushRef = useRef(false);
  // IDs captured when a batch request starts.  Items added while that batch
  // is running belong to the next FIFO batch and must never be removed when
  // the submitted user frames are acknowledged.
  const batchInFlightIdsRef = useRef<Set<string> | null>(null);
  const batchSourceKeyRef = useRef<string | null>(null);
  const batchExpectedUserCountRef = useRef(0);
  const batchKnownUserKeysRef = useRef<Set<string>>(new Set());
  const canonicalRunningRef = useRef(false);

  const messages = conversation?.messages ?? [];
  // A batch has no optimistic assistant message by design.  Keep the
  // composer in its running interaction mode from the moment the batch
  // request is sent until its SSE cleanup, including the tiny interval
  // between the user frames and the assistant node.create frame.
  const busy = (runningProp ?? localBusy) || batchSubmitting;
  const todo = useMemo(() => latestTodoList(messages), [messages]);
  const filteredCommands = commandSuggestions(input);
  const commandMenuVisible = !busy && commandMenuDismissedFor !== input && filteredCommands.length > 0;
  // The file menu is mutually exclusive with the slash-command menu and only
  // appears while the caret still sits inside an `@` trigger.
  const fileMenuVisible = !busy && fileMenuDismissedFor !== input && fileTriggerState !== null && fileCandidates.length > 0;
  const display = configuredDisplayMode ?? "medium";

  const activeRuntimeNode = (() => {
    const nodes = conversation?.runtimeNodes ?? [];
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
  const recoveryMode = !busy && activeRuntimeNode?.status === "cancel";
  const hasDraft = Boolean(input.trim() || references.length > 0 || pendingUploads.some((upload) => upload.status === "done"));
  const actionMode: ComposerActionMode = ((activeRuntimeNode?.status === "running" && !hasDraft) || (!activeRuntimeNode && busy && !hasDraft))
    ? "pause"
    : activeRuntimeNode?.status === "running" || (!activeRuntimeNode && busy)
      ? "send"
      : activeRuntimeNode?.status === "cancel" && !input.trim() && references.length === 0 && pendingUploads.every((upload) => upload.status !== "done")
        ? "resume"
        : activeRuntimeNode?.status === "cancel"
          ? "send"
          : "send";
  const projectUnavailable = conversation?.projectId !== undefined && conversation.projectAvailable === false;
  const canPatchRuntimeConfig = activeRuntimeNode?.status === "running";

  useEffect(() => {
    const node = activeRuntimeNode;
    const status = node?.status;
    if (batchFlushRef.current && batchExpectedUserCountRef.current > 0) {
      const known = batchKnownUserKeysRef.current;
      const submittedUserCount = (conversation?.runtimeNodes ?? []).filter((candidate) => {
        const message = candidate.data.type === "message"
          ? candidate.data.message as { role?: unknown } | undefined
          : undefined;
        return message?.role === "user"
          && candidate.status === "success"
          && !known.has(`${candidate.session_id}:${candidate.id}`);
      }).length;
      if (submittedUserCount >= batchExpectedUserCountRef.current) {
        const submitted = batchInFlightIdsRef.current;
        if (submitted && submitted.size > 0) {
          onQueuedMessagesChange(conversation?.id ?? "", (items) => items.filter((item) => !submitted.has(item.id)));
        }
        batchInFlightIdsRef.current = null;
        batchExpectedUserCountRef.current = 0;
        batchKnownUserKeysRef.current = new Set();
        batchSourceKeyRef.current = null;
      }
    }
    if (status === "running") {
      canonicalRunningRef.current = true;
      if (
        batchFlushRef.current
        && node
        && node.data.type === "message"
        && (node.data.message as { role?: unknown } | undefined)?.role === "assistant"
        && `${node.session_id}:${node.id}` !== batchSourceKeyRef.current
      ) {
        const submitted = batchInFlightIdsRef.current;
        if (submitted && submitted.size > 0) {
          onQueuedMessagesChange(conversation?.id ?? "", (items) => items.filter((item) => !submitted.has(item.id)));
          batchInFlightIdsRef.current = null;
          batchSourceKeyRef.current = null;
        }
      }
      return;
    }
    if (
      canonicalRunningRef.current
      && !batchFlushRef.current
      && queuedMessages.length > 0
      && conversation?.id
      && (status === "success" || status === "cancel" || status === "abort")
    ) {
      canonicalRunningRef.current = false;
      batchFlushRef.current = true;
      setBatchSubmitting(true);
      void flushQueuedMessages();
    }
  }, [activeRuntimeNode?.id, activeRuntimeNode?.status, busy, queuedMessages.length, conversation?.id]);

  useEffect(() => {
    const node = activeRuntimeNode;
    if (!node) return;
    const model = normalizeRuntimeNodeModel(node.model);
    setProviderName(node.provider_name || "unknown");
    setRuntimeModel(model);
    setPermissionMode(node.permission_mode === "approval_for_me" ? "read_only" : (node.permission_mode || "read_only"));
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
    full_access_acknowledged?: boolean;
    running_mode?: ChatMode;
  }) {
    if (!conversation?.sessionId || !activeRuntimeNode || activeRuntimeNode.status !== "running") return;
    try {
      await patchRuntimeConfig(conversation.sessionId, {
        node_id: activeRuntimeNode.id,
        provider_name: patch.provider_name,
        model: patch.model,
        permission_mode: patch.permission_mode,
        full_access_acknowledged: patch.full_access_acknowledged,
        running_mode: patch.running_mode,
      });
    } catch (error) {
      setLast({ error: `运行配置更新失败：${String((error as Error).message ?? error)}` });
    }
  }

  useEffect(() => {
    if (!busy || !canPatchRuntimeConfig || !activeRuntimeNode || !configuredProviderName || !requestModel) return;
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
    canPatchRuntimeConfig,
    activeRuntimeNode?.id,
    activeRuntimeNode?.status,
    activeRuntimeNode?.provider_name,
    activeRuntimeModel.current_model,
    activeRuntimeModel.context_length,
    activeRuntimeModel.output_length,
    configuredProviderName,
    requestModel?.current_model,
    requestModel?.context_length,
    requestModel?.output_length,
  ]);

  useEffect(() => () => abortRef.current?.abort(), []);

  useEffect(() => () => {
    // Static antd confirmations are mounted outside ChatPage.  Tie the
    // confirmation lifetime back to this page so a conversation switch or
    // unmount cannot leave an orphaned modal blocking the next interaction.
    fullAccessConfirmRef.current?.destroy();
    fullAccessConfirmRef.current = null;
  }, []);

  useEffect(() => {
    for (const upload of pendingUploads) {
      if (upload.status === "uploading") discardedUploadUidsRef.current.add(upload.uid);
    }
    editorRef.current?.clear();
    setInput("");
    setReferences([]);
    setFileTriggerState(null);
    setFileCandidates([]);
    fileMenuDismissedPromptRef.current = null;
    setFileMenuDismissedFor(null);
    setPendingUploads([]);
    pendingRewindRef.current = null;
    editingSubmittingRef.current = false;
    setEditingSubmitting(false);
    // Upload callbacks use the uid set above to delete files that finish after
    // the composer has moved to another conversation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversation?.id]);

  useEffect(() => {
    if (editingMessageId) {
      editRef.current?.focus();
      nativeTextArea(editRef.current)?.select();
    }
  }, [editingMessageId]);

  function handleEditorChange(change: FileMentionChange) {
    const { prompt: value, references: inlineReferences, trigger } = change;
    const dismissedPrompt = fileMenuDismissedPromptRef.current;
    const preserveDismissedMenu = dismissedPrompt === value;
    if (!preserveDismissedMenu && dismissedPrompt !== null) fileMenuDismissedPromptRef.current = null;
    setInput(value);
    if (value !== input) pendingRewindRef.current = null;
    setReferences(inlineReferences);
    setCommandMenuDismissedFor(null);
    setActiveCommandIndex(0);
    setFileTriggerState(trigger);
    latestFileTriggerRef.current = trigger;
    if (!trigger) {
      if (fileSearchTimerRef.current !== null) {
        window.clearTimeout(fileSearchTimerRef.current);
        fileSearchTimerRef.current = null;
      }
      setFileCandidates([]);
      setFileMenuDismissedFor(null);
      return;
    }
    setFileMenuDismissedFor(preserveDismissedMenu ? value : null);
    setActiveFileIndex(0);
    if (fileSearchTimerRef.current !== null) {
      window.clearTimeout(fileSearchTimerRef.current);
    }
    fileSearchTimerRef.current = window.setTimeout(() => {
      fileSearchTimerRef.current = null;
      if (latestFileTriggerRef.current?.query !== trigger.query || latestFileTriggerRef.current?.start !== trigger.start) return;
      void searchFiles(trigger);
    }, 250);
  }

  async function searchFiles(trigger: FileTrigger) {
    if (!conversation?.sessionId) return;
    try {
      const results = await searchSessionFiles(conversation.sessionId, trigger.query, 20);
      setFileCandidates(toCandidates(results));
    } catch {
      setFileCandidates([]);
    }
  }

  function completeFile(index = activeFileIndex) {
    const candidate = fileCandidates[index];
    if (!candidate || !fileTriggerState) return;
    const token = completionToken(candidate.reference.path);
    const completedPrompt = `${input.slice(0, fileTriggerState.start)}${token}${input.slice(fileTriggerState.end)}`;
    fileMenuDismissedPromptRef.current = completedPrompt;
    setFileMenuDismissedFor(completedPrompt);
    setFileCandidates([]);
    editorRef.current?.replaceCurrentMention(candidate.reference, candidate.label);
  }

  function handlePickFiles(files: FileList | File[]) {
    if (!conversation?.sessionId) return;
    const selected = Array.from(files).filter((file) => file.size > 0);
    if (selected.length === 0) return;
    const sessionId = conversation.sessionId;
    const uploads: PendingUpload[] = selected.map((file) => ({
      uid: crypto.randomUUID(),
      name: file.name,
      isImage: file.type.startsWith("image/"),
      status: "uploading",
      percent: 0,
      file,
    }));
    setPendingUploads((current) => [...current, ...uploads]);
    void uploadSessionFiles(sessionId, selected, (percent) => {
      setPendingUploads((current) => current.map((item) => uploads.some((upload) => upload.uid === item.uid) && item.status === "uploading" ? { ...item, percent } : item));
    }).then((results) => {
      const discardedPaths: string[] = [];
      setPendingUploads((current) => {
        const next = [...current];
        results.forEach((result, index) => {
          const upload = uploads[index];
          if (!upload) return;
          const position = next.findIndex((item) => item.uid === upload.uid);
          if (position === -1 || discardedUploadUidsRef.current.has(upload.uid)) {
            discardedUploadUidsRef.current.delete(upload.uid);
            discardedPaths.push(result.path);
            return;
          }
          next[position] = {
            ...next[position],
            status: "done",
            percent: 100,
            path: result.path,
          };
        });
        return next;
      });
      for (const path of discardedPaths) void deleteSessionFile(sessionId, "upload", path).catch(() => undefined);
    }).catch((error) => {
      const message = String((error as Error).message ?? error);
      setPendingUploads((current) => current.map((item) => uploads.some((upload) => upload.uid === item.uid) ? { ...item, status: "error", error: message } : item));
    });
  }

  function removePendingUpload(index: number) {
    const upload = pendingUploads[index];
    if (!upload) return;
    if (upload.status === "uploading") {
      discardedUploadUidsRef.current.add(upload.uid);
    }
    if (upload.status === "done" && upload.path && conversation?.sessionId) {
      // Removing an upload deletes the server file. Inline mentions remain
      // independent and may intentionally become unavailable.
      void deleteSessionFile(conversation.sessionId, "upload", upload.path).catch(() => undefined);
    }
    setPendingUploads((current) => current.filter((_item, itemIndex) => itemIndex !== index));
  }

  function retryUpload(index: number) {
    const upload = pendingUploads[index];
    if (!upload || !conversation?.sessionId || !upload.file) return;
    setPendingUploads((current) => current.filter((_item, itemIndex) => itemIndex !== index));
    handlePickFiles([upload.file]);
  }

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

  function appendDelta(content: string, conversationId?: string) {
    updateLast((message) => ({ ...message, content: message.content + content }), conversationId);
  }

  function setLast(fields: Partial<ChatMessage>, conversationId?: string) {
    updateLast((message) => ({ ...message, ...fields }), conversationId);
  }

  function defaultSourceNodeId(): string | undefined {
    return conversation?.lastNodeId ?? conversation?.forkAnchorNodeId;
  }

  function defaultSourceNodeSessionId(): string | undefined {
    if (conversation?.forkAnchorNodeId) return conversation.forkAnchorSessionId;
    return [...messages].reverse().find((message) => message.role === "assistant")?.nodeSessionId;
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
    sourceNodeSessionId?: string | null,
    references?: FileReference[],
    branch = false,
    batchMessages?: QueuedMessage[],
  ) {
    const controller = new AbortController();
    abortRef.current = controller;
    const activeStream: NonNullable<typeof activeStreamRef.current> = { controller, stopRequested: false, cancelIssued: false };
    activeStreamRef.current = activeStream;
    setLocalBusy(true);
    try {
      let nodeProtocol = false;
      let sawDone = false;
      let finalNode: import("../../types").RuntimeStateNode | undefined;
      const cancelStreamJob = (jobId: string) => {
        if (activeStream.cancelIssued) return;
        activeStream.cancelIssued = true;
        // Keep the reader alive until the backend emits the canonical
        // terminal node.delete frame for status=cancel.
        void cancelJob(jobId).catch(() => undefined);
      };
      const onMessage = (message: StreamMessage) => {
        if (message.type === "job" && message.job_id) {
          activeStream.jobId = message.job_id;
          if (activeStream.cancelTimer !== undefined && !activeStream.stopRequested) {
            window.clearTimeout(activeStream.cancelTimer);
            activeStream.cancelTimer = undefined;
          }
          if (activeStream.stopRequested) cancelStreamJob(message.job_id);
          return;
        }
        if (message.type === "run_segment") {
          if (activeStream.stopRequested || controller.signal.aborted) return;
          if (message.segment) updateLast((item) => applyRunSegment(item, message.segment!), conversationId);
          const runId = message.run_id ?? (typeof message.data?.run_id === "string" ? message.data.run_id : undefined);
          if (runId) setLast({ runId }, conversationId);
        } else if (message.type === "event") {
          if (activeStream.stopRequested || controller.signal.aborted) return;
          const kind = message.kind ?? "";
          if (kind === "response_delta" && !nodeProtocol) {
            const content = (message.data?.content as string | undefined) ?? message.message ?? "";
            if (content) appendDelta(content, conversationId);
          } else if (kind.startsWith("thinking_") || kind === "tool_call" || kind === "tool_result" || kind === "tool_failed") {
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
          if (activeStream.stopRequested || controller.signal.aborted) return;
          sawDone = true;
          setLast({
            content: message.final_answer ?? "",
            status: message.status === "cancelled" ? "cancel" : message.status,
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
          if (activeStream.stopRequested || controller.signal.aborted) return;
          setLast({ error: message.error ?? message.message ?? "发生错误", running: false, decision: undefined }, conversationId);
        }
      };
      if (resume) {
        const resumeSourceNodeId = sourceNodeId === undefined ? defaultSourceNodeId() : sourceNodeId ?? undefined;
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
              ragMode,
              permissionMode === "full_access",
              sourceNodeSessionId ?? undefined,
            )
          : await streamResume(
              sessionId,
              onMessage,
              controller.signal,
              permissionMode,
              reasoningEffort,
              undefined,
              requestProviderName,
              requestModel,
              mode,
              ragMode,
              permissionMode === "full_access",
              sourceNodeSessionId ?? undefined,
            );
        if (result === "aborted") setLast({
          running: false,
          status: "cancel",
          error: undefined,
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
        const chatSourceNodeId = sourceNodeId === undefined ? defaultSourceNodeId() : sourceNodeId ?? undefined;
        const options = chatSourceNodeId
          ? (enhancedChatOptions
            ? { sessionId, mode, permissionMode, fullAccessAcknowledged: permissionMode === "full_access", reasoningEffort, providerName: requestProviderName, model: requestModel, sourceNodeId: chatSourceNodeId, sourceNodeSessionId: sourceNodeSessionId ?? undefined, branch, references, ragMode }
            : { sessionId, sourceNodeId: chatSourceNodeId, sourceNodeSessionId: sourceNodeSessionId ?? undefined, branch, providerName: requestProviderName, model: requestModel, mode, permissionMode, fullAccessAcknowledged: permissionMode === "full_access", reasoningEffort, references, ragMode })
          // An empty tree has no dynamic runtime configuration to submit. Keep
          // the stable positional call for clients embedding ChatPage while
          // all established sessions use the explicit v0.3 config object.
          : (enhancedChatOptions
            ? { sessionId, mode, permissionMode, fullAccessAcknowledged: permissionMode === "full_access", reasoningEffort, providerName: requestProviderName, model: requestModel, references, ragMode }
            : (ragMode !== "off" ? { sessionId, ragMode } : sessionId));
        const result = batchMessages
          ? await streamChatBatch(batchMessages, onMessage, controller.signal, {
              sessionId,
              sourceNodeId: chatSourceNodeId,
              sourceNodeSessionId: sourceNodeSessionId ?? undefined,
              branch,
              providerName: requestProviderName,
              model: requestModel,
              mode,
              permissionMode,
              reasoningEffort,
              fullAccessAcknowledged: permissionMode === "full_access",
              ragMode,
            })
          : await streamChat(
              prompt ?? "",
              onMessage,
              controller.signal,
              options,
            );
        if (result === "aborted") setLast({
          running: false,
          status: "cancel",
          error: undefined,
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
      if (activeStream.cancelTimer !== undefined) window.clearTimeout(activeStream.cancelTimer);
      if (activeStreamRef.current === activeStream) activeStreamRef.current = null;
      setLocalBusy(false);
      abortRef.current = null;
    }
  }

  async function dispatchRun(
    conversationId: string,
    sessionId: string,
    prompt: string | null,
    resume = false,
    sourceNodeId: string | null = defaultSourceNodeId() ?? null,
    sourceNodeSessionId: string | null = defaultSourceNodeSessionId() ?? null,
    references?: FileReference[],
    branch = false,
    batchMessages?: QueuedMessage[],
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
        sourceNodeSessionId: sourceNodeSessionId ?? undefined,
        branch,
        references,
        batchMessages,
        ragMode,
      });
      return;
    }
    await runStream(conversationId, sessionId, prompt, resume, sourceNodeId, sourceNodeSessionId, references, branch, batchMessages);
  }

  function updateQueue(updater: (items: QueuedMessage[]) => QueuedMessage[]) {
    if (!conversation?.id) return;
    onQueuedMessagesChange(conversation.id, updater);
  }

  function queueCurrentPrompt(prompt: string, itemReferences?: FileReference[]) {
    if (!prompt.trim() && (!itemReferences || itemReferences.length === 0)) return;
    updateQueue((items) => [
      ...items,
      { id: crypto.randomUUID(), content: prompt, references: itemReferences },
    ]);
    editorRef.current?.clear();
    setInput("");
    setReferences([]);
    // The uploaded files are already represented by references on this queue
    // item.  Detach them from the composer so a subsequent queued message
    // cannot accidentally inherit the same upload; keep the server-side files
    // because the queue item still needs them when its batch is flushed.
    setPendingUploads([]);
  }

  function queueReferences(): FileReference[] {
    const uploadedReferences = pendingUploads
      .filter((upload) => upload.status === "done" && upload.path)
      .map((upload) => ({ source: "upload" as const, path: upload.path! }));
    return [...references, ...uploadedReferences].filter((reference, index, all) =>
      all.findIndex((candidate) => candidate.source === reference.source && candidate.path === reference.path) === index,
    );
  }

  function editQueuedMessage(item: QueuedMessage) {
    const currentPrompt = input.trim();
    const currentReferences = queueReferences();
    if (currentPrompt || currentReferences.length > 0) {
      updateQueue((items) => items.map((candidate) => candidate.id === item.id
        ? { ...candidate, content: currentPrompt, references: currentReferences }
        : candidate));
      editorRef.current?.clear();
      setInput("");
      setReferences([]);
      setPendingUploads([]);
      return;
    }
    updateQueue((items) => items.filter((candidate) => candidate.id !== item.id));
    editorRef.current?.restore(item.content, item.references);
    setInput(item.content);
    setReferences(item.references ?? []);
    window.setTimeout(() => editorRef.current?.focus(), 0);
  }

  function sendQueuedMessage() {
    if (!conversation?.sessionId || busy || batchFlushRef.current || queuedMessages.length === 0) return;
    batchFlushRef.current = true;
    batchInFlightIdsRef.current = new Set(queuedMessages.map((item) => item.id));
    batchSourceKeyRef.current = activeRuntimeNode ? `${activeRuntimeNode.session_id}:${activeRuntimeNode.id}` : null;
    setBatchSubmitting(true);
    void flushQueuedMessages();
  }

  async function flushQueuedMessages() {
    // Snapshot both content and IDs.  React may render a new queue while the
    // request is in flight; that new content must remain available for the
    // next run.
    const items = queuedMessages.slice();
    if (!conversation?.sessionId || items.length === 0) {
      batchFlushRef.current = false;
      batchInFlightIdsRef.current = null;
      batchExpectedUserCountRef.current = 0;
      batchKnownUserKeysRef.current = new Set();
      setBatchSubmitting(false);
      return;
    }
    if (!batchInFlightIdsRef.current) batchInFlightIdsRef.current = new Set(items.map((item) => item.id));
    batchExpectedUserCountRef.current = items.length;
    batchKnownUserKeysRef.current = new Set(
      (conversation.runtimeNodes ?? [])
        .filter((node) => node.data.type === "message" && (node.data.message as { role?: unknown } | undefined)?.role === "user")
        .map((node) => `${node.session_id}:${node.id}`),
    );
    try {
      const source = activeRuntimeNode;
      if (batchSourceKeyRef.current === null && source) {
        batchSourceKeyRef.current = `${source.session_id}:${source.id}`;
      }
      await dispatchRun(
        conversation.id,
        conversation.sessionId,
        null,
        false,
        source?.id ?? null,
        source?.session_id ?? null,
        undefined,
        false,
        items,
      );
    } catch (error) {
      // A rejected batch leaves its in-memory items untouched.  Surface the
      // failure on the current assistant projection without converting the
      // queued content into optimistic canonical messages.
      setLast({ error: String((error as Error).message ?? error), running: false, decision: undefined });
    } finally {
      batchFlushRef.current = false;
      setBatchSubmitting(false);
    }
  }

  async function runPrompt(
    prompt: string,
    target?: { conversationId: string; sessionId: string; sourceNodeId?: string; sourceNodeSessionId?: string; branch?: boolean },
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
      target ? target.sourceNodeId ?? null : defaultSourceNodeId() ?? null,
      target ? target.sourceNodeSessionId ?? null : defaultSourceNodeSessionId() ?? null,
      references,
      target?.branch ?? false,
    );
  }


  async function executeCommand(name: string, argument: string) {
    pendingRewindRef.current = null;
    editorRef.current?.clear();
    setReferences([]);
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
    // A running assistant no longer blocks the composer: a draft is handed
    // to the in-memory FIFO queue below.  Only an in-progress upload prevents
    // submission because its final reference is not available yet.
    if (pendingUploads.some((upload) => upload.status === "uploading")) return;
    if (recoveryMode && conversation?.sessionId && activeRuntimeNode) {
      await dispatchRun(
        conversation.id,
        conversation.sessionId,
        null,
        true,
        activeRuntimeNode.id,
        activeRuntimeNode.session_id,
        undefined,
        false,
      );
      return;
    }
    const mergedReferences = queueReferences();
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
    if (busy) {
      queueCurrentPrompt(prompt, mergedReferences);
      return;
    }
    const rewindTarget = pendingRewindRef.current;
    pendingRewindRef.current = null;
    editorRef.current?.clear();
    setReferences([]);
    await runPrompt(
      prompt,
      rewindTarget ? { ...rewindTarget } : undefined,
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
      return;
    }
    const activeStream = activeStreamRef.current;
    if (activeStream) {
      activeStream.stopRequested = true;
      if (activeStream.jobId && !activeStream.cancelIssued) {
        // Do not abort immediately: the final node.delete frame is the
        // durable cancel commit and must still reach the projection layer.
        void cancelJob(activeStream.jobId).catch(() => undefined);
        activeStream.cancelIssued = true;
      } else if (!activeStream.jobId && activeStream.cancelTimer === undefined) {
        activeStream.cancelTimer = window.setTimeout(() => activeStream.controller.abort(), 2000);
      }
    } else {
      abortRef.current?.abort();
    }
    // Stop is an explicit user action: switch the composer out of the busy
    // state immediately while the transport cancellation finishes in the
    // background.
    setLocalBusy(false);
    setLast({
      running: false,
      status: "cancel",
      error: undefined,
      decision: undefined,
    });
  }

  async function rewindMessage(messageId: string) {
    if (!conversation || !onRewind || busy || rewindPending) return;
    const message = conversation.messages.find((item) => item.id === messageId);
    setRewindPending(true);
    try {
      const result = await onRewind(conversation.id, messageId);
      if (result === undefined) return;
      const content = typeof result === "string" ? result : result.content;
      editorRef.current?.restore(content, message?.references ?? []);
      pendingRewindRef.current = typeof result === "string"
        ? { conversationId: conversation.id, sessionId: conversation.sessionId ?? conversation.id, branch: false }
        : {
          conversationId: conversation.id,
          sessionId: result.sessionId,
          sourceNodeId: result.sourceNodeId,
          sourceNodeSessionId: result.sourceNodeSessionId,
          branch: Boolean(result.branch),
        };
      setInput(content);
      setReferences(message?.references ?? []);
      window.setTimeout(() => editorRef.current?.focus(), 0);
    } finally {
      setRewindPending(false);
    }
  }

  function beginEdit(message: ChatMessage) {
    if (busy || !onRewind || !message.content) return;
    pendingRewindRef.current = null;
    setEditingMessageId(message.id);
    setEditingDraft(message.content);
  }

  function cancelEdit() {
    setEditingMessageId(null);
    setEditingDraft("");
  }

  async function saveEdit(message: ChatMessage) {
    if (!conversation || !onRewind || busy || !editingDraft.trim() || rewindPending || editingSubmitting || editingSubmittingRef.current) {
      return;
    }
    setRewindPending(true);
    editingSubmittingRef.current = true;
    setEditingSubmitting(true);
    try {
      const result = await onRewind(conversation.id, message.id);
      if (result === undefined) return;
      const nextPrompt = editingDraft.trim();
      const sessionId = typeof result === "string" ? conversation.sessionId : result.sessionId;
      if (!sessionId) return;
      cancelEdit();
      pendingRewindRef.current = null;
      await runPrompt(
        nextPrompt,
        {
          conversationId: conversation.id,
          sessionId,
          sourceNodeId: typeof result === "string" ? undefined : result.sourceNodeId,
          sourceNodeSessionId: typeof result === "string" ? undefined : result.sourceNodeSessionId,
          branch: typeof result === "string" ? false : Boolean(result.branch),
        },
        message.references,
      );
    } finally {
      setRewindPending(false);
      editingSubmittingRef.current = false;
      setEditingSubmitting(false);
    }
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

  function scrollToBottom() {
    const scrollContainer = chatScrollRef.current;
    if (!scrollContainer) return;
    scrollContainer.scrollTo({ top: scrollContainer.scrollHeight, behavior: "smooth" });
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
    <div className="chat-page">
      <div className="chat-content">
        <div className="chat-scroll" ref={chatScrollRef} data-conversation-scroll>
          <div className="chat-scroll-content">
            <div className="chat-messages">
              {messages.length === 0 ? (
                <div className="welcome">
                  <div className="logo">Mini-Agent</div>
                  <p className="welcome-sub">向你的智能体提问，它会调用文件、Shell、Web 等工具完成任务</p>
                </div>
              ) : messages.map((message) => message.role === "user" ? (
                <div
                  className="message user"
                  id={conversationTurnId(message.id)}
                  data-chat-anchor-key={message.id}
                  key={message.id}
                >
                  <div className={editingMessageId === message.id ? "message-content is-editing" : "message-content"}>
                    {editingMessageId === message.id ? (
                      <div className="message-edit" aria-label="编辑用户消息">
                        <Input.TextArea
                          className="message-edit-input"
                          ref={editRef}
                          aria-label="编辑用户消息"
                          value={editingDraft}
                          disabled={busy}
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
                          <Button type="primary" onClick={() => void saveEdit(message)} loading={rewindPending || editingSubmitting} disabled={!editingDraft.trim() || editingSubmitting || rewindPending || busy}>保存并重新生成</Button>
                        </div>
                      </div>
                    ) : (
                      <div
                        className="bubble user-bubble"
                        onClick={(event) => handleUserBubbleClick(event, message)}
                        title={onRewind && !busy ? "点击编辑此消息" : undefined}
                      >
                        <MarkdownContent text={message.content} />
                        {message.references && message.references.length > 0 ? (
                          <div className="message-references" aria-label="消息引用">
                            {message.references.map((reference) => (
                              <MessageReferenceChip key={`${reference.source}:${reference.path}`} reference={reference} sessionId={conversation?.sessionId} />
                            ))}
                          </div>
                        ) : null}
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
          </div>
          {!isMobile ? <ConversationTimeline messages={messages} scrollContainerRef={chatScrollRef} /> : null}
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
        editorRef={editorRef}
        onEditorChange={handleEditorChange}
        onKeyDown={handleComposerKeyDown}
        onComplete={completeCommand}
        onActiveCommandChange={setActiveCommandIndex}
        onModeChange={(value) => { onModeChange(value); if (canPatchRuntimeConfig) void updateRuntimeConfig({ running_mode: value }); setOpenSettingsSelect(null); }}
        onPermissionChange={async (value) => {
          const next = value === "approval_for_me" ? "read_only" : value;
          if (next === "full_access" && permissionMode !== "full_access") {
            const confirmed = await new Promise<boolean>((resolve) => {
              fullAccessConfirmRef.current = Modal.confirm({
                title: "启用 Full access？",
                content: "这会同时放开文件和网络访问，并标记为非沙箱运行。",
                okText: "继续",
                cancelText: "取消",
                onOk: () => {
                  fullAccessConfirmRef.current = null;
                  resolve(true);
                },
                onCancel: () => {
                  fullAccessConfirmRef.current = null;
                  resolve(false);
                },
              });
            });
            if (!confirmed) return;
          }
          setPermissionMode(next);
          if (canPatchRuntimeConfig) void updateRuntimeConfig({ permission_mode: next, full_access_acknowledged: next === "full_access" });
          setOpenSettingsSelect(null);
        }}
        onReasoningChange={(value) => { setReasoningEffort(value); setRuntimeModel((current) => ({ ...current, reasoning_effort: value })); if (canPatchRuntimeConfig) void updateRuntimeConfig({ model: { reasoning_effort: value } }); setOpenSettingsSelect(null); }}
        onSettingsSelectChange={setOpenSettingsSelect}
        onOpenSettings={() => setSettingsOpen(true)}
        onCloseSettings={() => setSettingsOpen(false)}
        onStop={stop}
        onSend={() => void send()}
        actionMode={actionMode}
        submitDisabled={projectUnavailable || (actionMode === "send" && busy && !hasDraft)}
        disabled={projectUnavailable}
        disabledReason={conversation?.projectAvailable === false ? "项目 cwd 不可用，恢复文件夹后才能运行" : undefined}
        startMode={recoveryMode}
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
        onQueueDelete={(item) => updateQueue((items) => items.filter((candidate) => candidate.id !== item.id))}
        onRemoveUpload={removePendingUpload}
        onRetryUpload={retryUpload}
        onUploadPreview={(index) => {
          const upload = pendingUploads[index];
          if (upload?.path && conversation?.sessionId) {
            window.open(sessionFileContentUrl(conversation.sessionId, "upload", upload.path), "_blank", "noopener");
          }
        }}
      />
      <FloatButton
        className="chat-scroll-bottom-button"
        icon={<VerticalAlignBottomOutlined />}
        tooltip="滚动到底部"
        aria-label="滚动到底部"
        style={{ right: 24, bottom: 96 }}
        onClick={scrollToBottom}
      />
    </div>
  );
}
