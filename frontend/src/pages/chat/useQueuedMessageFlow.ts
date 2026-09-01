import { useEffect, useRef, useState, type Dispatch, type MutableRefObject, type SetStateAction } from "react";
import { createQueuedMessage, deleteQueuedMessage, steerTurn, updateQueuedMessage } from "../../api";
import type { QueuedMessage } from "../../app/types";
import type { ChatMessage, Conversation, FileReference, RuntimeStateNode } from "../../types";
import type { FileMentionEditorHandle } from "./FileMentionEditor";
import type { PendingUpload } from "./contracts";

interface QueuedRunRequest {
  conversationId: string;
  sessionId: string;
  sourceNodeId: string | null;
  deliveryId: string;
  messageIds: string[];
  onBaseline: () => void;
}

interface UseQueuedMessageFlowOptions {
  conversation: Conversation | null;
  activeRuntimeNode?: RuntimeStateNode;
  queuedMessages: QueuedMessage[];
  queueSubmitting: boolean;
  sandboxBlocked: boolean;
  isSubagent: boolean;
  input: string;
  collectedReferences: () => FileReference[];
  clearComposer: () => void;
  editorRef: MutableRefObject<FileMentionEditorHandle | null>;
  setInput: Dispatch<SetStateAction<string>>;
  setReferences: Dispatch<SetStateAction<FileReference[]>>;
  setPendingUploads: Dispatch<SetStateAction<PendingUpload[]>>;
  setQueueSubmitting: Dispatch<SetStateAction<boolean>>;
  onQueuedMessagesChange: (conversationId: string, updater: (items: QueuedMessage[]) => QueuedMessage[]) => void;
  onQueuedMessagesRefresh: (conversationId: string) => Promise<void>;
  onSetLast: (fields: Partial<ChatMessage>) => void;
  onDispatch: (request: QueuedRunRequest) => Promise<void>;
  onStop: () => void;
  onWarning: (content: string) => void;
}

export function useQueuedMessageFlow({
  conversation,
  activeRuntimeNode,
  queuedMessages,
  queueSubmitting,
  sandboxBlocked,
  isSubagent,
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
  onSetLast,
  onDispatch,
  onStop,
  onWarning,
}: UseQueuedMessageFlowOptions) {
  const queueFlushRef = useRef(false);
  const queueAutoBlockedRef = useRef(false);
  const acknowledgedDeliveryIdsRef = useRef(new Set<string>());
  const [editingQueuedMessageId, setEditingQueuedMessageId] = useState<string | null>(null);

  useEffect(() => {
    setEditingQueuedMessageId(null);
  }, [conversation?.id]);

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
      && !isSubagent
      && queuedMessages.length > 0
      && conversation?.id
      && (status === "success" || status === "failed")
    ) {
      queueFlushRef.current = true;
      setQueueSubmitting(true);
      void flushQueuedMessages();
    }
  // `queueSubmitting` deliberately retriggers the effect after a completed
  // flush so entries appended while that request was in flight start the next
  // FIFO pass.
  }, [activeRuntimeNode?.id, activeRuntimeNode?.status, queueSubmitting, queuedMessages.length, conversation?.id, sandboxBlocked]);

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

  function updateQueue(updater: (items: QueuedMessage[]) => QueuedMessage[]) {
    if (conversation?.id) onQueuedMessagesChange(conversation.id, updater);
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
      onSetLast({ error: String((error as Error).message ?? error) });
      return;
    }
    clearComposer();
    setPendingUploads([]);
  }

  function editQueuedMessage(item: QueuedMessage) {
    if (item.state !== "pending") return;
    const currentPrompt = input.trim();
    const currentReferences = collectedReferences();
    if (currentPrompt || currentReferences.length > 0) {
      onWarning("输入框有内容，无法修改队列消息");
      return;
    }
    setEditingQueuedMessageId(item.id);
    editorRef.current?.restore(item.content, item.references);
    setInput(item.content);
    setReferences(item.references ?? []);
    window.setTimeout(() => editorRef.current?.focus(), 0);
  }

  function sendQueuedMessage(item: QueuedMessage) {
    if (item.state === "pending") void submitSteering([item]);
  }

  async function deleteMessage(item: QueuedMessage) {
    if (isSubagent || item.state !== "pending" || !conversation?.threadId) return;
    try {
      await deleteQueuedMessage(conversation.threadId, item.id);
      updateQueue((items) => items.filter((candidate) => candidate.id !== item.id));
    } catch (error) {
      onSetLast({ error: String((error as Error).message ?? error) });
    }
  }

  async function submitSteering(items: QueuedMessage[]) {
    if (sandboxBlocked || !conversation?.id || activeRuntimeNode?.status !== "running" || items.length === 0) return;
    try {
      await steerTurn(activeRuntimeNode.id, crypto.randomUUID(), items.map((item) => item.id));
      await onQueuedMessagesRefresh(conversation.id);
    } catch (error) {
      onSetLast({ error: String((error as Error).message ?? error) });
    }
  }

  function pauseOrSteer() {
    const pending = queuedMessages.filter((item) => item.state === "pending");
    if (pending.length > 0) {
      void submitSteering(pending);
      return;
    }
    if (queuedMessages.length === 0) onStop();
  }

  async function flushQueuedMessages() {
    const items = queuedMessages.slice();
    if (sandboxBlocked || !conversation?.sessionId || items.length === 0) {
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
    let acknowledged = false;
    try {
      await onDispatch({
        conversationId: conversation.id,
        sessionId: conversation.sessionId,
        sourceNodeId: activeRuntimeNode?.id ?? null,
        deliveryId: crypto.randomUUID(),
        messageIds: pendingItems.map((item) => item.id),
        onBaseline: () => {
          if (acknowledged) return;
          acknowledged = true;
          void onQueuedMessagesRefresh(conversation.id);
        },
      });
      if (!acknowledged) queueAutoBlockedRef.current = true;
    } catch (error) {
      onSetLast({ error: String((error as Error).message ?? error), running: false, decision: undefined });
      queueAutoBlockedRef.current = true;
    } finally {
      queueFlushRef.current = false;
      setQueueSubmitting(false);
    }
  }

  return { deleteQueuedMessage: deleteMessage, editQueuedMessage, pauseOrSteer, queueCurrentPrompt, sendQueuedMessage };
}
