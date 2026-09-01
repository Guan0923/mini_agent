import { useEffect, useMemo, useRef, useState } from "react";
import { getSessionNodes, sendAgentThreadMessage, streamAgentThread } from "../../api";
import { withLoadedTurns } from "../../app/conversationProjection";
import { applyRuntimeNodeFrame, runtimeNodeAccumulator } from "../../app/runtime/runtimeNodeReducer";
import { isRuntimeTurnNode } from "../../app/runtime/runtimeNodeNormalization";
import type {
  ChatMessage,
  ChatMode,
  Conversation,
  FileReference,
  PermissionMode,
  RuntimeConfigModel,
  RuntimeTreeNode,
} from "../../types";

interface UseAgentThreadViewOptions {
  canonical: Conversation | null;
  enabled: boolean;
  onUpdate: (id: string, updater: (conversation: Conversation) => Conversation) => void;
}

function mergeNodes(current: RuntimeTreeNode[] | undefined, updates: RuntimeTreeNode[]): RuntimeTreeNode[] {
  const nodes = new Map((current ?? []).map((node) => [`${node.session_id}:${node.id}`, node] as const));
  for (const node of updates) nodes.set(`${node.session_id}:${node.id}`, node);
  return [...nodes.values()];
}

export function useAgentThreadView({ canonical, enabled, onUpdate }: UseAgentThreadViewOptions) {
  const [selectedByRootThread, setSelectedByRootThread] = useState<Record<string, string>>({});
  const [pendingByThread, setPendingByThread] = useState<Record<string, ChatMessage[]>>({});
  const [treeInvalidation, setTreeInvalidation] = useState(0);
  const [streamError, setStreamError] = useState<string | null>(null);
  const updateRef = useRef(onUpdate);
  const rootTreeStateByThread = useRef<Record<string, string>>({});
  updateRef.current = onUpdate;

  const sessionId = canonical?.sessionId;
  const rootThreadId = canonical?.threadId;
  const selectedThreadId = rootThreadId
    ? selectedByRootThread[rootThreadId] ?? rootThreadId
    : canonical?.threadId;
  const isSubagent = Boolean(enabled && sessionId && selectedThreadId && selectedThreadId !== rootThreadId);
  const rootTurnState = (canonical?.runtimeNodes ?? [])
    .filter(isRuntimeTurnNode)
    .filter((node) => node.thread_id === rootThreadId)
    .map((node) => `${node.id}:${node.status}`)
    .join("|");
  const lastCanonicalMessage = canonical?.messages[canonical.messages.length - 1];
  const rootTreeState = `${rootTurnState}#${canonical?.messages.length ?? 0}:${lastCanonicalMessage?.running ? "running" : "idle"}`;

  useEffect(() => {
    if (!rootThreadId) return;
    const previous = rootTreeStateByThread.current[rootThreadId];
    rootTreeStateByThread.current[rootThreadId] = rootTreeState;
    if (previous !== undefined && previous !== rootTreeState) {
      setTreeInvalidation((current) => current + 1);
    }
  }, [rootThreadId, rootTreeState]);

  const viewConversation = useMemo(() => {
    if (!canonical || !isSubagent || !selectedThreadId) return canonical;
    const turns = (canonical.runtimeNodes ?? [])
      .filter(isRuntimeTurnNode)
      .filter((node) => node.thread_id === selectedThreadId)
      .sort((left, right) => left.timestamp.localeCompare(right.timestamp));
    return withLoadedTurns(
      {
        ...canonical,
        threadId: selectedThreadId,
        activeTurnId: undefined,
        lastNodeId: undefined,
        hiddenBeforeTurnId: turns[0]?.parent_id || undefined,
        messages: [],
      },
      canonical.runtimeNodes ?? [],
    );
  }, [canonical, isSubagent, selectedThreadId]);

  const pendingKey = sessionId && selectedThreadId ? `${sessionId}:${selectedThreadId}` : "";
  const canonicalDeliveryIds = useMemo(
    () => new Set((viewConversation?.messages ?? []).flatMap((item) => item.deliveryId ? [item.deliveryId] : [])),
    [viewConversation?.messages],
  );
  const pendingMessages = (pendingByThread[pendingKey] ?? []).filter(
    (item) => !item.deliveryId || !canonicalDeliveryIds.has(item.deliveryId),
  );
  const displayConversation = viewConversation && pendingMessages.length > 0
    ? { ...viewConversation, messages: [...viewConversation.messages, ...pendingMessages] }
    : viewConversation;

  useEffect(() => {
    if (!pendingKey || canonicalDeliveryIds.size === 0) return;
    setPendingByThread((current) => {
      const existing = current[pendingKey];
      if (!existing?.some((item) => item.deliveryId && canonicalDeliveryIds.has(item.deliveryId))) return current;
      return {
        ...current,
        [pendingKey]: existing.filter((item) => !item.deliveryId || !canonicalDeliveryIds.has(item.deliveryId)),
      };
    });
  }, [canonicalDeliveryIds, pendingKey]);

  useEffect(() => {
    if (!enabled || !isSubagent || !canonical?.id || !sessionId || !selectedThreadId) return;
    const controller = new AbortController();
    const conversationId = canonical.id;
    let retryMs = 500;
    let lastEventId = "";

    const commitNodes = (nodes: RuntimeTreeNode[]) => {
      updateRef.current(conversationId, (current) => ({
        ...current,
        runtimeNodes: mergeNodes(current.runtimeNodes, nodes),
      }));
    };

    const reload = async () => {
      const nodes = await getSessionNodes(sessionId);
      if (!controller.signal.aborted) commitNodes(nodes);
    };

    const run = async () => {
      while (!controller.signal.aborted) {
        const accumulator = runtimeNodeAccumulator();
        try {
          await reload();
          const result = await streamAgentThread(sessionId, selectedThreadId, (event) => {
            if (event.type === "thread.ready") {
              setStreamError(null);
              retryMs = 500;
              return;
            }
            if (event.type === "turn.terminal") {
              setTreeInvalidation((current) => current + 1);
              void reload();
              return;
            }
            if (event.type === "turn.snapshot") {
              const key = `${event.turn.session_id}:${event.turn.id}`;
              accumulator.nodes.delete(key);
              accumulator.revisions.delete(key);
            }
            const turn = applyRuntimeNodeFrame(accumulator, event);
            commitNodes([turn]);
          }, controller.signal, lastEventId, (cursor) => {
            lastEventId = cursor;
          });
          if (result === "aborted") return;
          throw new Error("Agent Thread SSE ended unexpectedly.");
        } catch (error) {
          if (controller.signal.aborted) return;
          setStreamError(String((error as Error).message ?? error));
          await new Promise<void>((resolve) => globalThis.setTimeout(resolve, retryMs));
          retryMs = Math.min(5_000, retryMs * 2);
        }
      }
    };
    void run();
    return () => controller.abort();
  }, [canonical?.id, enabled, isSubagent, selectedThreadId, sessionId]);

  function selectThread(threadId: string) {
    if (!rootThreadId) return;
    setSelectedByRootThread((current) => ({ ...current, [rootThreadId]: threadId }));
  }

  async function sendMessage(values: {
    content: string;
    references?: FileReference[];
    mode: ChatMode;
    permissionMode: PermissionMode;
    providerName?: string;
    model?: RuntimeConfigModel;
  }) {
    if (!sessionId || !selectedThreadId || !isSubagent) throw new Error("当前没有选中的 Subagent Thread。");
    const response = await sendAgentThreadMessage(selectedThreadId, {
      sessionId,
      ...values,
      fullAccessAcknowledged: values.permissionMode === "full_access",
    });
    const message: ChatMessage = {
      id: `pending:${response.delivery_id}`,
      role: "user",
      content: values.content,
      events: [],
      references: values.references,
      deliveryId: response.delivery_id,
      pending: true,
      timelineSource: "steering",
    };
    setPendingByThread((current) => ({
      ...current,
      [pendingKey]: [...(current[pendingKey] ?? []), message],
    }));
    return response;
  }

  return {
    conversation: displayConversation,
    rootThreadId,
    selectedThreadId,
    isSubagent,
    treeInvalidation,
    streamError,
    selectThread,
    sendMessage,
  };
}
