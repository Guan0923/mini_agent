import type { TextAreaRef } from "antd/es/input/TextArea";
import { useEffect, useRef, useState, type MouseEvent as ReactMouseEvent } from "react";
import { patchTurnCurrentData } from "../../api";
import { projectTurnPath } from "../../app/runtime/runtimeDetailProjection";
import { isRuntimeTurnNode } from "../../app/runtime/runtimeNodeNormalization";
import type { ChatMessage, Conversation, FileReference, RuntimeStateNode } from "../../types";
import type { RewindResult } from "./contracts";

interface MessageEditingOptions {
  conversation: Conversation | null;
  interactionBusy: boolean;
  activeRuntimeNode?: RuntimeStateNode;
  onRewind?: (conversationId: string, messageId: string) => Promise<RewindResult | string | undefined>;
  onFork?: (conversationId: string, messageId: string) => Promise<void>;
  onUpdate: (id: string, updater: (conversation: Conversation) => Conversation) => void;
  runPrompt: (
    prompt: string,
    target?: { conversationId: string; sessionId: string; sourceNodeId?: string; rewindTurnId?: string },
    references?: FileReference[],
  ) => Promise<void>;
  onError: (error: unknown) => void;
}

function nativeTextArea(ref: TextAreaRef | null): HTMLTextAreaElement | null {
  return ref?.resizableTextArea?.textArea ?? null;
}

export function useMessageEditing({
  conversation,
  interactionBusy,
  activeRuntimeNode,
  onRewind,
  onFork,
  onUpdate,
  runPrompt,
  onError,
}: MessageEditingOptions) {
  const [editingMessageId, setEditingMessageId] = useState<string | null>(null);
  const [editingDraft, setEditingDraft] = useState("");
  const [rewindPending, setRewindPending] = useState(false);
  const [editingSubmitting, setEditingSubmitting] = useState(false);
  const editingSubmittingRef = useRef(false);
  const editRef = useRef<TextAreaRef>(null);

  useEffect(() => {
    editingSubmittingRef.current = false;
    setEditingSubmitting(false);
  }, [conversation?.id]);

  useEffect(() => {
    if (editingMessageId) {
      editRef.current?.focus();
      nativeTextArea(editRef.current)?.select();
    }
  }, [editingMessageId]);

  function beginEdit(message: ChatMessage) {
    if (interactionBusy || !onRewind || !message.content) return;
    setEditingMessageId(message.id);
    setEditingDraft(message.content);
  }

  function cancelEdit() {
    setEditingMessageId(null);
    setEditingDraft("");
  }

  async function saveEdit(message: ChatMessage) {
    if (
      !conversation
      || !onRewind
      || interactionBusy
      || !editingDraft.trim()
      || rewindPending
      || editingSubmitting
      || editingSubmittingRef.current
    ) return;
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
      await runPrompt(
        nextPrompt,
        {
          conversationId: conversation.id,
          sessionId,
          rewindTurnId: typeof result === "string" ? message.nodeId : result.rewindTurnId ?? message.nodeId,
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
      interactionBusy
      || !onRewind
      || !message.content
      || event.button !== 0
      || event.altKey
      || event.ctrlKey
      || event.metaKey
      || event.shiftKey
    ) return;
    const target = event.target as HTMLElement;
    if (target.closest("a,button,textarea,input,code,pre,details,summary")) return;
    const selection = window.getSelection();
    if (selection && !selection.isCollapsed) return;
    beginEdit(message);
  }

  function forkMessage(messageId: string) {
    if (!conversation || !onFork || interactionBusy) return;
    void onFork(conversation.id, messageId);
  }

  async function changeMessageVersion(message: ChatMessage, direction: -1 | 1) {
    if (!conversation || !message.nodeId || interactionBusy) return;
    const turn = conversation.runtimeNodes?.find((item) => item.id === message.nodeId);
    if (!turn || !isRuntimeTurnNode(turn)) return;
    const nextIndex = turn.current_data_idx + direction;
    if (nextIndex < 0 || nextIndex >= turn.data.length) return;
    try {
      const updated = await patchTurnCurrentData(turn.id, nextIndex);
      onUpdate(conversation.id, (current) => {
        const map = new Map((current.runtimeNodes ?? []).map((item) => [`${item.session_id}:${item.id}`, item] as const));
        map.set(`${updated.session_id}:${updated.id}`, updated);
        const activeTurnId = current.activeTurnId ?? activeRuntimeNode?.id ?? updated.id;
        return { ...current, runtimeNodes: [...map.values()], messages: projectTurnPath(map, activeTurnId) };
      });
    } catch (error) {
      onError(error);
    }
  }

  function messageVersion(message: ChatMessage) {
    const turn = conversation?.runtimeNodes?.find((item) => item.id === message.nodeId);
    return turn && isRuntimeTurnNode(turn) ? { index: turn.current_data_idx, total: turn.data.length } : undefined;
  }

  return {
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
  };
}
